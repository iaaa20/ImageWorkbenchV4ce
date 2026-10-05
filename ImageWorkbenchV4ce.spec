# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

    pyinstaller ImageWorkbenchV4ce.spec

比默认打包小一截，靠的是把程序用不到的东西剔出去。每一条排除下面都写了
理由 —— 想加新排除项之前，先确认它不在运行路径上，然后跑一遍 tests/。
"""
import os

from PyInstaller.utils.hooks import collect_data_files

PROJECT = os.path.abspath(SPECPATH)
ICON = os.path.join(PROJECT, 'logo.ico')

# ttkbootstrap 2.x 启动时要加载 assets/icons/bootstrap.ttf 来生成图标，而
# PyInstaller 默认不收集这个包的资源文件。少了它一建窗就是：
#     FileNotFoundError: ...ttkbootstrap\assets\icons\bootstrap.ttf
# 注意这跟下面的排除项无关 —— 不加这一行的话，连完全不做排除的默认打包也一样缺。
TTKB_ASSETS = collect_data_files('ttkbootstrap')

# ---------------------------------------------------------------- 许可文本
# **这些必须打进 exe。** 打包版把 Pillow、Tcl/Tk 以及 HEIC 用的几个原生库
# （x265 是 GPL-2.0-or-later，libheif / libde265 是 LGPLv3）整个塞进了一个可执行
# 文件。那几个许可要求：分发二进制时要随附许可文本、说明用了哪些组件、
# 并提供组合程序的对应源码。完整范围及下载安排见《发布与重建.md》。
#
# 清单和版本考据见项目根目录的《第三方许可声明.md》；
# 核对打进去没有，跑 `python 检查打包许可.py`。
LICENSE_FILES = [
    (os.path.join(PROJECT, 'LICENSE'), 'licenses'),
    (os.path.join(PROJECT, '第三方许可声明.md'), 'licenses'),
    (os.path.join(PROJECT, '发布与重建.md'), 'licenses'),
]
_lic_dir = os.path.join(PROJECT, 'licenses')
if os.path.isdir(_lic_dir):
    for _n in sorted(os.listdir(_lic_dir)):
        _p = os.path.join(_lic_dir, _n)
        if os.path.isfile(_p):
            LICENSE_FILES.append((_p, 'licenses'))

# ---------------------------------------------------------------- 排除模块
#
# AVIF：Pillow 12 自带 AVIF 插件，_avif.pyd 一个就 4 MB 出头，是整个包里最大
#       的单项。VALID_EXTENSIONS 里没有 .avif，程序从头到尾不碰它。
#
# ssl / _ssl / _hashlib：这三个带进来 libcrypto-3.dll（约 1.8 MB）。程序不联
#       网、不校验证书。hashlib 少了 _hashlib 会退回 CPython 内置的
#       _sha512 等实现，random 模块照常能用。
#
# 其余都是标准库里明显用不到的部分，以及可能被环境误连带进来的科学计算栈。
EXCLUDES = [
    # 用不到的图像格式插件
    'PIL._avif', 'PIL.AvifImagePlugin',
    # 网络与加密
    #
    # 注意：这里**不能**排除 email。ttkbootstrap 在 import 阶段就会走
    # importlib.metadata，而后者依赖 email 解析包元数据。排掉它的话程序一启动
    # 就是 ModuleNotFoundError: No module named 'email'，而且因为是 windowed
    # 模式，用户只看到一个错误框。
    'ssl', '_ssl', '_hashlib', 'http', 'urllib.request',
    # 数据库
    'sqlite3', '_sqlite3',
    # 开发/调试工具
    'unittest', 'doctest', 'pydoc', 'pdb', 'test', 'lib2to3',
    'setuptools', 'pip', 'pkg_resources',
    # 科学计算栈（本项目没依赖，但环境里若装了容易被误收）
    'numpy', 'pandas', 'matplotlib', 'scipy', 'IPython', 'pytest',
]

# ---------------------------------------------------------------- HEIC 取舍
#
# 改成 False 会连同 pillow-heif 及其一整串原生库（libheif / libde265 /
# libx265 / libstdc++）一起剔掉，能再省约 5 MB，代价是程序不再识别
# .heic/.heif —— 界面「支持格式」那行会显示 HEIC ✗，iPhone 照片扫不到。
# 程序本身不会报错，config.py 里的 try/except ImportError 会自动降级。
#
# 注意：不要试图「只删 libx265、留下解码能力」。看名字像是只管编码，但它是
# _pillow_heif.pyd 的加载期依赖 —— 实测删掉之后整个模块 import 就失败：
#     DLL load failed while importing _pillow_heif
# 结果是 HEIC 支持整个消失，而不是只丢掉编码。
INCLUDE_HEIC = True

EXCLUDE_BINARIES = []
if not INCLUDE_HEIC:
    EXCLUDES += ['pillow_heif', '_pillow_heif']
    EXCLUDE_BINARIES += ['libx265', 'libheif', 'libde265', 'pillow_heif']


a = Analysis(
    [os.path.join(PROJECT, '融合_v4ce.py')],
    pathex=[PROJECT],
    binaries=[],
    datas=[(ICON, '.')] + TTKB_ASSETS + LICENSE_FILES,
    hiddenimports=[],
    excludes=EXCLUDES,
    noarchive=False,
)

a.binaries = [b for b in a.binaries
              if not any(pat in os.path.basename(b[0]).lower()
                         for pat in EXCLUDE_BINARIES)]

pyz = PYZ(a.pure)

# ---------------------------------------------------------------- 两种形态
#
# 默认出**单文件版**（一个 exe，双击就跑，分发最省事）。
#
# 设了环境变量 `IMGWB_ONEDIR=1` 就出**目录版**：exe 和所有依赖是输出目录里
# 一堆独立文件，便于替换接口兼容的修改版库（LGPLv3 §4(d)(1)）。
# 单文件版通过提供对应应用代码和重建材料履行 §4(d)(0)，见《发布与重建.md》。
# 额外提供目录版本身不能替代单文件版条件。
ONEDIR = os.environ.get('IMGWB_ONEDIR') == '1'

if ONEDIR:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,      # 二进制留到 COLLECT 里，做成独立文件
        name='ImageWorkbenchV4ce',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        icon=ICON,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name='ImageWorkbenchV4ce_dir',
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='ImageWorkbenchV4ce',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,      # UPX 会显著拖慢启动，且常被杀软误报，默认不用
        console=False,  # GUI 程序，不要控制台窗口
        icon=ICON,
    )
