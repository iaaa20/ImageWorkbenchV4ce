# -*- coding: utf-8 -*-
"""核对许可文本有没有真的打进 exe。

用法:  python 检查打包许可.py [exe路径]
       不给路径就用 dist\\ImageWorkbenchV4ce.exe。
退出码 0 = 材料一致及所列检查通过，1 = 缺失或不一致；不代表完整发布合规。

**为什么需要这个工具：改 spec 很容易自以为生效了。** 发布一个二进制而里面
没有许可文本，就不满足 GPL/LGPL 的随附条件 —— 而这件事光看 spec 看不出来，
得真去读打出来的那个 exe 的清单。顺带也把那几个 HEIC 原生库是不是还在包里
一起报出来：改了 `INCLUDE_HEIC` 之后，该附哪些许可是跟着变的。

清单和版本考据见《第三方许可声明.md》。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DEFAULT_EXE = os.path.join(HERE, 'dist', 'ImageWorkbenchV4ce.exe')
LIC_DIR = os.path.join(HERE, 'licenses')

# 这几个在包里就说明 HEIC 支持开着，对应的 GPL/LGPL 文本一个都不能少
HEIC_LIBS = ('libx265', 'libheif', 'libde265', '_pillow_heif')
HEIC_REQUIRED = ('GPL-2.0.txt', 'LGPL-3.0.txt', 'GPL-3.0.txt')


def read_toc(exe):
    from PyInstaller.archive.readers import CArchiveReader
    return list(CArchiveReader(exe).toc)


def main(argv):
    exe = argv[0] if argv else DEFAULT_EXE
    if not os.path.isfile(exe):
        print(f'找不到 exe: {exe}')
        return 2
    from pathlib import Path
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(exe)
    names = list(archive.toc)
    # 目录版的资料在 _internal，入口归档中没有这些数据文件。
    internal = Path(exe).resolve().parent / '_internal'
    is_onedir = ('pyi-contents-directory _internal' in archive.options
                 and not any(entry[-1] == 'b' for entry in archive.toc.values()))
    if is_onedir:
        names += [p.relative_to(internal).as_posix()
                  for p in internal.rglob('*') if p.is_file()]

    def packed_bytes(name):
        if is_onedir:
            return (internal / name.replace('\\', '/')).read_bytes()
        return archive.extract(name)
    print(f'exe: {exe}')
    print(f'打包清单: {len(names)} 项\n')

    packed = {}
    for n in names:
        parts = re.split(r'[\\/]', n)
        if parts[0].lower() == 'licenses' and len(parts) > 1:
            packed[parts[-1]] = n

    on_disk = sorted(f for f in os.listdir(LIC_DIR)
                     if os.path.isfile(os.path.join(LIC_DIR, f))) \
        if os.path.isdir(LIC_DIR) else []
    # LICENSE 和声明文件不在 licenses/ 下，但也会被打进去
    root_docs = {'LICENSE', '第三方许可声明.md', '发布与重建.md'}
    expect = set(on_disk) | root_docs

    missing = sorted(expect - set(packed))
    extra = sorted(set(packed) - expect)

    print(f'打进 exe 的许可文件：{len(packed)} 个')
    for k in sorted(packed):
        print(f'   ✓ {k}')
    if extra:
        print('\n包里有、本地没有的（可能是手工加的）：')
        for k in extra:
            print(f'   ? {k}')

    bad = False
    for name in sorted(expect & set(packed)):
        local = os.path.join(HERE if name in root_docs else LIC_DIR, name)
        if packed_bytes(packed[name]) != Path(local).read_bytes():
            bad = True
            print(f'\n✗ 内容与本地不同（旧声明或许可）: {name}')
    if missing:
        bad = True
        print(f'\n✗ 本地有、但没打进 exe 的 {len(missing)} 个：')
        for k in missing:
            print(f'   - {k}')
        print('  → 检查 spec 里的 LICENSE_FILES，然后重新打包。')

    # HEIC 开着的话，那三份必须在
    heic_on = [n for n in names
               if any(k in os.path.basename(n).lower() for k in HEIC_LIBS)]
    print(f'\nHEIC 相关原生库：{len(heic_on)} 个'
          + ('（HEIC 支持是开着的）' if heic_on else '（没打进来，HEIC 是关的）'))
    for n in sorted(heic_on):
        print(f'   {os.path.basename(n)}')
    if heic_on:
        lack = [f for f in HEIC_REQUIRED if f not in packed]
        if lack:
            bad = True
            print('\n✗ HEIC 开着，但这几份许可正文没在包里：')
            for f in lack:
                print(f'   - {f}')
            print('  → x265 是 GPL-2.0-or-later，libheif / libde265 是 LGPL-3.0，'
                  'LGPLv3 又引用 GPLv3，三份都得附。')
        else:
            print('   对应的 GPL-2.0 / LGPL-3.0 / GPL-3.0 正文都在包里 ✓')

    # 通用 C 运行时不该在包里。Windows 10 起系统自带，按本项目 spec 在正常环境
    # 打包不会收进来；收进来说明打包环境不对（2026-10-04 有一次在另一个执行
    # 环境里打的包多收了 43 个，清单 1063 项而不是 1020）。那批文件是从打包机
    # 系统目录拿的，不是微软的可再分发包，声明文件里也没有它们 —— 所以算有问题。
    stray = sorted(os.path.basename(n) for n in names
                   if os.path.basename(n).lower().startswith(
                       ('api-ms-win-', 'ucrtbase')))
    if stray:
        bad = True
        print(f'\n✗ 包里多出 {len(stray)} 个系统运行库文件'
              f'（{stray[0]} 等）—— 打包环境不对。')
        print('  → 在本机正常环境里用 `python 完整自检.py` 重新打包。')
    else:
        print('\n没有混进系统运行库（ucrtbase / api-ms-win-*）✓')

    print('\n' + ('有问题，见上面的 ✗' if bad else '材料一致性检查通过（不代表完整发布合规）'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
