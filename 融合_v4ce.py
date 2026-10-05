"""图片处理工作台 —— 启动入口。

这个文件刻意保持极薄。多进程模式下 Windows 用 spawn 启动子进程，会把入口
模块重新 import 一遍；把 GUI 的 import 放进 main() 里，子进程就只加载
imgwb.pipeline 那一条链，不用再把 ttkbootstrap / tkinter / tkinterdnd2
整套装一遍。程序主体见 imgwb 包。
"""
import multiprocessing
import sys


def enable_dpi_awareness():
    if sys.platform != 'win32':
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def other_instance_running():
    """已经有一个实例在跑吗？

    不拦着多开 —— 同时跑两批是正当用法。但两个窗口长得一模一样，而它们的
    能力未必相同（一个装了 pillow-heif、一个没装），出了矛盾行为会找不到原因。
    所以检测到多开就在标题里标出来。

    用具名互斥体而不是 pid 文件：进程被强杀时互斥体由系统自动释放，不会留下
    一个永远"占着"的陈旧锁。句柄故意不关，让它活到进程结束。
    """
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        from ctypes import wintypes

        ERROR_ALREADY_EXISTS = 183
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        handle = k32.CreateMutexW(None, False, 'imgwb_v4ce_single_instance')
        if not handle:
            return False
        globals()['_instance_mutex'] = handle      # 别让它被回收
        return ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    except Exception:
        return False


def missing_dependencies():
    from importlib.util import find_spec

    return [name for name, mod in (('pillow', 'PIL'), ('ttkbootstrap', 'ttkbootstrap'))
            if find_spec(mod) is None]


def run_cli(argv):
    """`程序 --cli ...`：不开窗口，直接走命令行接口（和 `python -m imgwb` 同一套）。

    给打包版用的。exe 里装着整套处理代码，却原来只有图形界面这一个入口 ——
    想让脚本调用、或者想**验证打包出来的东西真的能处理图片**（而不只是
    "窗口能打开"），都没有办法。`完整自检.py` 就是靠这个入口让 exe 真跑一批图的。

    打包版是不带控制台的，`sys.stdout` / `sys.stderr` 是 None，直接打印会崩。
    所以支持 `--cli-out 文件`：把输出写进文件。没给就丢弃。
    """
    import os

    out_path = None
    if '--cli-out' in argv:
        at = argv.index('--cli-out')
        if at + 1 >= len(argv):
            return 2
        # 现在就定成绝对路径：它既是下面要打开的文件，也是要告诉扫描
        # "这个不是输入"的那个文件，两处必须是同一个。
        out_path = os.path.abspath(argv[at + 1])
        argv = argv[:at] + argv[at + 2:]
    opened = []
    try:
        if out_path:
            sys.stdout = open(out_path, 'w', encoding='utf-8')
            opened.append(sys.stdout)
        elif sys.stdout is None:
            sys.stdout = open(os.devnull, 'w', encoding='utf-8')
            opened.append(sys.stdout)
        if sys.stderr is None:
            sys.stderr = open(os.devnull, 'w', encoding='utf-8')
            opened.append(sys.stderr)
        from imgwb import cli

        try:
            return cli.main(argv, reserved=[out_path] if out_path else None)
        except SystemExit as exc:           # argparse 用法错误会走这里
            return exc.code if isinstance(exc.code, int) else 2
    finally:
        for fh in opened:
            try:
                fh.close()
            except Exception:
                pass


def run_selftest(argv):
    """`程序 --selftest`：在这台机器上把自己真跑一遍，见 `imgwb/selftest.py`。

    界面那一半用的就是下面的 `start_app` —— 和双击启动是同一段启动流程，
    自测不另起一套，否则测的就不是用户会走到的那条路。
    """
    from imgwb import selftest

    return selftest.run(selftest.parse(argv), start_app)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--cli':
        return run_cli(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == '--selftest':
        return run_selftest(sys.argv[2:])

    missing = missing_dependencies()
    if missing:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        # 这个弹窗得在 imgwb 其它模块之前就能弹出来 —— i18n 只用标准库，
        # 所以它是唯一可以在依赖检查阶段安全导入的模块。
        from imgwb import i18n

        i18n.set_lang(i18n.load_pref() or i18n.system_default())
        messagebox.showerror(
            i18n.tr('缺少依赖'),
            i18n.tr('缺少以下库:') + '\n    ' + '\n    '.join(missing)
            + '\n\n' + i18n.tr('请先安装:') + '\n    pip install '
            + ' '.join(missing))
        return 1

    app = start_app()
    try:
        app.root.mainloop()
    finally:
        # 最后一道保险：不管 mainloop 是正常结束还是异常退出，都别把工作
        # 子进程留在后台。它们是独立进程，父进程死了也不会自己走。
        app.shutdown_workers()
    return 0


def start_app(lang=None, scale=None):
    """启动流程：定语言、开日志、设 DPI、建窗口。返回建好的 app，不进主循环。

    `lang` / `scale` 只有自测会传（指定界面语言、模拟界面缩放）；
    正常启动两个都是 None，行为和以前完全一样。
    """
    # 日志要在建窗口之前就位：依赖检查、DPI、界面构建这几步出问题的话，
    # windowed 模式下连 stderr 都没有，用户只看到窗口凭空消失。
    # 界面语言要在**写第一行日志之前**定下来 —— logfile.banner() 也走翻译，
    # 晚一步的话日志开头那几行（启动、版本、路径）会固定是中文，
    # 后面才变英文。打包版验证时就是这么发现的。
    from imgwb import i18n

    i18n.set_lang(lang or i18n.load_pref() or i18n.system_default())

    from imgwb import logfile

    logfile.setup()
    logfile.install_hooks()
    logfile.banner()

    enable_dpi_awareness()

    second = other_instance_running()

    from imgwb.app import ImageProcessorApp

    if scale:
        ImageProcessorApp.FORCE_UI_SCALE = float(scale)
    app = ImageProcessorApp()
    if second:
        # 标题里标明这是第几个窗口，顺便把可选能力写出来 —— 两个实例的
        # HEIC/拖拽支持可能不一样，行为对不上时至少看得出区别。
        from imgwb.config import HEIC_SUPPORTED
        from imgwb.dnd import HAS_DND

        caps = f"HEIC {'✓' if HEIC_SUPPORTED else '✗'} · 拖拽 {'✓' if HAS_DND else '✗'}"
        app.root.title(i18n.tr(f'图片处理工作台 V4ce（另一个窗口也开着 · {caps}）'))
        app.log('ℹ️ 检测到已经有一个窗口在运行。两个窗口各跑各的，'
                '但如果它们处理同一批文件会互相覆盖。')
    return app


if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main())
