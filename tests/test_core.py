"""核心逻辑回归：图像处理流水线、命名编号、写入模式、路径处理。

用法:  python tests\test_core.py
依赖:  pillow、ttkbootstrap（不需要显示器，这些用例都不建窗口）
"""
import os
import shutil
import sys
import tempfile

from PIL import Image, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from imgwb.config import ProcessConfig
from imgwb.pipeline import get_quality_params, process_single_image
from imgwb.tasks import build_tasks, commit_staged, discard_staged
from imgwb.utils import long_path, natural_sort_key, parse_drop_paths

PSI, Cfg = process_single_image, ProcessConfig

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


class Var:
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


def settings(**over):
    base = dict(start_num=1, digit_val='3位 (001)', prefix_val='IMG',
                name_mode='number', out_mode='original', keep_structure=True,
                dir_suffix_val='_已处理', ico_mode_val='multi',
                ico_format_val='自动 (推荐)', rotate_val='不旋转',
                quality_val='通用高清 (95%)', compress_only=False, is_ico=False,
                safe_write=True, scan_subdirs=False, max_workers=4)
    base.update(over)
    s = type('S', (), {})()
    for k, v in base.items():
        s.__dict__[k] = Var(v)
    s.dropped_paths = []
    return s


def folder(names, size=(40, 30)):
    d = tempfile.mkdtemp()
    for i, n in enumerate(names):
        Image.new('RGB', size, (30 * i % 256, 90, 160)).save(os.path.join(d, n))
    return d


def process_all(tasks, cfg, is_export=False):
    staged = [PSI(t) for t in tasks]
    ok = [r for r in staged if r.success]
    commit_staged(ok, is_export)
    return staged


# ---------------------------------------------------------------- 命名与编号
print('== 编号重命名 ==')
d = folder([f'DSC_{i:04d}.jpg' for i in range(1, 6)])
cfg = settings()
files = sorted(os.path.join(d, n) for n in os.listdir(d))
process_all(build_tasks(files, d, d, cfg), cfg)
first = sorted(os.listdir(d))
check('第一次从 001 开始', first[0] == '001.jpg' and len(first) == 5, str(first))

files = sorted(os.path.join(d, n) for n in os.listdir(d))
process_all(build_tasks(files, d, d, cfg), cfg)
second = sorted(os.listdir(d))
check('再跑一次仍从 001 开始（不是 1001）', second == first, str(second))
shutil.rmtree(d, ignore_errors=True)

print('\n== 保留原名 ==')
d = folder(['cat.jpg', 'holiday.jpg'])
cfg = settings(name_mode='keep', rotate_val='顺时针 90°')
for _ in range(2):
    files = sorted(os.path.join(d, n) for n in os.listdir(d))
    process_all(build_tasks(files, d, d, cfg), cfg)
check('反复处理文件名不变', sorted(os.listdir(d)) == ['cat.jpg', 'holiday.jpg'],
      str(sorted(os.listdir(d))))
shutil.rmtree(d, ignore_errors=True)

print('\n== 不属于本批的同名文件要保住 ==')
d = folder(['a.jpg', 'b.jpg'])
Image.new('RGB', (10, 10), (1, 2, 3)).save(os.path.join(d, '001.jpg'))
keep = os.path.getsize(os.path.join(d, '001.jpg'))
cfg = settings()
files = sorted(os.path.join(d, n) for n in (['a.jpg', 'b.jpg']))
files = [os.path.join(d, n) for n in ('a.jpg', 'b.jpg')]
process_all(build_tasks(files, d, d, cfg), cfg)
check('已存在的 001.jpg 没被覆盖',
      os.path.getsize(os.path.join(d, '001.jpg')) == keep, str(sorted(os.listdir(d))))
shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- 写入模式
print('\n== 两种写入模式 ==')
for safe in (True, False):
    d = folder(['002.jpg', '003.jpg', '004.jpg'])
    import hashlib
    before = {n: hashlib.md5(open(os.path.join(d, n), 'rb').read()).hexdigest()
              for n in sorted(os.listdir(d))}
    cfg = settings(safe_write=safe)
    files = sorted(os.path.join(d, n) for n in os.listdir(d))
    tasks = build_tasks(files, d, d, cfg)
    staged_flags = [t[3] for t in tasks]
    process_all(tasks, cfg)
    after = {n: hashlib.md5(open(os.path.join(d, n), 'rb').read()).hexdigest()
             for n in sorted(os.listdir(d))}
    want = {'001.jpg': before['002.jpg'], '002.jpg': before['003.jpg'],
            '003.jpg': before['004.jpg']}
    check(f'safe_write={safe} 错位重命名内容正确', after == want, f'暂存标记 {staged_flags}')
    if not safe:
        check('直写模式只对会撞车的用暂存', staged_flags == [False, True, True],
              str(staged_flags))
    shutil.rmtree(d, ignore_errors=True)

print('\n== 中途取消要能完整撤回 ==')
d = folder([f'p{i}.jpg' for i in range(6)])
before = sorted(os.listdir(d))
cfg = settings(safe_write=True, rotate_val='顺时针 90°')
files = sorted(os.path.join(d, n) for n in os.listdir(d))
tasks = build_tasks(files, d, d, cfg)
staged = [PSI(t) for t in tasks[:3]]
discard_staged(staged)
check('撤回后目录原样', sorted(os.listdir(d)) == before, str(sorted(os.listdir(d))))
check('无残留临时文件', not [n for n in os.listdir(d) if '.~' in n])
shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- 图像流水线
print('\n== Exif 方向（源图 200x100 存储、Orientation=6、应显示 100x200）==')
tmp = tempfile.mkdtemp()
src = os.path.join(tmp, 'phone.jpg')
ex = Image.Exif()
ex[0x0112] = 6
ex[0x0100] = 274          # 恰好等于 0x0112，旧的字节扫描会被它劫持
Image.new('RGB', (200, 100), (10, 120, 200)).save(src, exif=ex)
for label, rot, comp, want in [('仅压缩+不旋转', '不旋转', True, (100, 200)),
                               ('顺时针 90°', '顺时针 90°', False, (200, 100)),
                               ('旋转 180°', '旋转 180°', False, (100, 200))]:
    dst = os.path.join(tmp, f'o{abs(hash(label))}.jpg')
    r = PSI((src, dst, Cfg(rot, '通用高清 (95%)', comp, False), True))
    os.replace(r.tmp_path, dst)
    with Image.open(dst) as im:
        shown = ImageOps.exif_transpose(im).size
        tag = im.getexif().get(0x0112)
    check(label, shown == want, f'Orientation={tag} 显示{shown} 应为{want}')

print('\n== ICO 尺寸上限 ==')
big = os.path.join(tmp, 'icon.png')
Image.new('RGBA', (1024, 1024), (200, 30, 30, 255)).save(big)
for mode in ('fixed_512', 'auto_512', 'fixed_256', 'multi', 'fixed_64'):
    dst = os.path.join(tmp, f'i_{mode}.ico')
    r = PSI((big, dst, Cfg('不旋转', '通用高清 (95%)', False, True, [mode], '自动 (推荐)'), True))
    if not r.success:
        check(mode, False, r.error_msg)
        continue
    os.replace(r.tmp_path, dst)
    try:
        with Image.open(dst) as im:
            sizes = sorted(im.ico.sizes())
        check(mode, bool(sizes) and max(s[0] for s in sizes) <= 256,
              f'{os.path.getsize(dst):,}B {sizes}')
    except Exception as exc:
        check(mode, False, f'打不开: {exc}')

print('\n== 多帧图像逐帧处理，动画要保住 ==')
frames = [Image.new('RGB', (40, 30), (i * 90, 60, 200 - i * 60)) for i in range(3)]
cases = []
p = os.path.join(tmp, 'anim.webp')
frames[0].save(p, format='WEBP', save_all=True, append_images=frames[1:],
               duration=[40, 90, 150], loop=3)
cases.append(('动态 WebP', p, 3))
p = os.path.join(tmp, 'multi.tif')
frames[0].save(p, format='TIFF', save_all=True, append_images=frames[1:])
cases.append(('多页 TIFF', p, 3))
p = os.path.join(tmp, 'anim.gif')
frames[0].save(p, format='GIF', save_all=True, append_images=frames[1:],
               duration=[40, 90, 150], loop=3)
cases.append(('动态 GIF', p, 3))
p = os.path.join(tmp, 'plain.jpg')
frames[0].save(p)
cases.append(('普通 JPEG', p, 1))

for label, path, want_frames in cases:
    out = os.path.join(tmp, 'out_' + os.path.basename(path))
    r = PSI((path, out, Cfg('顺时针 90°', '通用高清 (95%)', False, False), True))
    if not r.success:
        check(label, False, r.error_msg)
        continue
    os.replace(r.tmp_path, out)
    with Image.open(out) as im:
        got = getattr(im, 'n_frames', 1)
        size = im.size
    check(label, got == want_frames and size == (30, 40),
          f'{got} 帧 {size}（应为 {want_frames} 帧 (30, 40)）')

# 每帧内容都得单独转过，不能是第一帧复制 N 份
with Image.open(os.path.join(tmp, 'out_anim.webp')) as im:
    pix = []
    for i in range(im.n_frames):
        im.seek(i)
        pix.append(im.convert('RGB').getpixel((3, 3)))
check('各帧内容互不相同', len(set(pix)) == 3, f'{len(set(pix))} 种')

# GIF 的逐帧延时能被 Pillow 读回来，用它验证元数据没丢
with Image.open(os.path.join(tmp, 'out_anim.gif')) as im:
    durs = []
    for i in range(im.n_frames):
        im.seek(i)
        durs.append(im.info.get('duration'))
    loop = im.info.get('loop')
check('GIF 逐帧延时保留', durs == [40, 90, 150], str(durs))
check('GIF 循环次数保留', loop == 3, str(loop))

# 目标格式存不下多帧时退回第一帧
out = os.path.join(tmp, 'anim.ico')
r = PSI((os.path.join(tmp, 'anim.webp'), out,
         Cfg('不旋转', '通用高清 (95%)', False, True, ['fixed_256'], '自动 (推荐)'), True))
if r.success:
    os.replace(r.tmp_path, out)
    with Image.open(out) as im:
        check('动图转 ICO 取第一帧', sorted(im.ico.sizes()) == [(256, 256)],
              str(sorted(im.ico.sizes())))
else:
    check('动图转 ICO 取第一帧', False, r.error_msg)

print('\n== 不旋转的多帧图：原样复制，不重编码 ==')
# 重编码一个动图代价极高（WebP 编码器把全部帧物化在内存里，method=6 又慢），
# 而「仅压缩」重编码出来往往比源文件还大。不旋转就没有非重编码不可的理由。
anim_src = os.path.join(tmp, 'anim.webp')
os.utime(anim_src, (1000000000, 1000000000))
raw = open(anim_src, 'rb').read()

out = os.path.join(tmp, 'carry_anim.webp')
r = PSI((anim_src, out, Cfg('不旋转', '网络压缩 (85%)', True, False), True))
if r.success:
    os.replace(r.tmp_path, out)
    check('仅压缩 + 不旋转：字节与源完全一致', open(out, 'rb').read() == raw,
          f'{os.path.getsize(out):,}B / 源 {len(raw):,}B')
    check('文件修改时间也带过来', int(os.path.getmtime(out)) == 1000000000,
          str(int(os.path.getmtime(out))))
else:
    check('仅压缩 + 不旋转：字节与源完全一致', False, r.error_msg)

# 反向断言：别把「不重编码」做过头 —— 选了旋转就必须真的逐帧转。
out = os.path.join(tmp, 'rot_anim.webp')
r = PSI((anim_src, out, Cfg('顺时针 90°', '网络压缩 (85%)', True, False), True))
if r.success:
    os.replace(r.tmp_path, out)
    with Image.open(out) as im:
        rotated = im.size
    check('选了旋转仍然重编码', rotated == (30, 40) and open(out, 'rb').read() != raw,
          f'{rotated}')
else:
    check('选了旋转仍然重编码', False, r.error_msg)

# 高级设置里的「动图也重编码」能把这条捷径关掉 —— 给确实要换画质的人留的后门
out = os.path.join(tmp, 'forced_anim.webp')
forced = Cfg('不旋转', '网络压缩 (85%)', True, False, reencode_animated=True)
r = PSI((anim_src, out, forced, True))
if r.success:
    os.replace(r.tmp_path, out)
    with Image.open(out) as im:
        kept = im.n_frames
    check('开了「动图也重编码」就真的重编码',
          open(out, 'rb').read() != raw and kept == 3, f'{kept} 帧')
else:
    check('开了「动图也重编码」就真的重编码', False, r.error_msg)

# 闸门额度必须跟着走：只复制的动图不该白占内存预算，否则两个「预估 737 MB」
# 的动图就能把 2 GB 的闸门占满，而它们其实只是在做文件复制。反过来，开关一开
# 又得重新按帧数算 —— 少算了闸门就会放行一堆动图，几秒内把内存冲爆。
from imgwb.pipeline import estimate_peak_mb

for label, cfg_, want_zero in (
        ('不旋转（复制）', Cfg('不旋转', '网络压缩 (85%)', True, False), True),
        ('选了旋转', Cfg('顺时针 90°', '网络压缩 (85%)', True, False), False),
        ('开了动图也重编码', forced, False)):
    mb = estimate_peak_mb(anim_src, cfg_)
    check(f'闸门额度 · {label}', (mb == 0.0) == want_zero,
          f'{mb:.3f} MB（应{"为 0" if want_zero else "大于 0"}）')

print('\n== 压缩不许把文件压得更大（S-03 体积护栏）==')
# 造一张有噪点的照片型图：纯色图会被压成几 KB，体积关系失真，测不出东西。
noisy = Image.new('RGB', (700, 520))
_px = noisy.load()
for _y in range(520):
    for _x in range(700):
        _px[_x, _y] = ((_x * 7 + _y * 3) % 256, (_x ^ _y) % 256, (_y * 5) % 256)

guard_dir = os.path.join(tmp, 'guard')
os.makedirs(guard_dir, exist_ok=True)

# 已经压过的 JPEG 再走「网络压缩」会变大；BMP 根本没有画质参数，纯属白干
already = os.path.join(guard_dir, 'q60.jpg')
noisy.save(already, quality=60)
bmp_src = os.path.join(guard_dir, 'a.bmp')
noisy.save(bmp_src)
# 高画质 JPEG 是真能压小的，护栏不该把它也拦下来
fat = os.path.join(guard_dir, 'q95.jpg')
noisy.save(fat, quality=95)

squeeze = Cfg('不旋转', '网络压缩 (85%)', True, False)
for label, src_file, want_kept in (('已压过的 JPEG', already, True),
                                   ('BMP（没有画质参数）', bmp_src, True),
                                   ('高画质 JPEG', fat, False)):
    out_file = os.path.join(guard_dir, 'out_' + os.path.basename(src_file))
    res = PSI((src_file, out_file, squeeze, True))
    if not res.success:
        check(f'护栏 · {label}', False, res.error_msg)
        continue
    os.replace(res.tmp_path, out_file)
    a, b = os.path.getsize(src_file), os.path.getsize(out_file)
    check(f'护栏 · {label}', res.kept_original is want_kept,
          f'{a:,} -> {b:,} ({b / a:.2f}x) kept={res.kept_original}')
    if want_kept:
        # 保留原文件必须是**逐字节**保留，不是「又编码一遍但碰巧差不多」
        check(f'护栏 · {label} 逐字节等于源',
              open(out_file, 'rb').read() == open(src_file, 'rb').read())
    else:
        check(f'护栏 · {label} 确实压小了', b < a, f'{b / a:.2f}x')

# 选了旋转就不该兜底 —— 转完变大是应该的，拦下来反而丢了用户要的旋转
turned = os.path.join(guard_dir, 'turned.jpg')
res = PSI((already, turned, Cfg('顺时针 90°', '网络压缩 (85%)', True, False), True))
if res.success:
    os.replace(res.tmp_path, turned)
    with Image.open(turned) as _im:
        turned_size = _im.size
    check('护栏 · 选了旋转时不兜底', res.kept_original is False and turned_size == (520, 700),
          f'kept={res.kept_original} 尺寸={turned_size}')
else:
    check('护栏 · 选了旋转时不兜底', False, res.error_msg)

print('\n== 调色板图存 JPG（原来抛 OSError 并误报 IO错误）==')
pal = os.path.join(tmp, 'pal.jpg')
Image.new('RGB', (40, 30), (90, 160, 40)).convert('P').save(pal, format='GIF')
r = PSI((pal, os.path.join(tmp, 'pal_out.jpg'),
         Cfg('顺时针 90°', '通用高清 (95%)', False, False), True))
check('P 模式转 JPG', r.success, r.error_msg or '成功')

# ---------------------------------------------------------------- 路径处理
print('\n== 拖拽路径解析（tkdnd 只给含空格的项加花括号）==')
for label, data, want in [
        ('全部含空格', '{C:/My Photos/a.jpg} {C:/My Docs/b.jpg}',
         ['C:/My Photos/a.jpg', 'C:/My Docs/b.jpg']),
        ('都不含空格', 'C:/tmp/a.jpg C:/tmp/b.jpg', ['C:/tmp/a.jpg', 'C:/tmp/b.jpg']),
        ('混合·末项无空格', '{C:/My Photos/a.jpg} C:/tmp/b.jpg',
         ['C:/My Photos/a.jpg', 'C:/tmp/b.jpg']),
        ('混合·首项无空格', 'C:/tmp/a.jpg {C:/My Photos/b.jpg}',
         ['C:/tmp/a.jpg', 'C:/My Photos/b.jpg']),
        ('裸项夹中间', '{C:/My Pics/a.jpg} C:/b.jpg {C:/My Docs/c.jpg}',
         ['C:/My Pics/a.jpg', 'C:/b.jpg', 'C:/My Docs/c.jpg'])]:
    got = parse_drop_paths(data)
    check(label, got == want, str(got))

print('\n== 导出目录逃逸 ==')
OUT = r'D:\out'
ecfg = settings(out_mode='new', keep_structure=True, rotate_val='顺时针 90°')
tasks = build_tasks([r'D:\Photos\A\1.jpg', r'D:\Other\2.jpg'],
                    r'D:\Photos\A', OUT, ecfg)
check('多文件夹拖拽不跑出导出目录',
      all(os.path.normpath(t[1]).lower().startswith(OUT.lower()) for t in tasks),
      str([os.path.normpath(t[1]) for t in tasks]))
try:
    tasks = build_tasks([r'D:\Photos\1.jpg', r'C:\Users\me\2.jpg'],
                        r'D:\Photos', OUT, ecfg)
    check('跨盘不抛 ValueError',
          all(os.path.normpath(t[1]).lower().startswith(OUT.lower()) for t in tasks))
except Exception as exc:
    check('跨盘不抛 ValueError', False, f'{type(exc).__name__}: {exc}')
tasks = build_tasks([r'D:\Photos\1.jpg', r'D:\Photos\2024\2.jpg'],
                    r'D:\Photos', OUT, ecfg)
check('正常子目录结构保留',
      os.path.normpath(tasks[1][1]) == os.path.normpath(r'D:\out\Photos_已处理\2024\001.jpg'),
      os.path.normpath(tasks[1][1]))

print('\n== long_path ==')
unc = r'\\NAS\Photos' + r'\sub' * 60 + r'\a.jpg'
check('UNC 用 \\\\?\\UNC\\ 前缀',
      long_path(unc).startswith('\\\\?\\UNC\\NAS\\Photos'), long_path(unc)[:30])
check('短路径不加前缀', long_path(r'D:\a\b.jpg') == r'D:\a\b.jpg')

print('\n== 自然排序 ==')
try:
    got = sorted(['IMG_10.jpg', 'IMG_2.jpg', '2\u00b23.jpg'], key=natural_sort_key)
    check('上标数字不再让扫描崩掉', got[-1] == 'IMG_10.jpg', str(got))
except Exception as exc:
    check('上标数字不再让扫描崩掉', False, f'{type(exc).__name__}: {exc}')

print('\n== TIFF 画质选项 ==')
check('网络压缩 -> LZW',
      get_quality_params('.tif', '网络压缩 (85%)').get('compression') == 'tiff_lzw')
check('极致保真 -> 不改压缩方式',
      'compression' not in get_quality_params('.tiff', '极致保真 (100%)'))

print('\n== 扫描去重：真实路径按目录解析，不按文件 ==')
# 去重（父子来源重叠、重复路径、大小写别名）靠的是把路径归一成真实路径。
# `realpath` 很贵 —— 逐文件解析的话 3000 个文件要 325 毫秒（abspath 是 5 毫秒）。
# 同一个文件夹里的文件目录那段是一样的，所以每个目录只解析一次。
from imgwb import tasks as _tasks

scan_dir = os.path.join(tmp, 'scan_cost')
os.makedirs(os.path.join(scan_dir, 'sub'))
for _i in range(40):
    open(os.path.join(scan_dir, f'{_i:03d}.jpg'), 'wb').close()
    open(os.path.join(scan_dir, 'sub', f's{_i:03d}.jpg'), 'wb').close()

scan_cfg = type('C', (), {})()
scan_cfg.dropped_paths = []
scan_cfg.scan_subdirs = Var(True)
scan_cfg.copy_others = Var(False)

_calls = []
_real_realpath = _tasks.os.path.realpath


def _counting(p, *a, **k):
    _calls.append(p)
    return _real_realpath(p, *a, **k)


def _scan(roots):
    scan_cfg.dropped_roots = roots
    _calls.clear()
    _tasks.os.path.realpath = _counting
    try:
        return _tasks.scan_files(roots[0], scan_cfg)
    finally:
        _tasks.os.path.realpath = _real_realpath


found = _scan([scan_dir])
check('80 个文件都扫到了', len(found) == 80, str(len(found)))
check('realpath 只按目录调（2 个目录 -> 2 次），不是按文件（80 次）',
      len(_calls) == 2, f'{len(_calls)} 次')
# 省了调用不能把去重本身省没了：父;子、子;父、大小写别名都只出一份
for label, roots in (('父;子', [scan_dir, os.path.join(scan_dir, 'sub')]),
                     ('子;父', [os.path.join(scan_dir, 'sub'), scan_dir]),
                     ('大小写别名', [scan_dir, scan_dir.upper()]),
                     ('带 .. 的别名',
                      [scan_dir, os.path.join(scan_dir, 'sub', '..')])):
    found = _scan(roots)
    check(f'来源重叠（{label}）仍然只出一份', len(found) == 80,
          f'{len(found)} 个')
# 文件列表入口同样走目录缓存
scan_cfg.dropped_paths = ([os.path.join(scan_dir, f'{_i:03d}.jpg')
                           for _i in range(40)]
                          + [os.path.join(scan_dir.upper(), '000.JPG')])
found = _scan([scan_dir])
check('文件列表：重复项（大小写别名）去掉了', len(found) == 40, str(len(found)))
check('文件列表：realpath 也只按目录调', len(_calls) == 2, f'{len(_calls)} 次')

shutil.rmtree(tmp, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败 {len(fails)} 项: {fails}'))
sys.exit(1 if fails else 0)
