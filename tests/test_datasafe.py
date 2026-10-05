"""数据安全红线：**任何情况下都不能把用户的原文件弄丢或弄坏。**

这里的每一条都是**真的复现过**的，
不是假想：用合成夹具跑出过"源文件没了"、"返回 ok=true 但源图被覆盖"
这样的结果。所以这个文件里的断言是**契约**，不是风格偏好 —— 红了就不能发版。

最初的七条：

  * 部分失败时，失败那张的原文件被别的成功任务盖掉
  * 提交时先删源再改名，改名失败原文件已经没了
  * 导出目录选"清空后写入"，能把源目录整个 rmtree 掉
  * 前缀允许绝对路径/路径分隔符，产物能写到导出目录之外
  * 透明 GIF 旋转后透明度丢失
  * 原地直写动图时 copy2(src, src) 自己复制自己，失败
  * 动图缩放成功，但结果里不报 resized

后来又陆续补了几批，见 main() 里的分组。
动图相关的另有几条在 test_anim.py，闸门额度的在 test_probe.py。

用法:  python tests\test_datasafe.py
      （或 python tests\run_all.py datasafe）
清空导出目录那一条会真的建窗口，需要桌面会话。其余不需要。
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image

from imgwb import api
from imgwb.config import TaskResult
from imgwb.tasks import commit_staged, sweep_temps
from imgwb.utils import make_temp_path

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def jpg(path, color='red', size=(12, 8)):
    Image.new('RGB', size, color).save(path)


def gif(path, colors=('red', 'blue'), size=(12, 8)):
    frames = [Image.new('RGB', size, c) for c in colors]
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=[80, 90], loop=0)


def transparent_gif(path, size=(12, 8)):
    """两帧、带透明索引的调色板 GIF。"""
    pal = [0, 0, 0, 255, 0, 0, 0, 255, 0] + [0] * (768 - 9)
    frames = []
    for color in (1, 2):
        f = Image.new('P', size, 0)
        f.putpalette(pal)
        f.paste(color, (3, 2, 7, 5))
        frames.append(f)
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=[80, 90], loop=0, transparency=0, disposal=2)


def alpha_zeros(path):
    """完全透明的像素有几个。"""
    with Image.open(path) as im:
        return sum(a == 0 for a in im.convert('RGBA').getchannel('A').getdata())


# ================================================================
def b1_failed_source_not_overwritten(work):
    """一张处理失败时，它的原文件不能被别的成功任务盖掉。

    `1.jpg → 2.jpg`、`2.jpg → 3.jpg`。后一项失败（2.jpg 不是图片），
    前一项仍然提交，`os.replace` 正好盖在 2.jpg 上 ——
    用户那份"坏但属于他自己"的文件就没了。
    """
    d = os.path.join(work, 'partial_failure')
    os.makedirs(d)
    jpg(os.path.join(d, '1.jpg'))
    damaged = b'original damaged file still owned by user'
    with open(os.path.join(d, '2.jpg'), 'wb') as fh:
        fh.write(damaged)

    r = api.run(d, name='number', start=2, digits=1, rotate='cw90',
                safe_write=True, workers=1)

    with open(os.path.join(d, '2.jpg'), 'rb') as fh:
        now = fh.read()
    check('失败那张的原文件原样保留', now == damaged,
          f'{len(now)} 字节，期望 {len(damaged)}')

    # 同一个路径既算失败来源、又算成功产物，这个返回值本身就是自相矛盾的
    failed_paths = {os.path.normcase(f['path']) for f in r['failed']}
    out_paths = {os.path.normcase(p) for p in r['outputs']}
    overlap = failed_paths & out_paths
    check('同一个路径不会既是失败来源又是成功产物', not overlap,
          str(sorted(overlap)))


def b1_missing_temp_protects_source(work):
    """临时文件丢了的那一项，它的源文件也要受保护。

    代码已经会把"临时文件不见了"的那项从 staged 里剔掉，并说一句
    「放弃提交以免弄丢原文件」—— 但只拦住了直接 remove，没拦住**别的任务**
    用 os.replace 盖上去。于是提示说保住了，实际内容已经是别人的产物。
    """
    d = os.path.join(work, 'missing_temp_collision')
    os.makedirs(d)
    a, b, c = (os.path.join(d, n) for n in ('1.jpg', '2.jpg', '3.jpg'))
    for p, data in ((a, b'original one'), (b, b'original two')):
        with open(p, 'wb') as fh:
            fh.write(data)
    ta, tb = make_temp_path(b), make_temp_path(c)
    with open(ta, 'wb') as fh:
        fh.write(b'processed one')
    # tb 故意不创建：模拟临时文件被中途清扫掉

    commit_staged([TaskResult(True, a, b, tmp_path=ta),
                   TaskResult(True, b, c, tmp_path=tb)], False)

    with open(b, 'rb') as fh:
        now = fh.read()
    check('说了"放弃提交以免弄丢原文件"，那就真的不能丢',
          now == b'original two', now[:40])


def b2_replace_failure_keeps_original(work):
    """提交阶段改名失败时，原文件必须还能找回来。

    `commit_staged` 现在是"先把所有源文件删一遍，再逐个 os.replace"。
    改名失败（这里让目标变成一个目录，真实会报 WinError 5）时，
    源文件已经删了 —— 原始字节和产物一起没。
    """
    d = os.path.join(work, 'replace_failure')
    os.makedirs(d)
    src, dst = os.path.join(d, 'source.jpg'), os.path.join(d, 'output.jpg')
    original = b'original unique image bytes'
    with open(src, 'wb') as fh:
        fh.write(original)
    tmp = make_temp_path(dst)
    with open(tmp, 'wb') as fh:
        fh.write(b'processed image bytes')
    os.mkdir(dst)                      # 制造一次真实的提交期文件系统故障

    errors = commit_staged([TaskResult(True, src, dst, tmp_path=tmp)], False)
    check('改名失败时有明确报错', bool(errors), str(errors)[:120])

    # 原字节必须还在某处：源文件本身，或报错里点明的恢复位置
    recoverable = os.path.isfile(src) and open(src, 'rb').read() == original
    check('改名失败后原文件内容还找得回来', recoverable,
          f'src 还在={os.path.isfile(src)}')

    # 而且不能被常规临时文件清扫顺手删掉
    swept = sweep_temps([d])
    still = os.path.isfile(src) and open(src, 'rb').read() == original
    check('清扫临时文件不会把恢复用的内容一起删掉', still,
          f'清扫了 {swept} 个')


def b4_prefix_cannot_escape(work):
    """前缀是**一个文件名片段**，不是路径。

    `os.path.join(导出目录, f'{prefix}_{编号}{后缀}')` —— Windows 下绝对路径
    前缀会吃掉前面的导出目录，产物直接落到源目录里把源图覆盖，而 API 返回
    ok=true、导出目录压根没生成。
    """
    d = os.path.join(work, 'prefix_escape')
    srcdir, outdir = os.path.join(d, 'source'), os.path.join(d, 'export')
    os.makedirs(srcdir)
    jpg(os.path.join(srcdir, 'a.jpg'))
    victim = os.path.join(srcdir, 'victim_01.jpg')
    jpg(victim, 'blue')
    with open(victim, 'rb') as fh:
        before = fh.read()

    raised = None
    try:
        r = api.run(srcdir, output='new', dest=outdir, keep_structure=False,
                    name='prefix', prefix=os.path.join(srcdir, 'victim'),
                    workers=1)
    except Exception as exc:
        raised, r = exc, None

    with open(victim, 'rb') as fh:
        after = fh.read()
    check('带路径的前缀不会覆盖导出目录之外的文件', after == before,
          '源图被改写了' if after != before else '')
    # 期望的行为是直接报参数错误；退而求其次至少不能声称成功
    ok_claimed = bool(r and r.get('ok'))
    check('带路径的前缀被当参数错误挡掉（而不是静悄悄成功）',
          raised is not None or not ok_claimed,
          f'raised={type(raised).__name__ if raised else None}, '
          f'ok={ok_claimed}')


def b3_clear_export_keeps_source(work, app):
    """清空导出目录绝不能删到来源。

    源是 `parent/source`，导出填 `parent`，关掉"保留目录结构"，
    非空目录对话框选"清空后写入" —— 平铺模式把整个 parent 当待清空目录，
    它的子目录 source 就被 rmtree 了。清理发生在扫描之前，安全写入和取消
    撤回都来不及保护输入。
    """
    d = os.path.join(work, 'clear_export_parent')
    srcdir = os.path.join(d, 'source')
    os.makedirs(srcdir)
    photo = os.path.join(srcdir, 'photo.jpg')
    jpg(photo)

    app.keep_structure.set(False)
    app.dir_suffix_val.set('_已处理')
    app._ask_merge_choice = lambda *a, **k: 'clear'          # 用户选"清空"
    app._confirm_output_dirs(srcdir, d)

    check('清空导出目录没有删掉来源目录', os.path.isdir(srcdir),
          '来源目录被删了' if not os.path.isdir(srcdir) else '')
    check('清空导出目录没有删掉来源里的图片', os.path.isfile(photo),
          '源图被删了' if not os.path.isfile(photo) else '')


def b5_gif_keeps_transparency(work):
    """透明 GIF 旋转后，透明度不能丢。

    动图逐帧那条路把帧转成 RGB 再回量化，然后**无条件删掉 transparency** ——
    alpha 和输出透明索引一起没了。那行是为了绕开 Pillow 写入器的崩溃加的，
    代价当时没算到。
    """
    d = os.path.join(work, 'gif_transparency')
    srcdir, outdir = os.path.join(d, 'source'), os.path.join(d, 'export')
    os.makedirs(srcdir)
    src = os.path.join(srcdir, 'transparent.gif')
    transparent_gif(src)
    before = alpha_zeros(src)

    r = api.run(srcdir, output='new', dest=outdir, name='keep',
                rotate='cw90', workers=1)
    check('旋转后产物还在', bool(r['outputs']), str(r['failed'])[:120])
    if r['outputs']:
        after = alpha_zeros(r['outputs'][0])
        check('透明 GIF 旋转后仍然透明', after > 0,
              f'透明像素 {before} -> {after}')


def b6_inplace_animated_same_file(work):
    """原地直写动图时，源和目标是同一个文件，应当直接算成功。

    这条分支压根不需要变换（不旋转、不换格式、不重编码），却执行了
    `shutil.copy2(src, src)` —— Windows 上直接报权限/占用错误。
    """
    d = os.path.join(work, 'animated_direct')
    os.makedirs(d)
    gif(os.path.join(d, 'anim.gif'))

    r = api.run(d, name='keep', compress_only=True, safe_write=False,
                workers=1)
    check('原地直写动图不报错', r['ok'] and not r['failed'],
          str(r['failed'])[:140])


def b7_animated_resize_reported(work):
    """动图缩放成功了，结果里就要报 resized。

    动图分支调了 shrink_to，但 TaskResult 没带 resized ——
    API 的 `resized` 和界面的缩放计数都靠这个字段。
    """
    d = os.path.join(work, 'animated_resize')
    srcdir, outdir = os.path.join(d, 'source'), os.path.join(d, 'export')
    os.makedirs(srcdir)
    gif(os.path.join(srcdir, 'anim.gif'))

    r = api.run(srcdir, output='new', dest=outdir, name='keep', max_edge=6,
                workers=1)
    if not r['outputs']:
        check('缩放后产物还在', False, str(r['failed'])[:120])
        return
    with Image.open(r['outputs'][0]) as im:
        size = im.size
    check('动图真的被缩小了', size == (6, 4), str(size))
    check('缩小了就要报 resized', bool(r['resized']),
          f"size={size}, resized={r['resized']}")




# ================================================================
# 2026-10-04 追加的场景。
# 上一轮每条只验了最简情形就收工，下面这几条正是它漏掉的那一半。
# ================================================================
def _sha(p):
    import hashlib
    with open(p, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def r1_chain_three_files(work):
    """连锁改名时，保护必须**一路往前传**，不能只拦一层。

    `1→2`、`2→3`、`3→4`，其中 3 是坏文件。
    3 失败 → 拒绝 `2→3`（对）；但这样一来 2 也成了"没有产物的原文件"，
    `1→2` 还是会盖上去，把一张**完好的** 2.jpg 毁掉。
    上一轮只测了两个文件，正好测不出这一层。
    """
    d = os.path.join(work, 'chain3')
    os.makedirs(d)
    jpg(os.path.join(d, '1.jpg'), 'red')
    jpg(os.path.join(d, '2.jpg'), 'blue', (14, 9))      # 内容和 1 不同
    with open(os.path.join(d, '3.jpg'), 'wb') as fh:
        fh.write(b'not an image at all')

    before2 = _sha(os.path.join(d, '2.jpg'))
    r = api.run(d, name='number', start=2, digits=1, rotate='cw90',
                safe_write=True, workers=1)
    still = os.path.isfile(os.path.join(d, '2.jpg'))
    ok2 = still and _sha(os.path.join(d, '2.jpg')) == before2
    check('链条中间那张完好的原图没被盖掉', ok2,
          '' if ok2 else ('2.jpg 被改写了' if still else '2.jpg 没了'))
    failed_paths = {os.path.normcase(f['path']) for f in r['failed']}
    out_paths = {os.path.normcase(p) for p in r['outputs']}
    check('链条下同一路径不会既失败又成功',
          not (failed_paths & out_paths),
          str(sorted(failed_paths & out_paths)))


def r1_chain_missing_temp(work):
    """链条末端是"临时文件丢了"，保护同样要往前传。"""
    d = os.path.join(work, 'chain_missing')
    os.makedirs(d)
    a, b, c, e = (os.path.join(d, n)
                  for n in ('1.jpg', '2.jpg', '3.jpg', '4.jpg'))
    for p, data in ((a, b'one'), (b, b'two'), (c, b'three')):
        with open(p, 'wb') as fh:
            fh.write(data)
    ta, tb, tc = make_temp_path(b), make_temp_path(c), make_temp_path(e)
    for t in (ta, tb):
        with open(t, 'wb') as fh:
            fh.write(b'processed')
    # tc 故意不建：3→4 的临时文件丢了

    commit_staged([TaskResult(True, a, b, tmp_path=ta),
                   TaskResult(True, b, c, tmp_path=tb),
                   TaskResult(True, c, e, tmp_path=tc)], False)

    got_c = open(c, 'rb').read() if os.path.isfile(c) else b''
    got_b = open(b, 'rb').read() if os.path.isfile(b) else b''
    check('临时文件丢失时，紧邻的上游不许覆盖', got_c == b'three',
          f'3.jpg = {got_c[:20]!r}')
    check('临时文件丢失时，更上游也不许覆盖', got_b == b'two',
          f'2.jpg = {got_b[:20]!r}')


def r2_empty_suffix_clear(work, app):
    """目录后缀留空时，输出目录**就是来源目录本身**。

    这时"清空输出目录"遍历的是来源目录**里面**的内容。上一轮的保护只看
    "这一项是不是来源、或者装着来源"，而来源里面的文件两样都不是，
    于是原图被直接删光。
    """
    d = os.path.join(work, 'empty_suffix')
    srcdir = os.path.join(d, 'source')
    os.makedirs(srcdir)
    photo = os.path.join(srcdir, 'photo.jpg')
    jpg(photo)

    app.keep_structure.set(True)
    app.dir_suffix_val.set('')          # 后缀留空 -> 输出目录 == 来源目录
    app._ask_merge_choice = lambda *a, **k: 'clear'
    app._confirm_output_dirs(srcdir, d)
    check('空后缀下"清空"没有删掉来源里的原图',
          os.path.isfile(photo),
          '原图被删了' if not os.path.isfile(photo) else '')


def opaque_per_frame(path):
    """每一帧有多少个**不透明**像素。比"有没有透明像素"严格得多。"""
    out = []
    with Image.open(path) as im:
        for i in range(getattr(im, 'n_frames', 1)):
            im.seek(i)
            out.append(sum(a != 0 for a in
                           im.convert('RGBA').getchannel('A').getdata()))
    return out


def r4_opaque_color_survives(work):
    """透明色和不透明前景**RGB 相同**时，前景不能被量化成透明。

    调色板里索引 0 是"透明的黑"，索引 1 是"不透明的黑"。先转 RGB 再按整张
    调色板量化，黑色前景会被映射到索引 0（透明）；而"把 alpha=0 的位置写回
    透明索引"那一步只负责加透明，不会把错分的像素纠回来。
    """
    d = os.path.join(work, 'dup_rgb')
    srcdir, outdir = os.path.join(d, 'source'), os.path.join(d, 'export')
    os.makedirs(srcdir)
    src = os.path.join(srcdir, 'dup.gif')
    pal = [0, 0, 0, 0, 0, 0, 255, 255, 255] + [0] * (768 - 9)
    f1 = Image.new('P', (12, 8), 0)     # 底 = 索引 0（透明的黑）
    f1.putpalette(pal)
    f1.paste(1, (2, 1, 7, 6))           # 前景 = 索引 1（不透明的黑），25 像素
    f2 = Image.new('P', (12, 8), 0)
    f2.putpalette(pal)
    # 第二帧**必须和第一帧不一样**，否则编码器会把两帧并成一帧 ——
    # 我第一版就是这么写的，结果"通过"了，其实根本没测到东西。
    f2.paste(2, (3, 2, 8, 7))           # 索引 2（白），同样 25 像素
    # disposal=2：让两帧各自独立，不然源自己就会累积，基准数就不对了
    f1.save(src, save_all=True, append_images=[f2],
            duration=[80, 90], loop=0, transparency=0, disposal=2)

    before = opaque_per_frame(src)
    r = api.run(srcdir, output='new', dest=outdir, name='keep',
                rotate='cw90', workers=1)
    if not r['outputs']:
        check('产物还在', False, str(r['failed'])[:120])
        return
    after = opaque_per_frame(r['outputs'][0])
    check('不透明前景没被当成透明吃掉', after == before,
          f'每帧不透明像素 {before} -> {after}')


def r3_no_ghosting(work):
    """透明动图逐帧看，后一帧不能带上一帧的残影。

    源是"前景在移动、两帧不重叠"的透明 GIF。我们写出去的是整帧合成画面，
    画面里带透明像素 —— 播放时如果不清画布，透明处就会透出上一帧。
    只断言"还有透明像素"是看不出这个的。
    """
    d = os.path.join(work, 'ghost')
    srcdir, outdir = os.path.join(d, 'source'), os.path.join(d, 'export')
    os.makedirs(srcdir)
    src = os.path.join(srcdir, 'move.gif')
    pal = [0, 0, 0, 255, 0, 0, 0, 255, 0] + [0] * (768 - 9)
    frames = []
    for box in ((1, 1, 6, 4), (6, 4, 11, 7)):   # 前景换位置，两帧不重叠
        fr = Image.new('P', (12, 8), 0)
        fr.putpalette(pal)
        fr.paste(1, box)
        frames.append(fr)
    frames[0].save(src, save_all=True, append_images=frames[1:],
                   duration=[80, 90], loop=0, transparency=0, disposal=2)

    before = opaque_per_frame(src)
    r = api.run(srcdir, output='new', dest=outdir, name='keep',
                rotate='cw90', workers=1)
    if not r['outputs']:
        check('产物还在', False, str(r['failed'])[:120])
        return
    after = opaque_per_frame(r['outputs'][0])
    check('每帧不透明像素数都和源一致（没有残影）', after == before,
          f'源 {before} -> 产物 {after}')


def r5_prescan_counts_carried(work, app):
    """预扫描要把"随带的非图片文件"也算进去。

    我把预扫描改成"只读扫描需要的字段"的时候，漏了 `copy_others` 和
    `dropped_roots` —— `scan_files` 这两个都读（tasks.py 第 79、110 行）。
    漏掉 copy_others 等于强制按 False 算，随带文件整个不计数。
    """
    from imgwb.tasks import scan_files

    d = os.path.join(work, 'prescan_carry')
    os.makedirs(d)
    jpg(os.path.join(d, 'a.jpg'))
    with open(os.path.join(d, 'note.txt'), 'w', encoding='utf-8') as fh:
        fh.write('hello')

    try:
        app.copy_others.set(True)       # 不支持的文件也带走
        snap = app._scan_only_settings()
        files = scan_files(d, snap, None)
        check('预扫描把随带的非图片文件也数进去了', len(files) == 2,
              f'数到 {len(files)} 个，应为 2（a.jpg + note.txt）')

        # 多条源路径也要认（scan_files 读 dropped_roots）
        d2 = os.path.join(work, 'prescan_root2')
        os.makedirs(d2)
        jpg(os.path.join(d2, 'b.jpg'))
        app.dropped_roots = [d, d2]
        snap2 = app._scan_only_settings()
        files2 = scan_files(d, snap2, None)
        check('预扫描认得多条源路径', len(files2) == 3,
              f'数到 {len(files2)} 个，应为 3')
        app.dropped_roots = []

        # 顺带确认原来的问题没回来：起始编号清空时预扫描不能炸
        app.src_folder.set(d)
        app.root.update()
        app.root.globalsetvar(str(app.start_num), '')
        try:
            app._prescan()
            crashed = False
        except Exception as exc:
            crashed = repr(exc)
        check('起始编号清空时预扫描仍然不炸',
              crashed is False, str(crashed))
    finally:
        # 起始编号改回能用的值，别影响后面的用例
        try:
            app.start_num.set(1)
        except Exception:
            pass




# ================================================================
# 2026-10-04 再次追加。
# 这一批的共同点：**前两轮都漏了**，因为前两轮只在"正常路径"和"已知故障点"
# 上验，没往"容错分支自己失败"和"输出目录和来源重叠"这两类里钻。
# ================================================================
def n1_fallback_copy_failure(work):
    """体积回退时复制失败，不能把残缺文件当成功交上去。

    回退逻辑是「产物没压小 → 删掉产物、把原件复制回来」。原来这两步裹在
    同一个 `try` 里、尾巴是 `except OSError: pass` —— 复制要是**在截断目标
    之后**失败，残缺文件留在原地，错误被吞掉，函数照样返回 success=True。
    提交器只看"暂存文件在不在"，于是把残缺产物转正、把原件备份删掉。

    **新加的备份保护拦不住一个被错标为成功的产物。**
    """
    import shutil as _sh
    from imgwb import pipeline as _pl

    d = os.path.join(work, 'fallback_fail')
    os.makedirs(d)
    src = os.path.join(d, 'a.png')
    Image.new('RGB', (12, 8), 'red').save(src)
    with open(src, 'rb') as fh:
        original = fh.read()

    real_copy = _pl.shutil.copy2
    state = {'hit': 0}

    def flaky_copy(a, b, *args, **kw):
        # 只破坏"回退复制"这一次：先把目标截断，再抛错 ——
        # 模拟复制写到一半磁盘满/被占用
        if state['hit'] == 0 and os.path.dirname(b) == d:
            state['hit'] = 1
            open(b, 'wb').close()           # 目标被截成 0 字节
            raise OSError(28, '假装磁盘满了')
        return real_copy(a, b, *args, **kw)

    _pl.shutil.copy2 = flaky_copy
    try:
        r = api.run(d, name='number', start=1, digits=1, quality='85',
                    compress_only=True, safe_write=True, workers=1)
    finally:
        _pl.shutil.copy2 = real_copy

    check('回退复制失败时确实触发了（夹具有效）', state['hit'] == 1,
          '没触发就说明这个用例没测到东西')
    if state['hit'] != 1:
        return

    check('复制失败不能报成功', not r['ok'] or bool(r['failed']),
          f'ok={r["ok"]}, failed={r["failed"]}')

    # 最关键的一条：原始字节必须还找得回来
    survivors = []
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            with open(p, 'rb') as fh:
                if fh.read() == original:
                    survivors.append(name)
    check('原始字节还在（没被残缺产物顶替）', bool(survivors),
          '目录里现在是: ' + str(sorted(os.listdir(d))))

    zero = [n for n in os.listdir(d)
            if os.path.isfile(os.path.join(d, n))
            and os.path.getsize(os.path.join(d, n)) == 0]
    check('没有留下 0 字节的残缺产物', not zero, str(zero))


def n2_export_must_not_land_in_source(work):
    """导出目录算出来等于来源目录时，必须拦住。

    来源 `parent/source`、导出填 `parent`、保留结构、后缀留空 ——
    入口只比了顶层的 `dest` 和 `src_root`（不嵌套，放行），
    可最终算出来的 `final_dir` 正是来源本身。产物写回来源，
    下一轮扫描又把它们当新图，**跑四次文件数 2→4→8→16**。

    前面修过同一个目录关系在"清空"那一侧，这是"写入"这一侧。
    """
    d = os.path.join(work, 'export_into_source')
    srcdir = os.path.join(d, 'source')
    os.makedirs(srcdir)
    jpg(os.path.join(srcdir, 'a.jpg'))

    raised, r = None, None
    try:
        r = api.run(srcdir, output='new', dest=d, keep_structure=True,
                    dir_suffix='', name='keep', workers=1)
    except Exception as exc:
        raised = exc

    n = len([f for f in os.listdir(srcdir)
             if os.path.isfile(os.path.join(srcdir, f))])
    check('产物没有落回来源目录', n == 1, f'来源里现在有 {n} 个文件')
    check('这种导出配置被明确拒绝（而不是静悄悄照做）',
          raised is not None or not (r and r.get('ok')),
          f'raised={type(raised).__name__ if raised else None}, '
          f'ok={r and r.get("ok")}')


def n3_local_palette_colors(work):
    """各帧有**自己调色板**的 GIF，颜色不能丢。

    我们原来只取首帧的调色板当"母版"，把所有帧都映射回去。可 GIF 允许每帧
    带自己的局部调色板，后面帧的颜色未必在首帧表里 —— 第 2 帧的蓝色就被映射
    成了首帧表里的黑色。源两帧红、蓝，产物成了红、**黑**。

    现有的透明度断言和"数不透明像素"都拦不住这个：红变黑，像素数一个不差。
    """
    d = os.path.join(work, 'local_palette')
    srcdir, outdir = os.path.join(d, 'src'), os.path.join(d, 'out')
    os.makedirs(srcdir)
    p = os.path.join(srcdir, 'local.gif')
    f1 = Image.new('P', (8, 6), 0)
    f1.putpalette([255, 0, 0] + [0] * 765)      # 只有红
    f2 = Image.new('P', (8, 6), 0)
    f2.putpalette([0, 0, 255] + [0] * 765)      # 只有蓝
    f1.save(p, save_all=True, append_images=[f2], duration=[80, 110], loop=0)

    def colors(path):
        got = []
        with Image.open(path) as im:
            for i in range(getattr(im, 'n_frames', 1)):
                im.seek(i)
                got.append(im.convert('RGBA').getpixel((4, 3))[:3])
        return got

    before = colors(p)
    r = api.run(srcdir, output='new', dest=outdir, name='keep',
                rotate='cw90', workers=1)
    if not r['outputs']:
        check('产物还在', False, str(r['failed'])[:120])
        return
    after = colors(r['outputs'][0])
    check('每帧颜色都对（局部调色板不能丢色）', after == before,
          f'源 {before} -> 产物 {after}')


def n4_thread_exception_is_logged(work):
    """后台线程里没人接住的异常，必须进日志。

    `threading.excepthook` 给的是 `args.thread`，不是 `args.thread_name`。
    钩子里写错了属性名 → 先抛 AttributeError → 又被钩子自己的
    `except Exception: pass` 吞掉。结果：**原异常既没进日志，也没交回默认
    处理器**，等于凭空消失。

    这条和界面回调异常是同一类问题（打包版没有 stderr，不进日志就等于没有）。
    """
    import threading
    from imgwb import logfile

    logfile.setup()
    logfile.install_hooks()
    path = logfile._path
    check('日志文件就位', bool(path and os.path.isfile(path)), str(path))
    if not path or not os.path.isfile(path):
        return
    before = os.path.getsize(path)
    mark = 'N4_UNIQUE_THREAD_MARKER'

    def boom():
        raise RuntimeError(mark)

    # **这一刻要临时关掉严格模式。** 严格模式下线程钩子会往 stdout 打一行
    # `FAIL`，好让 `run_all.py` 把"意外的线程异常"判成失败。
    # 可这个用例是**故意**抛的 —— 不关掉的话，整个 test_datasafe 会因为自己
    # 的检验动作被判失败。关掉的只是给测试跑批看的那个信号，
    # 写日志的主逻辑照跑，断言验的还是它。
    strict = os.environ.pop('IMGWB_TK_STRICT', None)
    try:
        t = threading.Thread(target=boom)
        t.start()
        t.join()
    finally:
        if strict is not None:
            os.environ['IMGWB_TK_STRICT'] = strict

    with open(path, encoding='utf-8', errors='replace') as fh:
        fh.seek(before)
        added = fh.read()
    check('线程未捕获异常写进了日志', mark in added,
          f'日志只新增了 {len(added)} 字符')
    check('而且带着完整调用栈', 'Traceback' in added,
          added[:100].replace('\n', ' '))


def q2_file_list_must_not_land_in_source(work):
    """导出落回来源那个场景，把目录输入换成**文件列表**，原来照样写回来源。

    入口把文件路径当成目录根去推算输出目录（`dest\\a.png`），而任务实际
    写进 `dest\\<文件所在文件夹名><后缀>` —— 后缀留空就是来源目录本身。
    CLI 的 `--files` 走的就是这条分支。
    """
    d = os.path.join(work, 'filelist_into_source')
    srcdir = os.path.join(d, 'source')
    os.makedirs(os.path.join(srcdir, 'sub'))
    a = os.path.join(srcdir, 'a.jpg')
    b = os.path.join(srcdir, 'sub', 'b.jpg')
    jpg(a)
    jpg(b)

    def snapshot():
        return sorted(os.path.relpath(os.path.join(r, f), d)
                      for r, _d, fs in os.walk(d) for f in fs)

    # 单个文件、多个文件（第二个在子目录里）—— 都不许写回来源
    for label, files in (('单个文件', [a]), ('两个文件', [a, b])):
        before = snapshot()
        raised, r = None, None
        try:
            r = api.run(files, output='new', dest=d, keep_structure=True,
                        dir_suffix='', name='keep', workers=1)
        except api.OptionError as exc:
            raised = exc
        check(f'{label}：来源目录里没有多出文件', snapshot() == before,
              f'{before} -> {snapshot()}')
        check(f'{label}：被明确拒绝', raised is not None,
              f'ok={r and r.get("ok")}')

    # 正对照：后缀不留空时文件列表输入是正当用法，不能被一起拦掉
    r = api.run([a], output='new', dest=d, keep_structure=True,
                dir_suffix='_out', name='keep', workers=1)
    out = os.path.join(d, 'source_out', 'a.jpg')
    check('正常的文件列表导出照常工作', r.get('ok') and os.path.isfile(out),
          f'ok={r.get("ok")} outputs={r.get("outputs")}')


def q3_drive_root_source_is_protected(work):
    """来源是**盘符根目录**时，三道保护原来全部失效。

    `D:\\` 规范化之后自己带着结尾的分隔符，`startswith(root + os.sep)` 拼出
    `D:\\\\`，任何路径都匹配不上。只调路径函数，不扫盘、不写文件。
    """
    from imgwb import tasks
    from imgwb.utils import path_inside

    drive = os.path.splitdrive(os.path.abspath(work))[0] + os.sep   # 'D:\\'
    child = os.path.join(drive, '__imgwb_q3_probe__', 'out')
    check('导出到盘根来源里面 -> 拒绝',
          bool(tasks.output_escape_error(drive, child, True)))
    check('算出来的输出目录落在盘根来源里 -> 拒绝',
          bool(tasks.output_lands_in_source([child], [drive])))
    check('清空时盘根来源里面的东西受保护',
          tasks.is_protected(child, tasks.protected_paths([drive])))
    # 反方向：受保护的是深处的来源，要删的是盘根（装着来源）
    check('盘根装着来源 -> 也不能删',
          tasks.is_protected(drive, tasks.protected_paths([child])))
    # 对照：别的盘、名字只是前缀相同的兄弟目录，都不算"在里面"
    norm = os.path.normcase
    check('名字前缀相同的兄弟目录不算在里面',
          not path_inside(norm(os.path.join(drive, 'photos2')),
                          norm(os.path.join(drive, 'photos'))))
    check('别的盘不算在里面',
          not path_inside(norm('Q:\\x'), norm('P:\\')))
    check('UNC 共享根同样认得',
          path_inside(norm('\\\\srv\\share\\a\\b'), norm('\\\\srv\\share\\')))
    # 盘根作来源时，推算的输出目录要和 build_tasks 实际拼的一致
    got = tasks.output_dirs_for(drive, 'Q:\\out', True, True, '_已处理')
    check('盘根来源的输出目录和 build_tasks 的拼法一致',
          got == [os.path.join('Q:\\out', '_已处理')], str(got))


def q8_late_restore_failure_names_backup(work):
    """晚期拒绝时备份放不回原位，错误消息必须说出备份在哪。

    `1→2`、`2→3`：1 已经改名成备份，轮到 2 时挪不动（被占用）。于是 `1→2`
    也得撤（2 还在原地、没有产物，不能盖），要把 1 的备份放回去 —— 这一步
    再失败的话，原来是 `except OSError: pass`：原位空了、字节在备份名下，
    消息里却只有一句"已跳过"。
    """
    from imgwb import tasks

    d = os.path.join(work, 'late_restore')
    os.makedirs(d)
    one, two, three = (os.path.join(d, n) for n in ('1.txt', '2.txt', '3.txt'))
    for path, body in ((one, b'ONE'), (two, b'TWO')):
        with open(path, 'wb') as fh:
            fh.write(body)
    t1, t2 = os.path.join(d, 't1.tmp'), os.path.join(d, 't2.tmp')
    for path in (t1, t2):
        with open(path, 'wb') as fh:
            fh.write(b'NEW')
    staged = [TaskResult(True, one, two, tmp_path=t1),
              TaskResult(True, two, three, tmp_path=t2)]

    real = os.replace
    hits = {'backup': 0, 'restore': 0}

    def flaky(src, dst, *a, **k):
        s, t = os.path.basename(src), os.path.basename(dst)
        if s == '2.txt':
            hits['backup'] += 1
            raise PermissionError(5, '注入：挪不开', src)
        if tasks.BACKUP_MARK in s and t == '1.txt':
            hits['restore'] += 1
            raise PermissionError(5, '注入：放不回去', src)
        return real(src, dst, *a, **k)

    tasks.os.replace = flaky
    try:
        errs = commit_staged(staged, False)
    finally:
        tasks.os.replace = real

    # 先确认两处故障都真的注入到了，否则这条测的不是晚期恢复那条分支
    check('夹具成立（备份失败和恢复失败都触发了）',
          hits['backup'] >= 1 and hits['restore'] >= 1, str(hits))
    baks = [n for n in os.listdir(d) if tasks.BACKUP_MARK in n]
    kept = (len(baks) == 1
            and open(os.path.join(d, baks[0]), 'rb').read() == b'ONE')
    check('原始字节还在（躺在备份名下）', kept, str(baks))
    check('2.txt 没被盖掉', open(two, 'rb').read() == b'TWO')
    told = [reason for path, reason, _k in errs
            if path == one and baks and os.path.join(d, baks[0]) in reason]
    check('**错误消息给出了备份的完整路径**', bool(told),
          str([e[1] for e in errs])[:200])

    # 对照：恢复成功时不该多出"保留在"那半句，原文件就在原位
    for path in (one, two, t1, t2) + tuple(os.path.join(d, n) for n in baks):
        if os.path.exists(path):
            os.remove(path)
    for path, body in ((one, b'ONE'), (two, b'TWO'), (t1, b'NEW'), (t2, b'NEW')):
        with open(path, 'wb') as fh:
            fh.write(body)

    def only_backup_fails(src, dst, *a, **k):
        if os.path.basename(src) == '2.txt':
            raise PermissionError(5, '注入：挪不开', src)
        return real(src, dst, *a, **k)

    tasks.os.replace = only_backup_fails
    try:
        errs = commit_staged(
            [TaskResult(True, one, two, tmp_path=t1),
             TaskResult(True, two, three, tmp_path=t2)], False)
    finally:
        tasks.os.replace = real
    check('对照：恢复成功时 1.txt 回到原位、不留备份',
          open(one, 'rb').read() == b'ONE'
          and not [n for n in os.listdir(d) if tasks.BACKUP_MARK in n],
          str(sorted(os.listdir(d))))
    check('对照：这时消息里不提备份',
          not any('保留在' in reason for _p, reason, _k in errs),
          str([e[1] for e in errs])[:200])


def _photo(path, size=(64, 48)):
    """一张有内容的小图（纯色图压缩后几百字节，体积关系会失真）。"""
    im = Image.new('RGB', size)
    im.putdata([((x * 4) % 256, (y * 5) % 256, (x ^ y) % 256)
                for y in range(size[1]) for x in range(size[0])])
    im.save(path, quality=95)


def f1_case_only_rename_keeps_the_file(work):
    """原地把 `img_001.jpg` 改成 `IMG_001.jpg`（只差大小写）。

    Windows 目录不分大小写，这两个名字是**同一个文件**。关掉安全写入时
    产物直接写在这个文件里；提交器原来只比字符串，把它当成"要让位的旧文件"
    挪成备份、再按成功删掉 —— 目录变空，返回值却是成功。

    六种组合都验：安全写入开/关 × 只改名/压缩/旋转。**成功的标准是磁盘上
    真有一个叫新名字的、能解码的、内容对的文件**，不是返回值里的 ok。
    """
    for safe in (False, True):
        for label, kw, size in (
                ('只改名', {}, (64, 48)),
                ('压缩', dict(compress_only=True, quality='85'), (64, 48)),
                ('旋转', dict(rotate='cw90'), (48, 64))):
            d = os.path.join(work, f'case_{int(safe)}_{label}')
            os.makedirs(d)
            _photo(os.path.join(d, 'img_001.jpg'))
            tag = f'{"安全写入" if safe else "直写"} · {label}'
            try:
                r = api.run(d, output='inplace', name='prefix', prefix='IMG',
                            digits=3, start=1, safe_write=safe, workers=1, **kw)
            except Exception as exc:
                check(f'{tag}：没有抛异常', False, f'{type(exc).__name__}: {exc}')
                continue
            left = os.listdir(d)
            check(f'{tag}：报成功', r.get('ok') is True, str(r.get('failed'))[:160])
            # listdir 给的是磁盘上真实的大小写 —— 既验"文件还在"，也验"名字改了"
            check(f'{tag}：**目录里正好一个 IMG_001.jpg**', left == ['IMG_001.jpg'],
                  str(left))
            if left:
                try:
                    with Image.open(os.path.join(d, left[0])) as im:
                        im.load()
                        got = im.size
                except Exception as exc:
                    got = f'打不开: {type(exc).__name__}'
                check(f'{tag}：能解码，尺寸对', got == size, str(got))
            check(f'{tag}：没有留下备份或临时文件',
                  not [n for n in left if 'imgwb' in n], str(left))


def f2_inplace_direct_multiframe_keeps_all_frames(work):
    """关掉安全写入、原地旋转多帧图 —— 不能一边读源一边往它上面写。

    帧是以生成器交给编码器的：编码器先打开输出（截断），再回头要后面的帧，
    而那些帧还得从这个已经被截断的文件里读。4 帧 GIF 只剩 1 帧还报成功，
    多页 TIFF 报错且原文件已经打不开。

    **不只数帧数**：每一帧的画面要和"把源帧旋转之后"对得上，时长也要对。
    四帧故意颜色各不相同、各带一个位置不同的白块，不会被编码器合并。
    """
    def make(path, ext):
        fr = []
        for k, c in enumerate(('red', 'green', 'blue', 'yellow')):
            f = Image.new('RGB', (40, 30), c)
            f.paste('white', (k * 8, 2, k * 8 + 6, 8))
            fr.append(f)
        kw = {} if ext == '.tif' else dict(duration=[80, 90, 100, 110], loop=0)
        if ext == '.webp':
            kw['lossless'] = True
        fr[0].save(path, save_all=True, append_images=fr[1:], **kw)

    def read(path, rotate=False):
        from PIL import ImageSequence
        out = []
        with Image.open(path) as im:
            for f in ImageSequence.Iterator(im):
                rgb = f.convert('RGB')
                if rotate:
                    rgb = rgb.transpose(Image.Transpose.ROTATE_270)   # 顺时针 90°
                out.append((rgb.copy(), f.info.get('duration')))
        return out

    def close(a, b):
        from PIL import ImageChops, ImageStat
        return a.size == b.size and sum(
            ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3 < 6

    for ext in ('.gif', '.tif', '.png', '.webp'):
        for name_kw, final in ((dict(name='keep'), 'anim' + ext),
                               (dict(name='prefix', prefix='ANIM', digits=1),
                                None)):
            renaming = final is None
            d = os.path.join(work, f'direct_anim_{ext[1:]}_{int(renaming)}')
            os.makedirs(d)
            src_name = 'anim_1' + ext if renaming else 'anim' + ext
            final = 'ANIM_1' + ext if renaming else final
            make(os.path.join(d, src_name), ext)
            want = read(os.path.join(d, src_name), rotate=True)
            tag = f'{ext}' + ('（同时只改大小写）' if renaming else '')
            try:
                r = api.run(d, output='inplace', rotate='cw90',
                            safe_write=False, workers=1, **name_kw)
                ok = r.get('ok')
            except Exception as exc:
                ok = f'{type(exc).__name__}: {exc}'
            left = os.listdir(d)
            check(f'{tag}：报成功，目录里正好是 {final}',
                  ok is True and left == [final], f'ok={ok} {left}')
            try:
                got = read(os.path.join(d, left[0])) if left else []
            except Exception as exc:
                check(f'{tag}：产物能打开', False, type(exc).__name__)
                continue
            check(f'{tag}：**帧数没少**', len(got) == len(want) == 4,
                  f'{len(want)} -> {len(got)}')
            check(f'{tag}：每一帧的画面都是源帧旋转后的样子',
                  len(got) == len(want)
                  and all(close(g[0], w[0]) for g, w in zip(got, want)))
            if ext != '.tif':
                check(f'{tag}：每帧时长不变',
                      [g[1] for g in got] == [w[1] for w in want],
                      f'{[w[1] for w in want]} -> {[g[1] for g in got]}')


def f3_output_dirs_differing_only_in_case(work):
    """两个来源文件夹只差大小写（`photos` / `PHOTOS`，在不同的上级目录下），
    导出到同一个位置 —— 在 Windows 上它们的输出目录是**同一个文件夹**。

    原来名字表按目录字符串分，两边各分到 `a.png`，提交时后一个盖掉前一个：
    报"完成 2 张"，实际只有 1 张。安全写入开着也一样，编号模式同样会撞。

    走真实的编排 → 处理 → 提交，最后看磁盘上**每个来源的内容都找得到**。
    """
    import hashlib
    from imgwb import tasks
    from imgwb.pipeline import process_single_image

    class V:
        def __init__(self, v):
            self.v = v

        def get(self):
            return self.v

    for mode in ('keep', 'number', 'prefix'):
        d = os.path.join(work, f'case_dirs_{mode}')
        a = os.path.join(d, 'one', 'photos')
        b = os.path.join(d, 'two', 'PHOTOS')
        os.makedirs(a)
        os.makedirs(b)
        Image.new('RGB', (8, 8), 'red').save(os.path.join(a, 'a.png'))
        Image.new('RGB', (8, 8), 'blue').save(os.path.join(b, 'a.png'))
        cfg = type('C', (), {})()
        cfg.dropped_paths = []
        cfg.dropped_roots = [a, b]
        for k, v in dict(scan_subdirs=True, copy_others=False, start_num=1,
                         digit_val='3位', prefix_val='IMG', name_mode=mode,
                         out_mode='new', keep_structure=True,
                         dir_suffix_val='_done', ico_mode_val='256',
                         ico_format_val='自动 (推荐)', rotate_val='不旋转',
                         quality_val='通用高清 (95%)', compress_only=False,
                         is_ico=False, safe_write=True).items():
            setattr(cfg, k, V(v))
        files = tasks.scan_files(a, cfg)
        ts = tasks.build_tasks(files, a, os.path.join(d, 'out'), cfg)
        slots = {os.path.normcase(os.path.realpath(t.dst)) for t in ts}
        check(f'{mode}：两个任务分到两个不同的输出文件',
              len(ts) == 2 and len(slots) == 2,
              str([os.path.relpath(t.dst, d) for t in ts]))
        staged = [process_single_image(t) for t in ts]
        errs = commit_staged([r for r in staged if r.success], True)
        check(f'{mode}：处理和提交都没报错',
              all(r.success for r in staged) and not errs, str(errs)[:160])

        def color(p):
            with Image.open(p) as im:
                return im.convert('RGB').getpixel((4, 4))

        out_files = [os.path.join(r, f)
                     for r, _d, fs in os.walk(os.path.join(d, 'out')) for f in fs]
        got = sorted(color(p) for p in out_files)
        check(f'{mode}：**红、蓝两张都在输出里**',
              got == [(0, 0, 255), (255, 0, 0)],
              f'{len(out_files)} 个文件，颜色 {got}')

    # 对照：两个来源名字完全相同（都叫 photos）时本来就共用名字表，不能被改坏
    d = os.path.join(work, 'case_dirs_same')
    a = os.path.join(d, 'one', 'photos')
    b = os.path.join(d, 'two', 'photos')
    os.makedirs(a)
    os.makedirs(b)
    Image.new('RGB', (8, 8), 'red').save(os.path.join(a, 'a.png'))
    Image.new('RGB', (8, 8), 'blue').save(os.path.join(b, 'a.png'))
    cfg.dropped_roots = [a, b]
    cfg.name_mode = V('keep')
    ts = tasks.build_tasks(tasks.scan_files(a, cfg), a, os.path.join(d, 'out'), cfg)
    check('对照：同名来源仍然各得一个名字',
          sorted(os.path.basename(t.dst) for t in ts) == ['a.png', 'a_1.png'],
          str([os.path.basename(t.dst) for t in ts]))


def direct_write_failure_keeps_original(work):
    """关掉安全写入、原地同名处理时，**写到一半出故障，原文件必须完好**。

    直写原来是真的往目标上写。目标就是源文件时，编码器一打开文件原图就被
    截断了 —— 之后磁盘满、编码器抛错，留下一个损坏的原文件，没有任何备份。
    现在直写也先写旁路文件、写完整了再一步换到位。

    故障注入在"已经写出一部分字节之后"：先往目标路径写半截垃圾再抛错，
    模拟的正是写到一半磁盘满。只注入这一次保存，别的都是真的。
    """
    import hashlib
    from imgwb import pipeline

    def sha(p):
        with open(p, 'rb') as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def gif4(p):
        fr = [Image.new('RGB', (40, 30), c)
              for c in ('red', 'green', 'blue', 'yellow')]
        fr[0].save(p, save_all=True, append_images=fr[1:],
                   duration=[80, 90, 100, 110], loop=0)

    real_save = Image.Image.save
    real_replace = pipeline.os.replace

    def half_then_fail(self, fp, *a, **k):
        if isinstance(fp, str):
            with open(fp, 'wb') as fh:
                fh.write(b'half written')
        raise OSError(28, '假装磁盘满了')

    def replace_denied(a, b, *x, **k):
        raise PermissionError(5, '假装目标被别的程序占着', b)

    for label, name, make, kw, fault in (
            ('JPEG 压缩，保存中途磁盘满', 'a.jpg', _photo,
             dict(compress_only=True, quality='85'), 'save'),
            ('JPEG 旋转，保存中途磁盘满', 'a.jpg', _photo,
             dict(rotate='cw90'), 'save'),
            ('GIF 旋转，保存中途磁盘满', 'a.gif', gif4,
             dict(rotate='cw90'), 'save'),
            ('JPEG 旋转，写完了但换不上去', 'a.jpg', _photo,
             dict(rotate='cw90'), 'replace')):
        d = os.path.join(work, f'direct_fail_{len(os.listdir(work))}')
        os.makedirs(d)
        p = os.path.join(d, name)
        make(p)
        before = sha(p)
        if fault == 'save':
            Image.Image.save = half_then_fail
        else:
            pipeline.os.replace = replace_denied
        try:
            r = api.run(d, output='inplace', name='keep', safe_write=False,
                        workers=1, **kw)
        except Exception as exc:
            r = {'ok': f'{type(exc).__name__}: {exc}'}
        finally:
            Image.Image.save = real_save
            pipeline.os.replace = real_replace
        check(f'直写故障 · {label}：如实报失败', r.get('ok') is False,
              f'ok={r.get("ok")}')
        left = os.listdir(d)
        check(f'直写故障 · {label}：**原文件一个字节没变**',
              left == [name] and sha(p) == before, str(left))

    # 对照：不注入故障时，同样的直写是真的会把文件改掉的（否则上面的
    # "没变"可能只是因为根本没处理）
    d = os.path.join(work, 'direct_ok')
    os.makedirs(d)
    p = os.path.join(d, 'a.jpg')
    _photo(p)
    before = sha(p)
    r = api.run(d, output='inplace', name='keep', safe_write=False, workers=1,
                rotate='cw90')
    with Image.open(p) as im:
        size = im.size
    check('直写对照：没有故障时确实处理了', r.get('ok') is True
          and sha(p) != before and size == (48, 64) and os.listdir(d) == ['a.jpg'],
          f'ok={r.get("ok")} {size} {os.listdir(d)}')

    # 直写同名压缩，压完反而更大 —— 原来兜不了（原字节已经被盖掉），
    # 现在源还完好，体积护栏同样生效：保留原件。
    d = os.path.join(work, 'direct_guard')
    os.makedirs(d)
    p = os.path.join(d, 'a.jpg')
    im = Image.new('RGB', (64, 48))
    im.putdata([((x * 4) % 256, (y * 5) % 256, (x ^ y) % 256)
                for y in range(48) for x in range(64)])
    im.save(p, quality=30)                  # 已经压得很狠，再按 95% 存必然变大
    before = sha(p)
    r = api.run(d, output='inplace', name='keep', safe_write=False, workers=1,
                compress_only=True, quality='95')
    check('直写体积护栏：压不小就原样保留原件',
          r.get('ok') is True and sha(p) == before and os.listdir(d) == ['a.jpg']
          and len(r.get('kept_original', [])) == 1,
          f'ok={r.get("ok")} kept={r.get("kept_original")} {os.listdir(d)}')


def inplace_results_are_flushed_before_replacing(work):
    """原地处理：新文件的数据要先刷到盘上，才能去顶替原文件。

    防的是断电：改名这个动作文件系统记了日志，新文件的**数据**却可能还在
    系统缓存里；在那个窗口里断电，重启后原文件的名字下面是一个内容全空的
    文件。**真断电没法在测试里做**，所以这里验的只是机制本身：刷盘确实
    发生了、发生在顶替之前、对象是那个将要顶替原文件的文件。
    它证明不了"断电一定不丢" —— 那取决于磁盘和驱动是否老实执行刷盘。
    """
    from imgwb import pipeline

    events = []
    real_flush, real_replace = pipeline.flush_to_disk, os.replace

    def flush(path):
        ok = real_flush(path)
        events.append(('flush', os.path.basename(path), ok))
        return ok

    def replace(a, b, *x, **k):
        events.append(('replace', os.path.basename(a), os.path.basename(b)))
        return real_replace(a, b, *x, **k)

    def run(label, **kw):
        d = os.path.join(work, f'flush_{label}')
        os.makedirs(d)
        _photo(os.path.join(d, 'a.jpg'))
        events.clear()
        pipeline.flush_to_disk, os.replace = flush, replace
        try:
            r = api.run(d, workers=1, rotate='cw90', **kw)
        finally:
            pipeline.flush_to_disk, os.replace = real_flush, real_replace
        return d, r, list(events)

    for label, kw, final in (
            ('直写同名', dict(output='inplace', name='keep', safe_write=False), 'a.jpg'),
            ('安全写入同名', dict(output='inplace', name='keep', safe_write=True), 'a.jpg'),
            ('安全写入改名', dict(output='inplace', name='prefix', prefix='N',
                            digits=1, safe_write=True), 'N_1.jpg')):
        d, r, ev = run(label, **kw)
        placed = [i for i, e in enumerate(ev) if e[0] == 'replace' and e[2] == final]
        check(f'刷盘 · {label}：处理成功，夹具成立（确实有一次顶替到 {final}）',
              r.get('ok') is True and len(placed) == 1 and os.listdir(d) == [final],
              str(ev))
        if len(placed) != 1:
            continue
        temp_name = ev[placed[0]][1]
        flushed = [i for i, e in enumerate(ev)
                   if e[0] == 'flush' and e[1] == temp_name and e[2]]
        check(f'刷盘 · {label}：**顶替原文件之前，那个新文件已经刷到盘上**',
              bool(flushed) and flushed[0] < placed[0], str(ev))

    # 对照：导出到别的文件夹，原文件根本不动 —— 不该花这个钱
    d = os.path.join(work, 'flush_export')
    os.makedirs(d)
    _photo(os.path.join(d, 'a.jpg'))
    events.clear()
    pipeline.flush_to_disk, os.replace = flush, replace
    try:
        r = api.run(d, workers=1, rotate='cw90', output='new',
                    dest=os.path.join(work, 'flush_export_out'), name='keep')
    finally:
        pipeline.flush_to_disk, os.replace = real_flush, real_replace
    check('刷盘 · 对照：导出到别处时不刷（原文件不受威胁）',
          r.get('ok') is True and not [e for e in events if e[0] == 'flush'],
          str(events))


def leftovers_are_reported_in_gui(work, app):
    """界面上开工前要把残留说出来：备份文件（那是原文件）和判断不了主人的
    临时文件。说完**一个都不许动** —— 包括不能把它们当成输入扫进去处理。"""
    from imgwb import tasks
    from imgwb.utils import SCOPE_TAG

    d = os.path.join(work, 'gui_leftovers')
    os.makedirs(d)
    for i in range(3):
        _photo(os.path.join(d, f'pic_{i}.jpg'))
    other = 'abcd' if SCOPE_TAG != 'abcd' else 'dcba'
    unknown = os.path.join(d, f'x.~imgwb123456{other}11111111.jpg')
    backup = os.path.join(d, f'old.jpg{tasks.BACKUP_MARK}deadbeef')
    _photo(unknown)                     # 内容是一张真图：不拦就会被当图片处理
    with open(backup, 'wb') as fh:
        fh.write(b'ORIGINAL BYTES')

    app.dropped_paths, app.dropped_roots = [], []
    app.src_folder.set(d)
    app.out_mode.set('original')
    app.name_mode.set('prefix')
    app.prefix_val.set('KEPT')
    app.start_num.set(1)
    app.compress_only.set(False)
    app.rotate_val.set('不旋转')
    app.safe_write.set(True)
    app.copy_others.set(True)           # 开着"带上不支持的文件"也不许带走它们
    lines = []
    real_log = app.log

    def log(msg, *a, **k):
        lines.append(str(msg))
        return real_log(msg, *a, **k)

    app.log = log
    deadline = time.time() + 60

    def poll():
        if (not app.is_running and lines) or time.time() > deadline:
            app.root.after(300, app.root.quit)
            return
        app.root.after(50, poll)

    try:
        app.start_processing()
        app.root.after(50, poll)
        app.root.mainloop()
    finally:
        del app.log

    text = '\n'.join(lines)
    left = sorted(os.listdir(d))
    check('界面残留：说了有备份文件，并且说了那是原文件',
          '备份文件' in text and '原文件' in text and os.path.basename(backup) in text,
          text[-300:])
    check('界面残留：说了有来历不明的临时文件，并且说了不会自动删',
          '来历不明' in text and '不会自动删除' in text, text[-300:])
    check('界面残留：**备份和那个临时文件原样还在**',
          os.path.exists(unknown) and open(backup, 'rb').read() == b'ORIGINAL BYTES',
          str(left))
    check('界面残留：三张图照常改名，没把残留当输入',
          [n for n in left if n.startswith('KEPT_')]
          == ['KEPT_001.jpg', 'KEPT_002.jpg', 'KEPT_003.jpg']
          and len(left) == 5 and not app.error_list, f'{left} {app.error_list[:2]}')


def close_during_real_commit(work, app):
    """关窗口时工作线程**真的**正在提交 —— 跑真实的工作线程和真实的关窗流程。

    上面那个 `close_during_commit_waits` 是用桩摆出"正在提交"的状态、直接调
    `_await_worker_exit`，它验的是那个函数的分支，**验不了"工作线程真的会在
    提交期间把标志立起来、关窗流程真的会等它"**。这一条补的就是这个：
    真的开一轮原地改名，把提交拖慢到超过关窗的 5 秒期限，提交进行到一半时
    调真实的 `on_close()`。

    只换了两样：提交函数外面包一层 sleep（把"慢盘"变成确定的时序），
    `root.destroy` 换成记账后退出主循环（这个窗口后面还要用）。
    """
    from imgwb import app as app_mod

    d = os.path.join(work, 'real_close')
    os.makedirs(d)
    for i in range(6):
        _photo(os.path.join(d, f'pic_{i}.jpg'))

    seen = {}
    real_commit = app_mod.commit_staged
    real_sweep = app_mod.sweep_temps

    def slow_commit(staged, is_export, protect=()):
        seen['commit_started'] = time.time()
        seen['staged'] = [r.tmp_path for r in staged if r.tmp_path]
        time.sleep(6.5)                     # 比关窗的 5 秒期限长
        out = real_commit(staged, is_export, protect=protect)
        seen['commit_done'] = time.time()
        return out

    def counting_sweep(dirs, exclude=None):
        # 只盯**提交进行期间**的清扫。开工前那一次不算 —— 那时这一轮还没有
        # 任何待提交的临时文件（每轮开工前会先清一遍以前的残留）。
        if 'commit_started' in seen:
            seen.setdefault('sweeps', []).append(
                'commit_done' in seen)      # 清扫发生时提交做完了没有
        return real_sweep(dirs, exclude)

    def fake_destroy():
        seen['destroy_at'] = time.time()
        seen['commit_done_at_destroy'] = 'commit_done' in seen
        app.root.quit()

    def watch():
        # 以"提交函数真的被调起来了"为准，不看产品内部的标志位 ——
        # 这样同一条用例拿到没有这项保护的旧代码上也跑得起来（并且会红）。
        if 'commit_started' in seen and 'closed_at' not in seen:
            seen['closed_at'] = time.time()
            app.on_close()                  # 真实的关窗流程
            return
        if 'destroy_at' in seen or time.time() > deadline:
            app.root.quit()
            return
        app.root.after(50, watch)

    app_mod.commit_staged = slow_commit
    app_mod.sweep_temps = counting_sweep
    app.root.destroy = fake_destroy
    deadline = time.time() + 60
    try:
        app.dropped_paths, app.dropped_roots = [], []
        app.src_folder.set(d)
        app.out_mode.set('original')
        app.name_mode.set('prefix')
        app.prefix_val.set('DONE')
        app.start_num.set(1)
        app.compress_only.set(False)
        app.rotate_val.set('不旋转')
        app.safe_write.set(True)
        app.start_processing()
        app.root.after(50, watch)
        app.root.mainloop()
        # 关窗流程里还排着的回调放完
        end = time.time() + 12
        while 'destroy_at' not in seen and time.time() < end:
            app.root.update()
            time.sleep(0.05)
    finally:
        app_mod.commit_staged = real_commit
        app_mod.sweep_temps = real_sweep
        del app.root.destroy

    check('真实关窗：夹具成立（提交期间触发了关窗，当时有待提交的临时文件）',
          'closed_at' in seen and len(seen.get('staged', [])) == 6
          and seen['closed_at'] < seen.get('commit_done', 0), str({
              k: v for k, v in seen.items() if k != 'staged'}))
    check('真实关窗：**等到提交做完才销毁窗口**（等了超过 5 秒期限）',
          seen.get('commit_done_at_destroy') is True
          and seen.get('destroy_at', 0) - seen.get('closed_at', 0) > 5,
          f'{seen.get("destroy_at", 0) - seen.get("closed_at", 0):.1f} 秒')
    check('真实关窗：提交没做完之前没有清扫过',
          all(seen.get('sweeps', [])), str(seen.get('sweeps')))
    left = sorted(os.listdir(d))
    check('真实关窗：6 张都改名到位，没有备份名、没有临时文件',
          len(left) == 6 and all(n.startswith('DONE_') for n in left),
          str(left))


def _junction(link, target):
    """建一个目录 junction（不需要管理员权限）。建不出来返回 False。"""
    import subprocess
    r = subprocess.run(['cmd', '/c', 'mklink', '/J', link, target],
                       capture_output=True)
    return r.returncode == 0 and os.path.isdir(link)


def junction_alias_cannot_reach_source(work):
    """导出路径是一个**指回来源的 junction** 时，照样要拦住。

    字符串上看导出位置和来源毫不相干，实际指向同一个地方。分两种深度：

    * 顶层：导出位置本身就是指向来源的 junction；
    * 深层：导出目录里**某个子文件夹**是指回来源子目录的 junction ——
      顶层推算的输出目录是干净的，只有按排好的任务逐个看落点才发现得了。
    """
    d = os.path.join(work, 'junction_alias')
    srcdir = os.path.join(d, 'source')
    os.makedirs(os.path.join(srcdir, 'sub'))
    jpg(os.path.join(srcdir, 'a.jpg'))
    jpg(os.path.join(srcdir, 'sub', 'b.jpg'))
    links = []

    def snapshot():
        return sorted(os.path.relpath(os.path.join(r, f), srcdir)
                      for r, _d, fs in os.walk(srcdir) for f in fs)

    before = snapshot()
    try:
        # 顶层：dest 是指向来源的 junction
        top = os.path.join(d, 'looks_unrelated')
        if not _junction(top, srcdir):
            check('junction 夹具能建出来', False, '这台机器上 mklink /J 失败')
            return
        links.append(top)
        raised = None
        try:
            api.run(srcdir, output='new', dest=top, name='keep', workers=1)
        except api.OptionError as exc:
            raised = exc
        check('顶层 junction：被拒绝', raised is not None)
        check('顶层 junction：来源里没有多出文件', snapshot() == before,
              str(snapshot()))

        # 深层：导出目录是正常的，但里面的 sub 是指回 来源\sub 的 junction
        out = os.path.join(d, 'out')
        os.makedirs(os.path.join(out, 'source_已处理'))
        deep = os.path.join(out, 'source_已处理', 'sub')
        if not _junction(deep, os.path.join(srcdir, 'sub')):
            check('深层 junction 夹具能建出来', False)
            return
        links.append(deep)
        raised = None
        try:
            api.run(srcdir, output='new', dest=out, name='keep', workers=1)
        except api.OptionError as exc:
            raised = exc
        check('深层 junction：被拒绝', raised is not None)
        check('深层 junction：来源里没有多出文件', snapshot() == before,
              str(snapshot()))

        # 对照：把那个 junction 拿掉，同样的导出就是正当用法
        os.rmdir(deep)
        links.remove(deep)
        r = api.run(srcdir, output='new', dest=out, name='keep', workers=1)
        check('对照：没有 junction 时同样的导出照常工作',
              r.get('ok') and os.path.isfile(os.path.join(deep, 'b.jpg'))
              and snapshot() == before, f'ok={r.get("ok")}')
    finally:
        # junction 要用 rmdir 摘掉（只删链接本身），别让后面的 rmtree 顺着它走
        for link in links:
            try:
                os.rmdir(link)
            except OSError:
                pass


def close_during_commit_waits(work, app):
    """关窗口时工作线程**正在提交**：不许清扫、不许销毁窗口，要等它做完。

    原来 `_await_worker_exit` 到了 5 秒期限就「清扫临时文件 + 销毁窗口」。
    提交阶段被这么打断的话：还没改名的产物被清扫删掉，进程一退、工作线程
    冻在半路，原地修改模式下让位的原文件就留在备份名下。

    这里不起真的工作线程（那样时序没法定死），直接摆出那一刻的状态，
    看 `_await_worker_exit` 怎么做。销毁窗口和清扫都换成记账的桩。
    """
    from imgwb import app as app_mod

    d = os.path.join(work, 'close_commit')
    os.makedirs(d)
    tmp = make_temp_path(os.path.join(d, 'a.jpg'))
    with open(tmp, 'wb') as fh:
        fh.write(b'STAGED')

    calls = {'destroy': 0, 'sweep': 0}
    real_sweep = app_mod.sweep_temps
    pending = []

    def fake_sweep(dirs, exclude=None):
        calls['sweep'] += 1
        return real_sweep(dirs, exclude)

    saved = (app.is_running, app._committing, app._output_dirs)
    app_mod.sweep_temps = fake_sweep
    app.root.destroy = lambda: calls.__setitem__('destroy', calls['destroy'] + 1)
    app.root.after = lambda ms, fn=None, *a: pending.append(fn)
    try:
        app._output_dirs = {d}

        # 对照先行：没在提交、期限已过 —— 这时就该清扫并退出（原有行为）。
        # 这一步同时证明桩是接上的：下面"没调用"不是因为桩没生效。
        app.is_running, app._committing = True, False
        app._await_worker_exit(time.time() - 1)
        check('对照：没在提交时，到期就清扫并关窗',
              calls == {'destroy': 1, 'sweep': 1}, str(calls))
        check('对照：那次清扫把临时文件删了', not os.path.exists(tmp))

        with open(tmp, 'wb') as fh:
            fh.write(b'STAGED')
        calls.update(destroy=0, sweep=0)
        pending.clear()

        # 正题：正在提交、期限同样已过
        app._committing = True
        app._await_worker_exit(time.time() - 1)
        check('**正在提交时不销毁窗口**', calls['destroy'] == 0, str(calls))
        check('**正在提交时不清扫**', calls['sweep'] == 0, str(calls))
        check('待提交的临时文件还在', os.path.exists(tmp))
        check('而是安排了稍后再看', len(pending) == 1, str(len(pending)))

        # 提交做完、工作线程收尾结束 —— 再轮到它时正常退出
        app._committing, app.is_running = False, False
        os.remove(tmp)                  # 提交完临时文件已经改名走了
        if pending:
            pending.pop()()
        check('提交做完之后正常关窗', calls['destroy'] == 1, str(calls))

        # 提交卡死（比如网络盘掉线）：到了宽限上限也得能退，但**不清扫**
        with open(tmp, 'wb') as fh:
            fh.write(b'STAGED')
        calls.update(destroy=0, sweep=0)
        pending.clear()
        app.is_running, app._committing = True, True
        app._await_worker_exit(time.time() - 1, commit_deadline=time.time() - 1)
        check('提交卡死超过宽限：仍然能关窗',
              calls['destroy'] == 1, str(calls))
        check('但不清扫 —— 临时文件留着好手工找回',
              calls['sweep'] == 0 and os.path.exists(tmp), str(calls))
    finally:
        app_mod.sweep_temps = real_sweep
        del app.root.destroy            # 摘掉实例上的桩，露出真正的方法
        del app.root.after
        app.is_running, app._committing, app._output_dirs = saved


# ================================================================
def main():
    work = tempfile.mkdtemp(prefix='datasafe-')
    try:
        print('\nB1 部分失败时，失败那张的原文件')
        b1_failed_source_not_overwritten(work)
        b1_missing_temp_protects_source(work)

        print('\nB2 提交阶段出故障时的可恢复性')
        b2_replace_failure_keeps_original(work)

        print('\nB4 最终路径不能逃出导出目录')
        b4_prefix_cannot_escape(work)

        # **一个进程里只建一个窗口。** ttkbootstrap 的样式是全局注册的，
        # 第一个根窗口销毁时会带走它们，再建第二个就是
        # `TclError: Layout info.Round.Toggle not found`。
        from imgwb import app as app_mod
        from imgwb.app import ImageProcessorApp

        for _n in ('showinfo', 'showwarning', 'showerror'):
            setattr(app_mod.messagebox, _n, lambda *a, **k: None)
        app_mod.messagebox.askokcancel = lambda *a, **k: True
        gui = ImageProcessorApp()

        print('\n清空导出目录不能删到来源')
        b3_clear_export_keeps_source(work, gui)

        print('\n动图：透明度、原地直写、缩放上报')
        b5_gif_keeps_transparency(work)
        b6_inplace_animated_same_file(work)
        b7_animated_resize_reported(work)

        print('\n连锁改名、空后缀、动图透明、预扫描（每条都不只验最简情形）')
        r1_chain_three_files(work)
        r1_chain_missing_temp(work)
        r2_empty_suffix_clear(work, gui)
        r3_no_ghosting(work)
        r4_opaque_color_survives(work)
        r5_prescan_counts_carried(work, gui)

        print('\n容错分支自己失败的场景')
        n1_fallback_copy_failure(work)
        n2_export_must_not_land_in_source(work)
        n3_local_palette_colors(work)
        n4_thread_exception_is_logged(work)

        print('\n文件列表输入、盘符根目录、备份放不回原位')
        q2_file_list_must_not_land_in_source(work)
        q3_drive_root_source_is_protected(work)
        q8_late_restore_failure_names_backup(work)

        print('\njunction 别名、关窗时正在提交')
        junction_alias_cannot_reach_source(work)
        close_during_commit_waits(work, gui)

        print('\n正常成功路径上的丢文件、丢帧、互相覆盖')
        f1_case_only_rename_keeps_the_file(work)
        f2_inplace_direct_multiframe_keeps_all_frames(work)
        f3_output_dirs_differing_only_in_case(work)

        print('\n直写模式：写到一半出故障，原文件不能坏')
        direct_write_failure_keeps_original(work)

        print('\n原地处理：顶替原文件之前先把数据刷到盘上')
        inplace_results_are_flushed_before_replacing(work)

        print('\n开工前把残留说出来，但不动它们')
        leftovers_are_reported_in_gui(work, gui)

        # 放在最后：它会走一遍真实的关窗流程
        print('\n关窗口时真的正在提交（真实工作线程 + 真实关窗流程）')
        close_during_real_commit(work, gui)
    finally:
        try:
            gui.shutdown_workers()
            gui.root.destroy()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)

    print('\n' + ('全部通过' if not fails
                  else f'失败 {len(fails)} 项: {fails}'))
    return 1 if fails else 0


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
