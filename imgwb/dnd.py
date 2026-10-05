"""拖拽支持探测。

单独成模块是为了让 ui 和 app 共用同一份判断，同时不把 tkinterdnd2 拖进
config —— pipeline 依赖 config，而它要在子进程里保持轻量。
"""

# ============================================================
# 【导读】dnd.py —— 检测能不能拖拽文件（装了 tkinterdnd2 才行；没装也能正常打开）。
# ============================================================

HAS_DND = False
DND_FILES = None
TkinterDnD = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAS_DND = True
except ImportError:
    pass

__all__ = ['HAS_DND', 'DND_FILES', 'TkinterDnD']
