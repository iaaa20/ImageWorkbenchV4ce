"""闸门的预估必须又快又准，而且**绝不能低估**。

三件事：

**一、`probe_peak` 不许解码。** 它在**主编排线程**上对每一张图跑一次，
一旦解码就等于把整批图先在单线程上解一遍，工人全在旁边等着。
`recompress_pointless` 里的 `im.getexif()` 曾经就是这个坑 —— PNG 的
`getexif()` 会强制 `load()`（eXIf 块允许出现在 IDAT 之后，Pillow 只能先解
一遍才敢说没有），实测 42 MP 的 PNG 要 293 ms + 220 MB，其它格式都是 0.0 ms。
真实 PNG 语料上修掉之后墙上时间 8.0 → 6.3 秒、吞吐 56.9 → 71.3 MB/s。

**二、系数必须盖得住最坏配置，又不能白占额度。** 内存预算是固定的，
高估多少就等于少跑多少张，直接压 CPU 利用率；低估就是撑爆内存。
下面 `MEASURED` 是真实素材上量到的地板，口径见 pipeline.py 顶部第三次标定：
**峰值工作集净增 ÷（宽 × 高 × 通道数）**。

**三、两个真实翻车用例。** 19 个真实样本量下来，早期模型（一律按宽×高×4 估）
在两个方向都错：灰度 JPEG 高估 3.4 倍（白扔额度），PNG 改名成 .jpg 低估到
0.48 倍（会撑爆）。这两条单独钉住。

用法:  python tests\test_probe.py
不建窗口，几秒。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image

from imgwb.config import ProcessConfig
from imgwb.pipeline import identify, probe_peak, recompress_pointless

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def cfg(quality='通用高清 (95%)', rotate='不旋转'):
    return ProcessConfig(rotate_mode=rotate, quality_opt=quality,
                         compress_only=True, is_ico=False)


# (扩展名, 画质档, 是否旋转) -> 真实素材上量到的系数。**这是地板。**
MEASURED = {
    ('.jpg', '极致保真 (100%)', False): 5.09,
    ('.jpg', '极致保真 (100%)', True): 6.47,
    ('.jpg', '通用高清 (95%)', False): 4.88,
    ('.jpg', '通用高清 (95%)', True): 6.21,
    ('.jpg', '网络压缩 (85%)', False): 3.77,
    ('.png', '通用高清 (95%)', False): 3.05,
    ('.png', '通用高清 (95%)', True): 3.06,
    ('.png', '网络压缩 (85%)', False): 4.06,
    ('.png', '网络压缩 (85%)', True): 5.06,
    ('.webp', '通用高清 (95%)', False): 9.47,
    ('.webp', '通用高清 (95%)', True): 11.44,
}

W, H = 2400, 1800


def main():
    work = tempfile.mkdtemp()
    im = Image.new('RGB', (W, H))
    px = im.load()
    for y in range(0, H, 3):
        for x in range(0, W, 2):
            px[x, y] = ((x * 3) % 256, (y * 7) % 256, (x ^ y) % 256)
    paths = {}
    for ext, kw in (('.jpg', {'quality': 100, 'subsampling': 0}),
                    ('.png', {'compress_level': 1}),
                    ('.webp', {'quality': 90})):
        # 两个夹具都要保证「真的会走处理路径」，否则估算返回 0，系数断言就
        # 测了个寂寞：
        #   JPEG 存 q100 —— 源画质低于「目标 - 5」会被判定为再压没意义
        #   PNG 存 level 1 —— 源已按不低于 level 6 压过（zlib FLEVEL>=2）会被跳过
        p = os.path.join(work, 'a' + ext)
        im.save(p, **kw)
        paths[ext] = p
    # 分母跟产品代码一致：宽 × 高 × 通道数（合成图是 RGB，3 通道）
    basis_mb = W * H * 3 / 1048576

    print('一、probe_peak 不许解码')
    for ext, p in paths.items():
        probe_peak(p, cfg())
        with Image.open(p) as o:
            recompress_pointless(o, ext, '通用高清 (95%)', os.path.getsize(p))
            # _im 是 Pillow 的像素缓冲；非 None 就说明真的解码了
            check(f'{ext} 判定时没有解码', o._im is None,
                  '' if o._im is None
                  else '像素被解出来了 —— getexif() 之类的调用会强制 load()')

    print('\n二、估算不许低于实测（低估 = 撑爆内存）')
    for (ext, quality, rot), floor in sorted(MEASURED.items()):
        c = cfg(quality, '顺时针' if rot else '不旋转')
        est = probe_peak(paths[ext], c)[0]
        got = est / basis_mb
        # 0 表示这一张会被原样复制、根本不解码，那就没有系数可言
        check(f'{ext} {quality} {"旋转" if rot else "不旋转"}',
              est > 0 and got >= floor,
              f'{got:.2f} >= {floor:.2f}' if est > 0 and got >= floor
              else f'估算系数 {got:.2f} < 实测 {floor:.2f}')

    print('\n三、也别白占额度（余量不该超过 70%）')
    for (ext, quality, rot), floor in sorted(MEASURED.items()):
        c = cfg(quality, '顺时针' if rot else '不旋转')
        got = probe_peak(paths[ext], c)[0] / basis_mb
        check(f'{ext} {quality} {"旋转" if rot else "不旋转"} 余量',
              got <= floor * 1.7, f'{got / floor:.2f}x')

    print('\n四、两个真实翻车用例')
    # 灰度：Pillow 内部只有 1 个通道，按 4 通道估就是白扔 3/4 的闸门额度。
    # 真实样本 s09/s12（扫描件）当时高估 3.37x / 3.57x。
    gray = os.path.join(work, 'g.jpg')
    Image.new('L', (W, H)).save(gray, quality=100)
    rgb_est = probe_peak(paths['.jpg'], cfg())[0]
    gray_est = probe_peak(gray, cfg())[0]
    check('灰度图的额度明显低于同尺寸彩图', 0 < gray_est < rgb_est * 0.5,
          f'灰度 {gray_est:.0f} MB vs 彩色 {rgb_est:.0f} MB')

    # 扩展名和真实格式不符：PNG 改名成 .jpg，真实素材里就有两张。
    # 只看源格式会按 PNG 估，实际 RGBA 要先转 RGB 再交给 JPEG 编码器，
    # 当时低估到 0.48x。峰值该由重的那一侧决定。
    fake = os.path.join(work, 'fake.png')
    Image.new('RGBA', (W, H), (10, 20, 30, 255)).save(fake, compress_level=1)
    as_png = probe_peak(fake, cfg(), os.path.join(work, 'out.png'))[0]
    as_jpg = probe_peak(fake, cfg(), os.path.join(work, 'out.jpg'))[0]
    check('目标是 .jpg 时按更重的那一侧估', as_jpg > as_png,
          f'目标 .png {as_png:.0f} MB / 目标 .jpg {as_jpg:.0f} MB')

    print('\n五、要缩尺寸的动图不是"复制"，额度不能记 0')
    # 不旋转的动图默认只复制、额度记 0 —— 但设了最长边而它又超了的话，
    # `process_single_image` 会逐帧解码、缩放、重编码。两边判据必须一致。
    anim = os.path.join(work, 'anim.gif')
    f1 = Image.new('RGB', (120, 80), (255, 0, 0))
    f2 = Image.new('RGB', (120, 80), (0, 0, 255))
    f1.save(anim, save_all=True, append_images=[f2], duration=[80, 110], loop=0)

    def anim_cfg(max_edge):
        return ProcessConfig(rotate_mode='不旋转', quality_opt='通用高清 (95%)',
                             compress_only=False, is_ico=False,
                             max_edge=max_edge)

    est, frames = probe_peak(anim, anim_cfg(60), anim)
    check('**超了最长边的动图要占额度**', est > 0 and frames == 2,
          f'估 {est:.3f} MB，{frames} 帧')
    info = identify((anim, anim, anim_cfg(60), False))
    check('预扫描也不再说它"原样复制"', info.plan == 'process',
          f'plan={info.plan} {info.reason}')
    # 对照：没超上限 / 没设上限时仍然是复制、额度 0（这条捷径不能被修没了）
    for edge, label in ((0, '没设上限'), (200, '没超上限')):
        est, _n = probe_peak(anim, anim_cfg(edge), anim)
        check(f'{label}时仍按复制记 0', est == 0.0, f'估 {est:.3f} MB')

    print('\n六、动图写成 WebP / APNG：估算不许低于实测')
    # 实测值（独立子进程、旋转 180°、峰值工作集净增，见 pipeline.py 里
    # ANIMATED_COST 的说明）。旧公式「帧数 × 1.3」在这几个用例上只估到
    # 实测的 0.39 ~ 0.66 倍 —— 闸门会放进来两倍半的量。
    # 峰值取决于尺寸、帧数和像素格式，所以夹具只要这三样对得上就行，
    # 画面内容随意（这里用纯色 + 一个每帧移动的色块，免得帧被合并）。
    ANIM_MEASURED = {
        ('.webp', 600, 40, 'RGB'): 85,
        ('.webp', 1600, 12, 'RGB'): 256,
        ('.webp', 800, 60, 'RGBA'): 199,
        ('.png', 600, 40, 'RGB'): 119,
        ('.png', 1600, 12, 'RGB'): 294,
        ('.png', 800, 60, 'RGBA'): 309,
    }
    spin = ProcessConfig(rotate_mode='旋转 180°', quality_opt='通用高清 (95%)',
                         compress_only=False, is_ico=False)
    for (ext, size, n, mode), floor in sorted(ANIM_MEASURED.items()):
        p = os.path.join(work, f'anim_{size}_{n}_{mode}{ext}')
        frames_ = []
        for k in range(n):
            f = Image.new(mode, (size, size),
                          (40, 90, 160) + ((255,) if mode == 'RGBA' else ()))
            f.paste((250, (k * 6) % 256, 30) + ((255,) if mode == 'RGBA' else ()),
                    (k * 5, 10, k * 5 + 60, 70))
            if mode == 'RGBA':
                f.paste((0, 0, 0, 0), (0, size - 40, 40, size))
            frames_.append(f)
        kw = {'quality': 80, 'method': 0} if ext == '.webp' else {'compress_level': 1}
        frames_[0].save(p, save_all=True, append_images=frames_[1:],
                        duration=80, loop=0, **kw)
        est, got_n = probe_peak(p, spin, p)
        with Image.open(p) as o:
            real_mode = o.mode
        label = f'{ext} {size}x{size} x{n} {mode}'
        check(f'{label}：夹具成立', got_n == n and real_mode == mode,
              f'{got_n} 帧 {real_mode}')
        check(f'{label}：**不低估**', est >= floor,
              f'估 {est:.0f} MB / 实测 {floor} MB（{est / floor:.2f}x）')
        check(f'{label}：也没白占太多（余量 ≤ 70%）', est <= floor * 1.7,
              f'{est / floor:.2f}x')

    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
