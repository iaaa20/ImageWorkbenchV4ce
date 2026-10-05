"""格式混淆：丢帧、颜色模式、以及"再压一遍注定白干就别压"。

来自第二轮格式混测报告。三件事：
  G-01 目标是 PNG 时，帧必须物化成列表 —— 给生成器会**静默**只写下第一帧。
  G-02 动画被压成单帧时要报出来，不能一声不吭当成功。
  G-04 .png / .bmp 目标缺颜色模式兜底，CMYK / 16 位 / LA 直接撞进编码器。
另外盯住性能优化的正确性：跳过重存不能跳掉带 EXIF 方向的照片。

用法:  python tests\\test_formats.py
不建窗口。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image

from imgwb.config import ProcessConfig
from imgwb.pipeline import (jpeg_quality, process_single_image,
                            recompress_pointless, target_quality)

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


tmp = tempfile.mkdtemp()


def frames_of(path):
    with Image.open(path) as im:
        return getattr(im, 'n_frames', 1)


def run(src, dst, cfg):
    return process_single_image((src, dst, cfg, False, False))


ROT = ProcessConfig(rotate_mode='顺时针 90°', quality_opt='通用高清 (95%)',
                    compress_only=False, is_ico=False)
COMPRESS85 = ProcessConfig(rotate_mode='不旋转', quality_opt='网络压缩 (85%)',
                           compress_only=True, is_ico=False)

# ---------------------------------------------------------------- 语料
anim = [Image.new('RGB', (60, 40), (i * 40 % 256, 90, 200 - i * 20))
        for i in range(6)]


def save_anim(path, **kw):
    anim[0].save(path, save_all=True, append_images=anim[1:], duration=100,
                 loop=0, **kw)
    return path


apng = save_anim(os.path.join(tmp, 'a.png'))
awebp = save_anim(os.path.join(tmp, 'a.webp'))

print('=== G-01 多帧源转 PNG，帧数不能变 ===')
# 这是要害：文件本身命名完全正确，用户只是点了「顺时针 90°」，动画就没了。
for label, src in (('APNG -> PNG', apng), ('动态 WebP -> PNG', awebp)):
    out = os.path.join(tmp, f'out_{os.path.basename(src)}.png')
    r = run(src, out, ROT)
    got = frames_of(out) if r.success else -1
    check(label, got == 6, f'{got} 帧（源 6 帧）')

print('\n=== 其余多帧目标格式也不许丢帧 ===')
for ext in ('.webp', '.gif', '.tiff'):
    out = os.path.join(tmp, f'rot{ext}')
    r = run(awebp, out, ROT)
    got = frames_of(out) if r.success else -1
    check(f'动态 WebP -> {ext}', got == 6, f'{got} 帧')

print('\n=== G-02 动画真被压成单帧时，必须报出来 ===')
out = os.path.join(tmp, 'flat.jpg')
r = run(awebp, out, ROT)
check('转 JPG 确实只剩一帧', frames_of(out) == 1)
check('结果里带出了 frames_dropped', r.frames_dropped == 6,
      f'frames_dropped={r.frames_dropped}')

out = os.path.join(tmp, 'keep.webp')
r2 = run(awebp, out, ROT)
check('动画保住时不误报', r2.frames_dropped == 0, f'{r2.frames_dropped}')

print('\n=== G-04 .png / .bmp 的颜色模式兜底 ===')
cmyk = os.path.join(tmp, 'cmyk.jpg')
Image.new('CMYK', (50, 40), (10, 20, 30, 40)).save(cmyk)
i16 = os.path.join(tmp, 'i16.png')
Image.new('I;16', (50, 40), 1000).save(i16)
la = os.path.join(tmp, 'la.png')
Image.new('LA', (50, 40), (128, 255)).save(la)

for label, src, ext in (('CMYK JPEG -> png', cmyk, '.png'),
                        ('CMYK JPEG -> bmp', cmyk, '.bmp'),
                        ('16 位 PNG -> bmp', i16, '.bmp'),
                        ('LA PNG -> bmp', la, '.bmp')):
    out = os.path.join(tmp, 'mode_' + os.path.basename(src) + ext)
    r = run(src, out, ROT)
    check(label, r.success, r.error_msg or '成功')

print('\n=== 性能优化：注定白干的重存要跳过，但不能跳错 ===')
photo = Image.new('RGB', (200, 150))
px = photo.load()
for y in range(150):
    for x in range(200):
        px[x, y] = ((x * 3) % 256, (y * 5) % 256, (x ^ y) % 256)

q60 = os.path.join(tmp, 'q60.jpg'); photo.save(q60, quality=60)
q95 = os.path.join(tmp, 'q95.jpg'); photo.save(q95, quality=95)
bmp = os.path.join(tmp, 'x.bmp'); photo.save(bmp)

check('画质反推准确', abs(jpeg_quality(Image.open(q60)) - 60) <= 2
      and abs(jpeg_quality(Image.open(q95)) - 95) <= 2,
      f'q60->{jpeg_quality(Image.open(q60)):.0f}  q95->{jpeg_quality(Image.open(q95)):.0f}')
check('目标画质映射', (target_quality('网络压缩 (85%)'), target_quality('极致保真 (100%)'))
      == (85, 100))

r = run(q60, os.path.join(tmp, 'o60.jpg'), COMPRESS85)
check('源 q60 按 85% 压 -> 跳过重存、原样保留', r.kept_original is True)
check('  且输出与源逐字节一致',
      open(os.path.join(tmp, 'o60.jpg'), 'rb').read() == open(q60, 'rb').read())

r = run(q95, os.path.join(tmp, 'o95.jpg'), COMPRESS85)
check('源 q95 按 85% 压 -> 正常重存', r.kept_original is False)
check('  且确实变小了',
      os.path.getsize(os.path.join(tmp, 'o95.jpg')) < os.path.getsize(q95),
      f'{os.path.getsize(q95)} -> {os.path.getsize(os.path.join(tmp, "o95.jpg"))}')

r = run(bmp, os.path.join(tmp, 'o.bmp'), COMPRESS85)
check('BMP 没有画质参数 -> 跳过重存', r.kept_original is True)

# 带 EXIF 方向的照片**不能**跳过：「仅压缩」承诺过会把方向烘焙进像素，
# 跳过就等于毁约，那批照片会在别的看图软件里躺倒。
rot_jpg = os.path.join(tmp, 'rot.jpg')
ex = Image.Exif()
ex[0x0112] = 6
photo.save(rot_jpg, quality=60, exif=ex)
with Image.open(rot_jpg) as im:
    why = recompress_pointless(im, '.jpg', '网络压缩 (85%)')
check('带 EXIF 方向的照片不跳过', why == '', why or '（不跳过）')
r = run(rot_jpg, os.path.join(tmp, 'orot.jpg'), COMPRESS85)
with Image.open(os.path.join(tmp, 'orot.jpg')) as im:
    baked = im.size == (150, 200) and (im.getexif() or {}).get(0x0112, 1) in (0, 1)
check('  方向确实被烘焙进像素', baked,
      f'输出尺寸 {Image.open(os.path.join(tmp, "orot.jpg")).size}（源 200x150）')

print('\n=== 按格式跳过：该跳的跳、能压的一个别误伤 ===')
# 用户实测：3393 张 WebP 走「仅压缩 95%」，3352 张编完之后被体积护栏丢弃，
# 白烧 1956 秒（占全程 95%）。跳过判据原来只认 JPEG，漏了 WebP。
#
# **阈值是拿真实素材标的，不是合成图。** 900 个真实文件（jpg/png/webp）
# 逐个"既跑判据、又真编一遍当标准答案"，统计误跳过和白干。合成图标出来的
# 那一版在真实图上错得很明显（WebP 95% 档"一律跳过"误跳过 6 张、漏掉 120%
# 体积），详见 pipeline.py 顶部的表。
#
# 现在的契约：
#   JPEG  源画质 <= 目标-5 才跳（留余量，因为元数据/缩略图也能挤掉一点）
#   WebP  只在目标 >=95 且 bpp <= 1.5 时跳；85% 档不跳（那档几乎总能压小）
#   PNG   不跳
#   BMP   没有画质参数，跳
import random as _r

big = Image.new('RGB', (900, 700))
_bpx = big.load()
_r.seed(11)
for _y in range(700):
    for _x in range(900):
        _n = _r.randint(-10, 10)
        _bpx[_x, _y] = (max(0, min(255, int(200 * _y / 700) + _n)),
                        max(0, min(255, 128 + (_x * 31 // 900) + _n)),
                        max(0, min(255, 90 + ((_x + _y) * 23 // 1600) + _n)))

_srcs = {}
for _q in (75, 95):
    _p = os.path.join(tmp, 'src%d.webp' % _q)
    big.save(_p, quality=_q, method=6)
    _srcs['WebP q%d' % _q] = _p
# 两张 PNG，差别只在源用了多狠的 zlib 级别 —— 这正是新判据看的东西。
# level 1 的 zlib 头写 FLEVEL=0，level 6 写 FLEVEL=2（我们自己也用 6）。
_p = os.path.join(tmp, 'src.png')
big.save(_p, compress_level=1); _srcs['PNG'] = _p
_p6 = os.path.join(tmp, 'src_lv6.png')
big.save(_p6, compress_level=6); _srcs['PNG-lv6'] = _p6
_p = os.path.join(tmp, 'src98.jpg'); big.save(_p, quality=98)
_srcs['JPEG q98'] = _p


def _run_compress(label, src, quality_opt):
    cfg = ProcessConfig(rotate_mode='不旋转', quality_opt=quality_opt,
                        compress_only=True, is_ico=False)
    out = os.path.join(tmp, 'o_%s_%s%s' % (label.replace(' ', ''),
                                           quality_opt[:2],
                                           os.path.splitext(src)[1]))
    r = process_single_image((src, out, cfg, False, False))
    encoded = bool((r.timings or {}).get('encode'))
    return r, encoded, os.path.getsize(out) / os.path.getsize(src)


# 95% 档：压得狠的 WebP 跳过，画质高的（bpp 大）照常处理
from imgwb.pipeline import source_bpp, WEBP_BPP_HIGH_TARGET

for _label in ('WebP q75', 'WebP q95', 'JPEG q98'):
    _src = _srcs[_label]
    _r2, _enc, _ratio = _run_compress(_label, _src, '通用高清 (95%)')
    if _label.startswith('JPEG'):
        # q98 高于目标 95，是真能压小的，必须照常处理
        check('%s 95%% 档要真压缩' % _label, _enc and _ratio < 1.0,
              '%.2fx' % _ratio)
    else:
        with Image.open(_src) as _im:
            _bpp = source_bpp(_im, os.path.getsize(_src))
        want_skip = _bpp <= WEBP_BPP_HIGH_TARGET
        if want_skip:
            check('%s 95%% 档（bpp %.2f <= %.1f）要跳过'
                  % (_label, _bpp, WEBP_BPP_HIGH_TARGET),
                  _r2.kept_original and not _enc,
                  '%.2fx 编码=%s' % (_ratio, _enc))
        else:
            check('%s 95%% 档（bpp %.2f > %.1f）不跳，照常处理'
                  % (_label, _bpp, WEBP_BPP_HIGH_TARGET), _enc,
                  '%.2fx' % _ratio)

# ---- GIF：唯一的压缩杠杆是减色，而且要有体积护栏 -------------------
# GIF 一律不压：唯一的杠杆是减色（128 色 0.91x、64 色 0.76x），而
# `optimize=True` 反而更大。减色发过一版又撤了 —— 收益一两成，但用户在真实
# 素材上看到明显偏色。这里钉住「不压」和「护栏兜得住」。
_gf = [Image.new('RGB', (240, 180)) for _ in range(6)]
for _k, _im in enumerate(_gf):
    _px = _im.load()
    for _y in range(0, 180, 2):
        for _x in range(0, 240, 2):
            _px[_x, _y] = ((_x * 3 + _k * 11) % 256, (_y * 7) % 256,
                           (_x ^ _y) % 256)
_gf = [f.convert('P', palette=Image.Palette.ADAPTIVE) for f in _gf]
_gsrc = os.path.join(tmp, 'anim.gif')
_gf[0].save(_gsrc, save_all=True, append_images=_gf[1:], duration=80, loop=0)
_gbefore = os.path.getsize(_gsrc)


def _run_gif(quality, reencode):
    cfg = ProcessConfig(rotate_mode='不旋转', quality_opt=quality,
                        compress_only=True, is_ico=False,
                        reencode_animated=reencode)
    out = os.path.join(tmp, f'g_{quality[:2]}_{reencode}.gif')
    r = process_single_image((_gsrc, out, cfg, False, False))
    with Image.open(r.dst_path) as o:
        return r, os.path.getsize(r.dst_path), o.n_frames


_r, _n, _fr = _run_gif('通用高清 (95%)', True)
# GIF 重编码现在**可能真的压小**（把每帧映射回源自带的调色板、不再硬填
# disposal=2）—— 用户那张 zzz.gif 实测 10.57 MB -> 0.86 MB。
# 所以这里不再断言「必然更大、必然走护栏」，改成钉住底线：**永远不会比源大**。
# 压小了就是压小了，压不小就得由护栏原样保留 —— 两条路都可以，只是不许变大。
# （**动图这条路上原来根本没有护栏**：用户勾了「动图也重编码」跑 GIF，
#   产出大 21% 还被当成成功写了出去。）
check('GIF 95% 重编码后绝不会比源大',
      _n <= _gbefore, f'{_n / _gbefore:.2f}x  保留原文件={_r.kept_original}')
check('GIF 95% 帧数不变', _fr == 6, f'{_fr} 帧')

# 减色试过又撤了（收益一两成，但用户在真实素材上看到明显偏色，
# 而合成夹具复现不出来 —— 详见 `get_quality_params` 里 GIF 那一段）。
# 所以 85% 档跟 95% 一个待遇：不做有损减色。
_r, _n, _fr = _run_gif('网络压缩 (85%)', True)
check('GIF 85% 档不做有损减色，也不会比源大',
      _n <= _gbefore, f'{_n / _gbefore:.2f}x')
check('GIF 85% 帧数不变', _fr == 6, f'{_fr} 帧')

_r, _n, _fr = _run_gif('通用高清 (95%)', False)
check('不勾「动图也重编码」时 GIF 原样带过', _n == _gbefore, f'{_n / _gbefore:.2f}x')

# PNG 的跳过判据看的是**源用了多狠的 zlib 级别**，不是 bpp。
# bpp 那条路量下来分不开（噪点 level6 bpp 8.33 无收益，纯色 level1 bpp 0.25
# 却有 0.18x 收益）—— 因为收益取决于级别，而 bpp 反映的是内容复杂度，两者正交。
# zlib 的 FLEVEL 把级别直接写在文件里：level 1->0、2~5->1、**6->2**、7~9->3。
# 我们自己写 PNG 用 compress_level=6，所以 FLEVEL>=2 的源重压必然不会更小。
# 318 个真实 PNG 实测：FLEVEL=2 占 91%，重编能省 >=5% 的只有 1%，
# 而它平均要 366 ms —— 是另外两档的十倍。跳掉省 105 秒，只错过 2 张共 0.1 MB。
from imgwb.pipeline import png_flevel                          # noqa: E402

check('level 1 的 PNG 头里 FLEVEL < 2', png_flevel(_srcs['PNG']) < 2,
      'FLEVEL=%s' % png_flevel(_srcs['PNG']))
check('level 6 的 PNG 头里 FLEVEL >= 2', png_flevel(_srcs['PNG-lv6']) >= 2,
      'FLEVEL=%s' % png_flevel(_srcs['PNG-lv6']))

_r2, _enc, _ratio = _run_compress('PNG', _srcs['PNG'], '通用高清 (95%)')
check('源压得松的 PNG 照常重压', _enc, '编码=%s' % _enc)

_r3, _enc6, _ratio6 = _run_compress('PNG-lv6', _srcs['PNG-lv6'], '通用高清 (95%)')
check('源已按 level 6 压过的 PNG 直接跳过、不白编',
      _r3.kept_original and not _enc6, '编码=%s' % _enc6)

# 85% 档要做调色板量化，那一档几乎总能压小很多，**不能**因为 FLEVEL 跳掉
_r4, _enc85, _ratio85 = _run_compress('PNG-lv6', _srcs['PNG-lv6'], '网络压缩 (85%)')
check('85% 档不受 FLEVEL 影响（要做量化，真能压小）', _enc85 and _ratio85 < 1.0,
      '%.2fx 编码=%s' % (_ratio85, _enc85))
check('  压不小时护栏保留原文件', _ratio <= 1.0, '%.2fx' % _ratio)

# 85% 档：**WebP 一律不跳**。真实素材里 44 张有损 WebP 有 23 张按 85% 重编码
# 真能省 ≥5%（压到 0.32~0.50 倍），而 bpp 预测不了谁能省 —— bpp 0.79 的压到
# 0.47，bpp 3.12 的压到 0.34。阈值取 0.5/0.8/1.0 分别误跳过 3/4/5 张，
# 怎么调都在误伤。q85 本身就够狠，交给体积护栏兜底。
for _label in ('WebP q95', 'WebP q75', 'PNG'):
    _r2, _enc, _ratio = _run_compress(_label, _srcs[_label], '网络压缩 (85%)')
    check('%s 85%% 档照常处理（不跳）' % _label, _enc,
          '%.2fx 编码=%s' % (_ratio, _enc))

shutil.rmtree(tmp, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
