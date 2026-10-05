"""python -m imgwb ... 的入口。

多进程模式下 Windows 会 spawn 子进程重新导入这个模块，freeze_support()
必须在最前面挡一道。
"""
import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()

    from .cli import main

    sys.exit(main())
