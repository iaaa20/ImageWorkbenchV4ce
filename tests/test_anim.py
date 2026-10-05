"""动图重编码：别把 disposal 填死，别丢时长，颜色要贴着原图。

三条都是从用户那张 `zzz.gif`（1254x1254、20 帧、带透明、GIMP 导出、10.57 MB）
上查出来的。

**一、`disposal` 的默认值绝不能填 2。** 原来写的是
`img.info.get('disposal', 2)` —— 源里没写就替它填 2。可我们写出去的每一帧都是
完整的合成画面（Pillow 迭代带透明的 GIF 时就是这么给的），根本不需要 disposal；
而 disposal=2「每帧前清空背景」会逼编码器放弃帧间差分、整帧全写：

    不写 disposal   0.86 MB (0.08x)   平均色差 0.46
    填 2            5.94 MB (0.56x)   平均色差 0.46     <- 大 7 倍，画质一点没多

**二、颜色要映射回源自带的全局调色板。** 不然 Pillow 会给每帧各自重新量化：

    Pillow 自己量化   1.30 MB   平均色差 1.44
    映射到源的原表    0.86 MB   平均色差 0.46     <- 又小又准

原图本来就只有那 256 种颜色，没必要另起炉灶。

**三、帧数变少不等于丢帧。** Pillow 会把相邻完全相同的帧合并、时长累加 ——
zzz.gif 20 帧变 14 帧，而总时长都是 1800 ms，播放完全一致。
所以**比对必须按时间轴对齐，不能按帧序号**：按序号比会一路错位，色差看起来
从 0 涨到 11.9，全是假的。我就这么误判过一次。

用法:  python tests\test_anim.py
不建窗口，几秒。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image, ImageChops, ImageSequence, ImageStat

from imgwb.config import ProcessConfig
from imgwb.pipeline import process_single_image

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def timeline(path):
    """展开成 (起, 止, 画面)。合并过的帧靠时长还原回同一条时间轴。"""
    out, t = [], 0
    with Image.open(path) as im:
        for f in ImageSequence.Iterator(im):
            d = f.info.get('duration') or 100
            out.append((t, t + d, f.convert('RGB').copy()))
            t += d
    return out


def at(tl, ms):
    for a, b, img in tl:
        if a <= ms < b:
            return img
    return tl[-1][2]


def main():
    work = tempfile.mkdtemp()
    # 一张大背景 + 一个移动的小方块：帧间高度冗余，正好考验帧间差分。
    # 有几帧刻意完全相同，用来验证「合并了但时长不丢」。
    W = H = 400
    bg = Image.new('RGB', (W, H))
    px = bg.load()
    for y in range(H):
        for x in range(W):
            px[x, y] = ((x * 5) % 256, (y * 7) % 256, ((x + y) * 3) % 256)
    raw = []
    for k in range(12):
        f = bg.copy()
        step = min(k, 8)                 # 后几帧不再移动 -> 完全相同
        for y in range(150, 250):
            for x in range(20 + step * 40, 120 + step * 40):
                if x < W:
                    f.putpixel((x, y), (250, 30, 30))
        raw.append(f)
    master = bg.convert('P', palette=Image.Palette.ADAPTIVE, colors=255)
    frames = [f.quantize(palette=master) for f in raw]
    src = os.path.join(work, 'anim.gif')
    # **必须带透明**，否则这个测试测不到东西：Pillow 迭代带透明的 GIF 时，
    # 第 1 帧起会合成成 RGBA，写回时才会走「重新量化 + disposal」那条路，
    # 也就是 zzz.gif 出问题的那条。不带透明的话帧一路是 P 模式、原样写回，
    # 色差恒为 0、体积 1.00x —— 测了个寂寞。
    frames[0].save(src, save_all=True, append_images=frames[1:],
                   duration=100, loop=0, transparency=255)
    before = os.path.getsize(src)
    ref = timeline(src)
    total = ref[-1][1]
    print(f'源 {len(ref)} 帧 {before / 1024:.0f} KB 总时长 {total} ms\n')

    cfg = ProcessConfig(rotate_mode='不旋转', quality_opt='通用高清 (95%)',
                        compress_only=True, is_ico=False,
                        reencode_animated=True)
    r = process_single_image((src, os.path.join(work, 'o.gif'), cfg,
                              False, False))
    check('重编码成功', r.success, str(r.error_msg))
    got = timeline(r.dst_path)
    after = os.path.getsize(r.dst_path)

    print('一、时长不许变（帧被合并是允许的）')
    check('**总时长一毫秒不差**', got[-1][1] == total,
          f'{total} -> {got[-1][1]} ms')

    print('\n二、颜色要贴着原图（按时间轴对齐比，不是按帧序号）')
    errs = [sum(ImageStat.Stat(ImageChops.difference(
        at(ref, ms), at(got, ms))).mean) / 3
        for ms in range(0, total, 50)]
    check('平均色差很小', sum(errs) / len(errs) < 3.0,
          f'平均 {sum(errs) / len(errs):.2f} 最大 {max(errs):.2f}')

    print('\n三、别被 disposal 逼成整帧全写')
    # **直接看传给编码器的参数**，不看回读值 —— Pillow 不把 disposal 写回
    # `info`，从产出文件上根本验不出来；而体积差只在编码本来就松散的源上才
    # 显眼（zzz.gif 是 GIMP 导出的，差 7 倍；合成夹具编得紧，差不出来）。
    from imgwb import pipeline as _pl

    seen = {}
    real_save = Image.Image.save

    def spy(self, fp, *a, **kw):
        if kw.get('save_all'):
            seen.clear()
            seen.update(kw)
        return real_save(self, fp, *a, **kw)

    Image.Image.save = spy
    try:
        with Image.open(src) as im:
            _pl.save_animated(im, os.path.join(work, 'spy.gif'), '.gif',
                              cfg, len(ref))
    finally:
        Image.Image.save = real_save

    check('源没写 disposal 时，不许替它填 2',
          seen.get('disposal') != 2,
          f'传给编码器的 disposal = {seen.get("disposal")!r}')
    check('时长和循环照常传下去',
          'duration' in seen and 'loop' in seen, str(sorted(seen)))
    check('体积没有反而暴涨', after <= before * 1.2,
          f'{before / 1024:.0f} KB -> {after / 1024:.0f} KB '
          f'({after / before:.2f}x)')

    color_loop_duration_cases(work)

    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


def _rot180(src, dst):
    cfg = ProcessConfig(rotate_mode='旋转 180°', quality_opt='通用高清 (95%)',
                        compress_only=False, is_ico=False)
    return process_single_image((src, dst, cfg, False, False))


def _rgba_frames(path):
    with Image.open(path) as im:
        return [f.convert('RGBA').copy() for f in ImageSequence.Iterator(im)]


def _over_256_gif(path, with_alpha):
    """两帧，每帧自己的调色板都合法（≤256 色），**合成后的第二帧超过 256 色**。

    第二帧左半边是透明的、`disposal=1`，所以播放时左边透出第一帧的 200 种
    红，右边是第二帧自己的 150 种蓝（最右一条是纯蓝）。
    `with_alpha` 时两帧顶上各留 4 行真透明，合成画面也带透明。
    """
    W, H = 64, 40
    top = 4 if with_alpha else 0
    f1 = Image.new('P', (W, H))
    pal = [0, 0, 0]
    for i in range(1, 201):
        pal += [55 + i, i, 0]                       # 蓝通道全是 0
    f1.putpalette(pal + [0] * (768 - len(pal)))
    f1.putdata([0 if y < top else 1 + ((x + y * 7) % 200)
                for y in range(H) for x in range(W)])
    f2 = Image.new('P', (W, H))
    pal = [0, 0, 0]
    for i in range(1, 151):
        pal += [0, i, 255 if i > 100 else 105 + i]
    f2.putpalette(pal + [0] * (768 - len(pal)))
    f2.putdata([0 if (x < W // 2 or y < top)
                else (150 if x >= W - 8 else 1 + ((x + y) % 149))
                for y in range(H) for x in range(W)])
    f1.save(path, save_all=True, append_images=[f2], duration=[80, 110],
            transparency=0, disposal=1, optimize=False, loop=0)


def color_loop_duration_cases(work):
    """这三条都是"帧数没少、文件能打开、返回成功"，
    所以只数帧数的断言一条都拦不住 —— 必须看颜色、循环字段和每帧时长。"""
    print('\n四、合成后超过 256 色的 GIF 不许丢色')
    for label, with_alpha in (('不透明', False), ('带透明', True)):
        src = os.path.join(work, f'over256_{with_alpha}.gif')
        _over_256_gif(src, with_alpha)
        ref = _rgba_frames(src)
        ncol = len(ref[-1].convert('RGB').getcolors(1 << 20))
        blue = ref[-1].getchannel('B').getextrema()[1]
        # 先确认夹具真的踩在被测条件上，否则下面的断言测了个寂寞
        check(f'{label}：夹具成立（两帧、第二帧合成后 >256 色、有纯蓝）',
              len(ref) == 2 and ncol > 256 and blue == 255,
              f'{len(ref)} 帧，{ncol} 色，蓝最大 {blue}')
        r = _rot180(src, os.path.join(work, f'over256_{with_alpha}_o.gif'))
        check(f'{label}：处理成功', r.success, str(r.error_msg))
        if not r.success:
            continue
        got = _rgba_frames(r.dst_path)
        out_blue = got[-1].getchannel('B').getextrema()[1]
        check(f'{label}：**纯蓝那一块还是蓝的**', out_blue >= 250,
              f'蓝通道最大值 {blue} -> {out_blue}')
        if with_alpha:
            def clear(frames):
                return [f.getchannel('A').histogram()[0] for f in frames]
            # 旋转 180° 不改变透明像素的个数；多了是透明区被填上，
            # 少了/逐帧变化是上一帧从透明处透出来（残影）
            check('带透明：每帧透明像素数不变（母版丢了，透明不能跟着丢）',
                  len(got) == len(ref) and clear(got) == clear(ref),
                  f'{clear(ref)} -> {clear(got)}')

    print('\n五、只播一遍的 GIF 不许变成无限循环')
    src = os.path.join(work, 'once.gif')
    a = Image.new('RGB', (30, 20), (255, 0, 0))
    b = Image.new('RGB', (30, 20), (0, 0, 255))
    a.save(src, save_all=True, append_images=[b], duration=[80, 110])
    with Image.open(src) as im:
        check('夹具成立（源里没有循环扩展）', 'loop' not in im.info,
              str(im.info.get('loop')))
    r = _rot180(src, os.path.join(work, 'once_o.gif'))
    check('处理成功', r.success, str(r.error_msg))
    if r.success:
        with Image.open(r.dst_path) as im:
            check('**产物里也没有循环扩展**', 'loop' not in im.info,
                  f'loop = {im.info.get("loop")!r}')
    # 反过来：写了循环次数的要原样带过去，0（无限）和 3 都验
    for want in (0, 3):
        src = os.path.join(work, f'loop{want}.gif')
        a.save(src, save_all=True, append_images=[b], duration=[80, 110],
               loop=want)
        with Image.open(src) as im:
            had = im.info.get('loop')
        r = _rot180(src, os.path.join(work, f'loop{want}_o.gif'))
        got = None
        if r.success:
            with Image.open(r.dst_path) as im:
                got = im.info.get('loop')
        check(f'源写了 loop={want} 的原样保留', r.success and got == had,
              f'{had!r} -> {got!r}')

    print('\n六、带默认图的 APNG：帧时长不许错位')
    src = os.path.join(work, 'preview.png')
    prev = Image.new('RGB', (30, 20), (0, 255, 0))
    prev.save(src, save_all=True, default_image=True, append_images=[a, b],
              duration=[80, 110], loop=0)

    def describe(path):
        with Image.open(path) as im:
            return (bool(getattr(im, 'default_image', False)),
                    [f.info.get('duration')
                     for f in ImageSequence.Iterator(im)],
                    [f.convert('RGB').getpixel((5, 5))
                     for f in ImageSequence.Iterator(im)])

    before = describe(src)
    check('夹具成立（默认图 + 两个动画帧，80/110 ms）',
          before[0] and len(before[1]) == 3
          and [int(d) for d in before[1][1:]] == [80, 110], str(before[:2]))
    r = _rot180(src, os.path.join(work, 'preview_o.png'))
    check('处理成功', r.success, str(r.error_msg))
    if r.success:
        after = describe(r.dst_path)
        check('**每帧时长和源一致**', after[1] == before[1],
              f'{before[1]} -> {after[1]}')
        check('默认图标记和各帧画面都还在',
              after[0] == before[0] and after[2] == before[2],
              f'{before[0]},{before[2]} -> {after[0]},{after[2]}')
    # 没有默认图的普通 APNG 不能被这条修复带歪
    src = os.path.join(work, 'plain.png')
    a.save(src, save_all=True, append_images=[b], duration=[80, 110], loop=0)
    before = describe(src)
    r = _rot180(src, os.path.join(work, 'plain_o.png'))
    after = describe(r.dst_path) if r.success else None
    check('普通 APNG 的时长照旧', r.success and after[:2] == before[:2],
          f'{before[:2]} -> {after and after[:2]}')


if __name__ == '__main__':
    main()
