"""与业务无关的纯函数。"""

# ============================================================
# 【导读】utils.py —— 与业务无关的小工具：路径处理、拖拽数据解析、多路径解析、
# 临时文件名、自然排序（让 2.jpg 排在 10.jpg 前面）。
# ============================================================
import os
import re
import sys
import uuid
from typing import List, Optional

from .config import IS_WINDOWS


def parse_source_paths(text: str):
    """把地址栏里的一串文本拆成多条源路径。

    批量整理经常要同时处理好几个文件夹，一次只能填一条太别扭。分号和换行都
    认（Windows 复制多个路径出来常常带引号，一并去掉）。拆完只保留真实存在
    的目录；一条都不剩就返回空列表，交给调用方按「单路径」老路处理。
    """
    import os as _os
    if not text:
        return []
    parts = []
    for chunk in re.split(r'[;\r\n]+', text):
        p = chunk.strip().strip('"').strip("'")
        if p and _os.path.isdir(p):
            parts.append(_os.path.abspath(p))
    return list(dict.fromkeys(parts))


def parse_drop_paths(data: str) -> List[str]:
    """
拆解 tkdnd 传来的路径列表。它给的是 Tcl 列表：只有含空格的项会被 {} 包起来，
其余裸写。原来用「整串是否以 { 开头并以 } 结尾」二选一地判断，混合时会把
{C:/My Photos/a.jpg} C:/b.jpg 按空格切碎，含空格的路径就被静默丢掉了。
"""
    paths = []
    buf = ''
    in_brace = False

    for ch in data:
        if in_brace:
            if ch == '}':
                paths.append(buf)
                buf = ''
                in_brace = False
            else:
                buf += ch
        elif ch == '{' and not buf:
            in_brace = True
        elif ch.isspace():
            if buf:
                paths.append(buf)
                buf = ''
        else:
            buf += ch

    if buf:
        paths.append(buf)

    return [p for p in (x.strip() for x in paths) if p]


def natural_sort_key(path: str):
    """
自然排序键：让 1, 2, 10 按数值排序而非字符串排序
例如: 1.jpg, 2.jpg, 10.jpg 而不是 1.jpg, 10.jpg, 2.jpg
"""
    filename = os.path.basename(path)

    parts = re.split(r'(\d+)', filename)

    # 用 isdecimal 而不是 isdigit：像 '²' 这种上标 isdigit() 为真、却不被 \d
    # 匹配，会落到非数字分片里，int() 直接抛 ValueError 让整次扫描失败。
    return [int(p) if p.isdecimal() else p.lower() for p in parts]


def parse_digit_count(digit_str: str):
    """
从位数选项字符串中解析数字
例如: "3位 (001)" -> 3, "10位" -> 10
选了「自动」时返回 None，由 build_tasks 按这一批的文件总数算出宽度。
"""
    if digit_str and digit_str.startswith('自动'):
        return None
    match = re.match(r'(\d+)', digit_str)
    if match:
        return int(match.group(1))
    return 3


def long_path(path: str) -> str:
    """Windows 长路径支持"""
    if not IS_WINDOWS or path.startswith('\\\\?\\'):
        return path

    abs_path = os.path.abspath(path)
    # 长度要按解析后的绝对路径算：相对路径本身可能很短，展开后却超限。
    if len(abs_path) <= 240:
        return path

    # UNC 网络路径要写成 \\?\UNC\服务器\共享\...，直接加前缀会得到非法路径。
    if abs_path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + abs_path[2:]
    return '\\\\?\\' + abs_path


def get_resource_path(relative_path):
    """获取资源的绝对路径，兼容开发环境和 PyInstaller 打包环境"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath('.'), relative_path)


INSTANCE_ENV = 'IMGWB_INSTANCE'


def _is_mp_worker() -> bool:
    """当前进程是不是 `multiprocessing` 起的工作子进程。

    **不能只问 `multiprocessing.parent_process()`。** spawn 出来的子进程是先
    重新 import 调用方的主模块、之后才把"父进程是谁"登记上的 —— 调用方的
    脚本在顶层 `import imgwb` 的话，这个函数被调用时那一项还是 None，
    子进程就会被当成一个新实例（`tests/test_tempname.py` 第五节就是这么
    抓到的：工人和主进程的标记对不上）。
    那段时间里能认出子进程的是另外两个迹象：进程对象上的 `_inheriting`，
    和命令行里 multiprocessing 自己加的 `--multiprocessing-fork`
    （打包版的工作进程也带着它）。三个都看。
    """
    import multiprocessing

    try:
        if multiprocessing.parent_process() is not None:
            return True
        if getattr(multiprocessing.current_process(), '_inheriting', False):
            return True
    except Exception:
        pass
    return '--multiprocessing-fork' in sys.argv


def _instance_tag() -> str:
    """这一次启动的标记（6 位十六进制），写进每个临时文件名里。

    **为什么需要它**：清扫残留只能按文件名认，而用户可以同时开两个窗口、
    导出到同一个文件夹。没有标记的话，一个窗口点取消触发的清扫会把另一个
    窗口**正等着提交**的临时文件一起删掉 —— 那边提交时发现临时文件没了，
    整批产物作废（原文件不受影响，但白跑了）。带上标记之后各扫各的。

    **工作子进程必须和主进程用同一个标记**，否则主进程认不出自己工人留下的
    残骸。所以标记放在环境变量里：主进程每次启动生成一个新的，
    `multiprocessing` 起的子进程继承环境变量、直接沿用。
    不能只看环境变量在不在 —— 用户从一个跑着本程序的命令行里再起一个，
    也会继承到那个变量，而那是另一个独立的实例，得有自己的标记。
    所以要先判断"我是不是 multiprocessing 起的工作子进程"，见 `_is_mp_worker`。
    """
    tag = os.environ.get(INSTANCE_ENV, '')
    if not (_is_mp_worker() and re.fullmatch(r'[0-9a-f]{6}', tag)):
        tag = uuid.uuid4().hex[:6]
        os.environ[INSTANCE_ENV] = tag
    return tag


INSTANCE_TAG = _instance_tag()

# 认不出"在哪台机器、哪个登录会话"时用的范围标记。带着它的临时文件，
# 任何别的实例都判断不了它的主人是死是活，所以**谁都不会去删**。
UNKNOWN_SCOPE = '0000'


def _scope_tag() -> str:
    """这台机器 + 这个登录会话的标记（4 位十六进制），也写进临时文件名。

    "主人还活着吗"是靠一个命名互斥体查的（见 `_hold_instance_mutex`），
    而那种系统对象**只在同一台机器、同一个登录会话里看得见**。导出目录
    可能是网络共享，另一台机器上的实例正在往里写 —— 从这边查不到它的
    互斥体，绝不能因此当它死了。所以先比这个范围标记：对不上的一律不碰。
    """
    if not IS_WINDOWS:
        return UNKNOWN_SCOPE
    try:
        import ctypes
        import hashlib
        from ctypes import wintypes

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        session = wintypes.DWORD()
        if not k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            return UNKNOWN_SCOPE
        host = os.environ.get('COMPUTERNAME', '')
        if not host:
            return UNKNOWN_SCOPE
        digest = hashlib.sha1(
            f'{host.lower()}|{session.value}'.encode('utf-8')).hexdigest()[:4]
        return digest if digest != UNKNOWN_SCOPE else 'ffff'
    except Exception:
        return UNKNOWN_SCOPE


def _mutex_name(scope: str, tag: str) -> str:
    return f'Local\\imgwb-instance-{scope}-{tag}'


_instance_mutex = None


def _hold_instance_mutex(scope: str, tag: str) -> bool:
    """主进程启动时拿住一个以实例标记命名的互斥体，一直拿到进程结束。

    这是"这个实例还活着"的**凭据**：进程不管怎么死（正常退出、崩溃、被强杀），
    系统都会把它的句柄收回，互斥体随之消失 —— 不用我们自己清理，
    也不会留下一个永远"占着"的陈旧锁。句柄故意不关。

    只有主进程拿。工作子进程是跟着主进程走的：主进程没了，那一批的结果
    就不会再被提交，它们留下的临时文件就是残骸。
    """
    global _instance_mutex
    if not IS_WINDOWS or scope == UNKNOWN_SCOPE:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        handle = k32.CreateMutexW(None, False, _mutex_name(scope, tag))
        if not handle:
            return False
        _instance_mutex = handle
        return True
    except Exception:
        return False


def instance_alive(scope: str, tag: str) -> bool:
    """带着这个标记的实例**确定已经不在了**才返回 False。

    只有三件事同时成立才敢说"不在了"：范围标记和自己的一样（同一台机器、
    同一个登录会话）、能正常查询、查到的结果是"没有这个互斥体"。
    其余任何情况 —— 别的机器、别的会话、查询出错、权限不够 —— 都返回 True。
    **宁可留垃圾，也不能把别人正在用的临时文件当垃圾。**
    """
    if not IS_WINDOWS or scope == UNKNOWN_SCOPE or scope != SCOPE_TAG:
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                   wintypes.LPCWSTR]
        k32.OpenMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenMutexW(0x00100000, False, _mutex_name(scope, tag))
        if handle:
            k32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() != 2     # ERROR_FILE_NOT_FOUND
    except Exception:
        return True


def _scope_for_this_process() -> str:
    """主进程：算出范围标记并拿住互斥体。拿不到互斥体就退回 UNKNOWN_SCOPE ——
    那样别的实例判断不了我们的死活，也就不会来删我们的临时文件。
    工作子进程：沿用主进程的（同一个实例，同一个凭据）。"""
    if _is_mp_worker():
        inherited = os.environ.get(INSTANCE_ENV + '_SCOPE', '')
        if re.fullmatch(r'[0-9a-f]{4}', inherited):
            return inherited
        return UNKNOWN_SCOPE
    scope = _scope_tag()
    if not _hold_instance_mutex(scope, INSTANCE_TAG):
        scope = UNKNOWN_SCOPE
    os.environ[INSTANCE_ENV + '_SCOPE'] = scope
    return scope


SCOPE_TAG = _scope_for_this_process()


def make_temp_path(dst: str) -> str:
    """与目标同目录的临时文件名。必须保留扩展名，Pillow 靠它推断保存格式。

    长这样：`001.~imgwb` + 6 位实例标记 + 4 位范围标记 + 8 位随机数 + `.jpg`。

    名字里的 `imgwb` 标记**不是装饰**。清扫残留的临时文件时只能按文件名认，
    而原来的格式是「.~ + 8 位十六进制」—— 用户自己要是有个
    `photo.~1a2b3c4d.jpg`，清扫时就会被当成垃圾直接删掉（实测确认会删）。
    加上标记之后，正常文件几乎不可能撞上。实例标记见 `_instance_tag`，
    范围标记见 `_scope_tag`。改名字格式前先看 `tasks.TEMP_PATTERN` 和
    `tasks.temp_owner`。
    """
    base, ext = os.path.splitext(dst)
    return (f'{base}.~imgwb{INSTANCE_TAG}{SCOPE_TAG}'
            f'{uuid.uuid4().hex[:8]}{ext}')


def flush_to_disk(path: str) -> bool:
    """把这个文件的数据强制写到盘上（而不是只停在系统缓存里）。成功返回 True。

    **为什么要有这一步**：原地处理的最后一步是"用新文件顶替原文件"。改名这个
    动作本身 NTFS 是记了日志的，断电后它要么发生、要么没发生；可新文件的
    **数据**默认只是交给了系统缓存，稍后才落盘。在"改名已经记下、数据还没
    落盘"的那个窗口里断电，重启之后原文件的名字下面是一个长度对、内容全空
    的文件 —— 原图就这么没了。先把数据刷到盘上再改名，这个窗口就关掉了。

    代价实测（NVMe SSD）：每个文件多 2.4 ~ 2.8 毫秒，和文件大小基本无关。
    所以只在**会顶替或删掉原文件**的时候做（见 `process_single_image` 里的
    `at_stake`）；导出到别的文件夹时原文件根本不动，不花这个钱。
    机械硬盘上会明显更贵，没量过。

    尽力而为：有的网络盘、虚拟盘不支持这个调用，失败了不影响主流程。
    """
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, 'O_BINARY', 0))
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


# 删掉一个临时文件；删不掉（比如已经不存在）也不报错。
def discard_temp(tmp: Optional[str]):
    if tmp:
        try:
            os.remove(tmp)
        except OSError:
            pass


# Windows 的保留设备名。做成文件名会被系统当设备处理，写出来的东西进不了磁盘。
_RESERVED_NAMES = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL']
    + [f'COM{i}' for i in range(1, 10)]
    + [f'LPT{i}' for i in range(1, 10)])

# 文件名里不许出现的字符。斜杠和反斜杠是分隔符，其余是 Windows 不接受的。
_BAD_CHARS = '<>:"|?*' + '\/'


def name_segment_error(value: str, label: str):
    """校验"前缀""目录后缀"这类**单个名字片段**。有问题返回一句话，没问题返回 None。

    为什么必须校验：这些值会被直接拼进路径 ——
    `os.path.join(导出目录, f'{前缀}_{编号}{扩展名}')`。
    Windows 上 `os.path.join` 遇到绝对路径会**丢掉前面所有部分**，于是一个
    `D:\别人的目录\victim` 这样的前缀能让产物落到导出目录之外，
    把源图直接覆盖掉，而程序一路报成功（已复现）。

    所以这里不做"清洗后凑合用"，而是直接报错 —— 悄悄改掉用户填的名字，
    用户会以为自己填的生效了。
    """
    if value is None:
        return None
    if value != value.strip():
        return f'{label}前后不能有空格'
    if not value:
        return None                     # 空前缀是允许的（等于不加前缀）
    bad = sorted({c for c in value if c in _BAD_CHARS})
    if bad:
        return (f'{label}不能包含这些字符：{" ".join(bad)}'
                f'（它是一个名字，不是路径）')
    if os.path.isabs(value) or (len(value) > 1 and value[1] == ':'):
        return f'{label}不能是绝对路径'
    if value in ('.', '..') or value.startswith('..'):
        return f'{label}不能是 . 或 ..'
    if value.split('.')[0].upper() in _RESERVED_NAMES:
        return f'{label}「{value}」是 Windows 的保留设备名，换一个'
    if value.endswith('.'):
        return f'{label}不能以句点结尾（Windows 会把它悄悄去掉）'
    return None


def path_escapes(dst_path: str, allowed_dir: str) -> bool:
    """`dst_path` 是否落在 `allowed_dir` 之外。最后一道兜底检查。

    前面已经校验过前缀和后缀了，这里是防"还有别的路径没想到"——
    只要最终路径不在它该在的目录里，就是出事了，宁可报错也不要写出去。
    """
    base = os.path.normcase(os.path.abspath(allowed_dir))
    target = os.path.normcase(os.path.abspath(dst_path))
    return not (os.path.dirname(target) == base)


def path_inside(child: str, parent: str) -> bool:
    """`child` 是不是**严格**在 `parent` 里面。两边都要先规范化好再传进来
    （`normcase` + `abspath`/`realpath`），这里只比字符串。

    **不能写成 `child.startswith(parent + os.sep)`。** 盘符根目录 `D:\\` 和
    UNC 共享根 `\\\\server\\share\\` 规范化之后**自己就带着结尾的分隔符**，
    再拼一个就成了 `D:\\\\`，任何正常路径都匹配不上 —— 于是来源是整个盘时，
    「导出不能落在来源里」「清空不能删到来源」这几道保护全部静默失效
    （`D:\\` 作来源时三处判断都放行）。
    """
    if not child or not parent or child == parent:
        return False
    base = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(base)


def same_file(a: str, b: str) -> bool:
    """两个路径是不是指向同一个文件。

    不能只比字符串：大小写、8.3 短名（`PROGRA~1`）、junction 和 symlink
    都能让同一个文件有好几种写法。`os.path.samefile` 要求两边都真实存在，
    它抛了就退回规范化路径比对。
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return (os.path.normcase(os.path.abspath(a))
                == os.path.normcase(os.path.abspath(b)))
