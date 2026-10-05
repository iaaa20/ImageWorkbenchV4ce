"""最长边上限：只缩不放，而且不能被别的机制偷偷撤销。

缩尺寸是所有压缩手段里省得最多的一招（实测 4000x3000 存 JPEG q90：
原尺寸 1377 KB，限到 1920 只剩 193 KB，0.14 倍）。但它会真的改变像素，
所以要盯住三件事：

  1. 只缩不放 —— 小图必须原样不动
  2. 那几条"为了快而跳过处理"的捷径都得给它让路：
     解码前跳过、多帧复制捷径、内存闸门记 0
  3. 体积护栏不能把缩放一起撤销（跟烘焙 EXIF 方向是同一类坑）

用法:  python tests\\test_maxedge.py
不建窗口。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image

from imgwb.config import MAX_EDGE_CHOICES, ProcessConfig
from imgwb.pipeline import (oversized, probe_peak, process_single_image,
                            shrink_to)

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


tmp = tempfile.mkdtemp()


def photo(w, h, seed=3):
    import random
    random.seed(seed)
    im = Image.new('RGB', (w, h))
    p = im.load()
    for y in range(h):
        for x in range(w):
            n = random.randint(-10, 10)
            p[x, y] = (max(0, min(255, int(200 * y / h) + n)),
                       max(0, min(255, 128 + (x * 31 // w) + n)),
                       max(0, min(255, 90 + ((x + y) * 23 // (w + h)) + n)))
    return im


def cfg(**kw):
    base = dict(rotate_mode='不旋转', quality_opt='通用高清 (95%)',
                compress_only=True, is_ico=False, max_edge=1920)
    base.update(kw)
    return ProcessConfig(**base)


def run(src, dst, c):
    return process_single_image((src, dst, c, False, False))


print('=== 1. 纯函数：只缩不放 ===')
check('4000x3000 -> 1920', shrink_to(photo(80, 60), 1920).size == (80, 60)
      and shrink_to(Image.new('RGB', (4000, 3000)), 1920).size == (1920, 1440))
check('小图原样不动', shrink_to(Image.new('RGB', (800, 600)), 1920).size == (800, 600))
check('0 表示不限', shrink_to(Image.new('RGB', (4000, 3000)), 0).size == (4000, 3000))
check('细长条不会被缩成 0 宽',
      min(shrink_to(Image.new('RGB', (10000, 3)), 1920).size) >= 1,
      str(shrink_to(Image.new('RGB', (10000, 3)), 1920).size))
check('oversized 判断', oversized((4000, 3000), 1920) is True
      and oversized((800, 600), 1920) is False
      and oversized((4000, 3000), 0) is False)

print('\n=== 2. 端到端：大图缩、小图不动 ===')
big = os.path.join(tmp, 'big.jpg'); photo(3000, 2000).save(big, quality=95)
small = os.path.join(tmp, 'small.jpg'); photo(800, 600).save(small, quality=95)

out = os.path.join(tmp, 'o_big.jpg')
r = run(big, out, cfg())
with Image.open(out) as im:
    got = im.size
check('大图缩到 1920', got == (1920, 1280), str(got))
check('  结果里带出了 resized', r.resized is True)
check('  体积确实小了很多',
      os.path.getsize(out) < os.path.getsize(big) * 0.6,
      f'{os.path.getsize(big)/1024:.0f}K -> {os.path.getsize(out)/1024:.0f}K '
      f'({os.path.getsize(out)/os.path.getsize(big):.2f}x)')

out = os.path.join(tmp, 'o_small.jpg')
r = run(small, out, cfg())
with Image.open(out) as im:
    check('小图尺寸不变', im.size == (800, 600), str(im.size))
check('  小图不报 resized', r.resized is False)

print('\n=== 3. 那几条捷径都要给它让路 ===')
# 捷径 A：解码前跳过（源画质 <= 目标）。超尺寸时必须照常处理。
q60 = os.path.join(tmp, 'q60.jpg'); photo(3000, 2000).save(q60, quality=60)
out = os.path.join(tmp, 'o_q60.jpg')
r = run(q60, out, cfg())
with Image.open(out) as im:
    check('已是最优但超尺寸 -> 仍然缩', im.size == (1920, 1280), str(im.size))
check('  没有被当成"保留原文件"', r.kept_original is False)

# 不限尺寸时它应该照旧跳过 —— 别把优化改坏了
r = run(q60, os.path.join(tmp, 'o_q60b.jpg'), cfg(max_edge=0))
check('不限尺寸时照旧跳过', r.kept_original is True)

# 捷径 B：多帧图不旋转时原样复制。超尺寸时必须逐帧缩。
frames = [photo(2400, 1800, seed=i) for i in range(3)]
anim = os.path.join(tmp, 'a.webp')
frames[0].save(anim, save_all=True, append_images=frames[1:], duration=100,
               loop=0, quality=80, method=4)
out = os.path.join(tmp, 'o_a.webp')
r = run(anim, out, cfg(quality_opt='网络压缩 (85%)'))
with Image.open(out) as im:
    check('动图超尺寸 -> 逐帧缩', im.size == (1920, 1440), str(im.size))
    check('  帧数一帧不少', getattr(im, 'n_frames', 1) == 3,
          f'{getattr(im, "n_frames", 1)} 帧（源 3 帧）')

# 捷径 C：内存闸门。会缩尺寸的任务要照常记账，不能记 0。
mb_limited, _ = probe_peak(q60, cfg())
mb_free, _ = probe_peak(q60, cfg(max_edge=0))
check('超尺寸任务在闸门里照常记账', mb_limited > 0,
      f'限尺寸 {mb_limited:.0f} MB / 不限 {mb_free:.0f} MB（不限时因为会跳过所以记 0）')
check('  不限时才记 0', mb_free == 0.0, f'{mb_free:.0f} MB')

print('\n=== 4. 体积护栏不能把缩放一起撤销 ===')
# 造一张"缩了之后反而更大"很难，所以直接验：缩过的一律不进护栏。
# 用 100% 档 + 已经压得很狠的源，重存后极可能变大。
tiny_q = os.path.join(tmp, 'tinyq.jpg'); photo(3000, 2000).save(tiny_q, quality=30)
out = os.path.join(tmp, 'o_tiny.jpg')
r = run(tiny_q, out, cfg(quality_opt='极致保真 (100%)'))
with Image.open(out) as im:
    check('缩过尺寸的产物不会被护栏还原', im.size == (1920, 1280), str(im.size))
check('  也不会被记成"保留原文件"', r.kept_original is False)

print('\n=== 5. 档位表 ===')
check('档位包含 0=不限', dict(MAX_EDGE_CHOICES).get('不限 (保持原尺寸)') == 0)
check('档位是从大到小的像素值',
      [px for _l, px in MAX_EDGE_CHOICES] == [0, 3840, 2560, 1920, 1280],
      str([px for _l, px in MAX_EDGE_CHOICES]))

shutil.rmtree(tmp, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
