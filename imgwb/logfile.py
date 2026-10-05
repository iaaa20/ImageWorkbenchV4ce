"""系统日志：把所有操作、处理记录和异常落到磁盘文件。

界面上那个日志框是**易失**的 —— 关掉窗口就没了。出问题时用户只能截图，
而截图里往往正好缺了要紧的那几行（滚上去了、或者程序已经崩了）。
这里再落一份到磁盘：用户报问题时直接把文件发过来，前因后果都在里面。

三类东西都要进日志：

1. **操作**  —— 选文件夹、拖拽、点了哪个一键按钮、切模式、开始/暂停/取消/
   强制停止/关窗，连同当时的完整参数。
2. **处理记录** —— 界面日志框里出现的每一行都会同步写进来，外加**每一个**
   失败文件（界面上只列前 20 条，文件里一条不落）。
3. **异常** —— 未捕获的异常、后台线程里的异常、以及 Python 的 warning
   （比如 Pillow 的解压炸弹警告，它原本只打到 stderr，GUI 用户根本看不见）。

这个模块不能 import 任何 GUI 库 —— 子进程也可能用到它。
"""

# ============================================================
# 【导读】logfile.py —— 系统日志。
# 所有操作、处理记录、报错（含完整调用栈）都写进 logs 文件夹里的 imgwb.log。
# 遇到问题时把这个文件发过来，基本就能看出发生了什么。
# ============================================================
import logging
import os
import sys
import threading
import warnings
from logging.handlers import RotatingFileHandler
from . import i18n as _i18n

APP_NAME = 'ImageWorkbenchV4ce'
MAX_BYTES = 2 * 1024 * 1024      # 单个文件 2 MB
BACKUP_COUNT = 5                 # 连轮转副本一共留 6 个，最多约 12 MB

_logger = None
_path = None


def _candidate_dirs():
    """按优先级给出可以放日志的目录。

    打包版优先放在 exe 旁边（用户找得到，绿色软件的习惯）；那里不可写
    （Program Files、只读介质、U 盘写保护）就退回用户目录。
    """
    dirs = []
    try:
        if getattr(sys, 'frozen', False):
            dirs.append(os.path.join(os.path.dirname(sys.executable), 'logs'))
        else:
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            dirs.append(os.path.join(here, 'logs'))
    except Exception:
        pass

    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    dirs.append(os.path.join(base, APP_NAME, 'logs'))
    return dirs


def _usable(directory):
    """这个目录能不能真的写进去。**必须实际写一次**才算数 ——
    Windows 上 Program Files 的虚拟化、只读介质、权限限制，
    光看 os.access 是看不出来的。"""
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, '.write_test')
        with open(probe, 'w', encoding='utf-8') as fh:
            fh.write('ok')
        os.remove(probe)
        return True
    except Exception:
        return False


def path():
    """当前日志文件的完整路径；还没初始化就返回 None。"""
    return _path


def setup():
    """初始化日志。可以重复调用，只有第一次真正生效。返回日志文件路径。"""
    global _logger, _path
    if _logger is not None:
        return _path

    logger = logging.getLogger('imgwb')
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for directory in _candidate_dirs():
        if not _usable(directory):
            continue
        target = os.path.join(directory, 'imgwb.log')
        try:
            handler = RotatingFileHandler(
                target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                encoding='utf-8', delay=False)
        except Exception:
            continue
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)-5s %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'))
        logger.addHandler(handler)
        _logger = logger
        _path = target
        break
    else:
        # 一个都写不了也不能让程序挂掉，退化成什么都不做
        logger.addHandler(logging.NullHandler())
        _logger = logger
        _path = None

    return _path


def write(message, level='INFO'):
    """写一行。level: INFO / WARN / ERROR。"""
    # 日志文件跟着界面语言走（用户选的）。直接调 logfile.write 的地方
    # 不经过 app.log()，所以这里也要翻一道。已经是英文的句子匹配不上
    # 任何中文模板，会原样返回，不会被翻两遍。
    message = _i18n.tr(message)
    if _logger is None:
        setup()
    try:
        if level == 'ERROR':
            _logger.error(message)
        elif level == 'WARN':
            _logger.warning(message)
        else:
            _logger.info(message)
    except Exception:
        pass          # 日志自己出问题绝不能影响主流程


def op(action, detail=''):
    """记一次用户操作。跟处理记录区分开，好在日志里一眼看出"人做了什么"。

    **三段分别翻，不要让整行去套模板。** 整行是 `[操作] 动作 | 明细`，
    拿它去匹配的话，模板里的占位符会跨过竖线乱配 —— 踩过一次：
    `[操作] 开始执行 | 120 张` 被 `'{} {} 张'` 吃掉，翻成了
    `[操作] ×开始执行 | 120`。现在 i18n 那边也加了竖线护栏，两道都留着。
    """
    line = _i18n.tr('[操作]') + ' ' + _i18n.tr(action)
    if detail:
        line += ' | ' + _i18n.tr(detail)
    write(line)


def exception(where, exc=None):
    """记一个异常，**带完整调用栈**。

    只写一句 str(exc) 是不够的 —— 报问题的人给不出栈，我们就只能靠猜。
    """
    if _logger is None:
        setup()
    try:
        _logger.error(f'[异常] {where}', exc_info=exc if exc is not None else True)
    except Exception:
        pass


def install_hooks():
    """把"没人接住的异常"和 Python warning 也收进日志。

    没有这一层，崩溃信息只会打到 stderr —— 而 windowed 模式下根本没有
    stderr，用户看到的就是窗口凭空消失，日志里一片空白。
    """
    setup()

    # 主线程里没人接住的异常，统统记进日志文件（带完整调用栈）。
    def hook(exc_type, exc, tb):
        try:
            _logger.error('[未捕获异常] 主线程',
                          exc_info=(exc_type, exc, tb))
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook

    # 后台工作线程里的异常默认只打到 stderr，这里一并收走（3.8+）
    if hasattr(threading, 'excepthook'):
        # 后台线程里没人接住的异常，同样记进日志。
        def thread_hook(args):
            """后台线程里没人接住的异常，记进日志。

            **属性名是 `args.thread`，不是 `args.thread_name`。**
            `threading.excepthook` 给的是
            `ExceptHookArgs(exc_type, exc_value, exc_traceback, thread)`。
            原来写成 `args.thread_name`：取属性先抛 AttributeError，
            又被下面那个 `except Exception` 吞掉 —— 结果原异常**既没进日志、
            也没交回默认处理器**，凭空消失。打包版连 stderr 都没有，
            这类 bug 在用户那边就是"后台突然不动了，日志里一个字没有"。
            （实测 10/10 日志为空。）

            `args.thread` 在解释器关闭阶段可能是 None，所以用 getattr 兜底。
            """
            name = getattr(getattr(args, 'thread', None), 'name', '未知线程')
            try:
                _logger.error(f'[未捕获异常] 线程 {name}',
                              exc_info=(args.exc_type, args.exc_value,
                                        args.exc_traceback))
            except Exception:
                # **钩子自己坏了也不能把异常吞了。** 退回默认行为，
                # 至少还能打到 stderr，而不是整个消失。
                try:
                    _ORIGINAL_THREAD_HOOK(args)
                except Exception:
                    pass
            if os.environ.get('IMGWB_TK_STRICT') == '1':
                # 严格模式（测试跑批）：往 stdout 打一行 FAIL，
                # 让 tests/run_all.py 能把它判成失败。日志是给用户的，
                # 这一行是给测试的，两条路分开。
                print(f'FAIL 线程未捕获异常 {name}: '
                      f'{args.exc_type.__name__}: {args.exc_value}', flush=True)

        _ORIGINAL_THREAD_HOOK = threading.excepthook
        threading.excepthook = thread_hook

    # Pillow 的解压炸弹警告（DecompressionBombWarning）原本只进 stderr，
    # GUI 用户完全看不见：一张 144 MP 的图会被照常解开、吃掉 2.4 GB 内存，
    # 表现为"卡了很久"却没有任何解释。
    original = warnings.showwarning

    # Python 发出的警告也记进日志，方便排查。
    def show(message, category, filename, lineno, file=None, line=None):
        write(f'[警告] {category.__name__}: {message} '
              f'({os.path.basename(str(filename))}:{lineno})', 'WARN')
        try:
            original(message, category, filename, lineno, file, line)
        except Exception:
            pass

    warnings.showwarning = show
    warnings.simplefilter('always', Warning)


def banner(extra=''):
    """程序启动时记一段环境信息。排查问题时第一件事就是看这几行。"""
    setup()
    try:
        from PIL import Image as _Image
        pil = getattr(_Image, '__version__', '?')
    except Exception:
        pil = '未安装'
    write('=' * 60)
    write(f'启动 {APP_NAME}'
          + ('（打包版）' if getattr(sys, 'frozen', False) else '（源码运行）'))
    write(f'Python {sys.version.split()[0]} | Pillow {pil} | {sys.platform}')
    write(f'可执行文件 {sys.executable}')
    if extra:
        write(extra)
    if _path:
        write(f'日志文件 {_path}')
