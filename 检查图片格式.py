"""检查文件夹里的图片：扩展名和真实格式是否一致、有没有动图。

用法:  python 检查图片格式.py "D:\\你的文件夹"
"""
import os
import sys

from PIL import Image

EXT_FORMAT = {
    '.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.bmp': 'BMP',
    '.webp': 'WEBP', '.tif': 'TIFF', '.tiff': 'TIFF', '.gif': 'GIF',
    '.heic': 'HEIF', '.heif': 'HEIF', '.ico': 'ICO',
}
SUPPORTED = ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tiff', '.tif')

folder = sys.argv[1] if len(sys.argv) > 1 else '.'
mismatch, animated, unsupported = [], [], []

for name in sorted(os.listdir(folder)):
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        continue
    ext = os.path.splitext(name)[1].lower()
    try:
        with Image.open(path) as im:
            real, frames = im.format, getattr(im, 'n_frames', 1)
    except Exception:
        continue

    if EXT_FORMAT.get(ext) and real != EXT_FORMAT[ext] and not (
            real == 'MPO' and EXT_FORMAT[ext] == 'JPEG'):
        mismatch.append((name, ext, real))
    if frames > 1 and real != 'MPO':
        animated.append((name, real, frames))
    if ext not in SUPPORTED:
        unsupported.append((name, real))

print(f'扫描目录: {os.path.abspath(folder)}\n')

if mismatch:
    print('⚠️ 扩展名与真实格式不符（工具按扩展名判断，这类文件会出问题）:')
    for name, ext, real in mismatch:
        print(f'   {name}  扩展名说是 {ext}，实际是 {real}')
else:
    print('✅ 没有扩展名与真实格式不符的文件')

print()
if animated:
    print('🎞️ 多帧图像（动图 / 多页），处理时会被跳过并在错误列表里列出:')
    for name, real, frames in animated:
        print(f'   {name}  {real}  {frames} 帧')
else:
    print('✅ 没有多帧图像')

print()
if unsupported:
    print('⏭️ 不在支持列表里、会被跳过的文件:')
    for name, real in unsupported:
        print(f'   {name}  (真实格式 {real})')
else:
    print('✅ 没有会被跳过的图片')
