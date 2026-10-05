"""性能模式 —— 决定用多少并发、以什么优先级去抢系统资源。

批量处理几千张图时，把电脑拖到卡死的往往不是 CPU 而是**磁盘 I/O**：几个工作
进程同时狂读狂写，前台程序（浏览器、资源管理器、各种弹窗）连打开一个文件都要
排队。所以这里除了并发数，还会调进程优先级 —— Windows 的「后台模式」会同时
压低 CPU 和 I/O 优先级，是让系统保持跟手的关键。

这个模块只依赖标准库，子进程会加载它，不能牵扯任何 GUI。
"""

# ============================================================
# 【导读】perf.py —— 「用多大力气跑」。
# worker_count（并发几个）、memory_budget_mb（内存上限）、tasks_per_child（子进程多久换新）、
# priority_of / apply_priority（进程优先级）；
# 下半部分是直接调用 Windows 系统接口的代码：查内存、找子进程、结束进程。
# ============================================================
import os
import sys

IS_WINDOWS = sys.platform == 'win32'

# Windows 优先级常量（winbase.h）
_IDLE = 0x00000040               # IDLE_PRIORITY_CLASS
_BELOW_NORMAL = 0x00004000       # BELOW_NORMAL_PRIORITY_CLASS
_NORMAL = 0x00000020             # NORMAL_PRIORITY_CLASS
_BACKGROUND_BEGIN = 0x00100000   # 连磁盘 I/O 一起降级，仅对自身进程有效

ECO = 'eco'
BALANCED = 'balanced'
TURBO = 'turbo'
CUSTOM = 'custom'

MODES = {
    ECO: {
        'label': '节能',
        'desc': '慢一点，但电脑照常用',
        'priority': 'background',
        'tip': '只用 ¼ 核心，磁盘 I/O 也让给前台。慢，但电脑照常用。',
    },
    BALANCED: {
        'label': '均衡',
        'desc': '默认，兼顾速度和响应',
        'priority': 'below',
        'tip': '一半核心，优先级低于普通程序。日常用这个。',
    },
    TURBO: {
        'label': '高性能',
        'desc': '核心用满，但仍让着前台',
        'priority': 'below',
        'tip': '核心用满，但不提高优先级。电脑空着时最快。',
    },
    CUSTOM: {
        'label': '自定义',
        'desc': '并发数自己定',
        'priority': 'below',
        'tip': '自己填并发数（1~64），优先级同「高性能」。',
    },
}

# 并发上限。每个工作进程都是一个独立的 Python，加载 Pillow 后约几十 MB，
# 而且超过一定数量后瓶颈就从 CPU 转到磁盘，再加只会互相抢。
# 这些上限是为了让同一份代码在 2 核笔记本和 64 核工作站上都表现合理，
# 不是照着某台机器凑的。想突破就用「自定义」。
CAP_ECO = 4
CAP_BALANCED = 8
CAP_TURBO = 32
CAP_CUSTOM = 64


def worker_count(mode, custom=None, cpu=None):
    """按模式算并发数。``cpu`` 只在测试里传，用来模拟不同核数的机器。"""
    cpu = cpu or os.cpu_count() or 4

    if mode == ECO:
        return max(1, min(CAP_ECO, cpu // 4))
    if mode == BALANCED:
        return max(1, min(CAP_BALANCED, cpu // 2))
    if mode == TURBO:
        return max(1, min(CAP_TURBO, cpu))

    try:
        return max(1, min(CAP_CUSTOM, int(custom)))
    except (TypeError, ValueError):
        return max(1, min(CAP_BALANCED, cpu // 2))


# 查这个性能模式对应的进程优先级（节能 / 均衡会降低，把资源让给前台程序）。
def priority_of(mode):
    return MODES.get(mode, MODES[CUSTOM])['priority']


def apply_priority(level):
    """把**当前进程**的优先级降下来。在工作子进程里调用。

    返回实际生效的级别，失败返回 None —— 调用方（和测试）才能判断到底生效没有。

    注意 'background' 生效后 GetPriorityClass 仍然报原来的优先级类：
    PROCESS_MODE_BACKGROUND_BEGIN 是一个独立标志，压低的是有效调度优先级和
    磁盘 I/O 优先级，不改优先级类本身。所以别拿 GetPriorityClass 判断它成没成。
    """
    if not IS_WINDOWS:
        try:
            os.nice(10 if level == 'background' else 5)
            return level
        except (AttributeError, OSError):
            return None

    try:
        k32 = _kernel32()
        handle = k32.GetCurrentProcess()
        if level == 'background':
            if k32.SetPriorityClass(handle, _BACKGROUND_BEGIN):
                return 'background'
            # 有些系统版本不接受后台模式，退回 IDLE（只降 CPU，不降 I/O）
            return 'idle' if k32.SetPriorityClass(handle, _IDLE) else None
        if level == 'below':
            return 'below' if k32.SetPriorityClass(handle, _BELOW_NORMAL) else None
        return 'normal' if k32.SetPriorityClass(handle, _NORMAL) else None
    except Exception:
        # 调不动就算了，顶多是卡一点，不该因此让整个处理失败
        return None


def _kernel32():
    """拿到 kernel32 并声明好函数签名。

    必须显式声明：GetCurrentProcess 返回的是伪句柄 (HANDLE)-1，64 位下不声明
    restype 的话会被 ctypes 按 C int 截断，后面 SetPriorityClass 收到的句柄是
    坏的、调用静默失败 —— 表现就是「设了优先级但一点用没有」。
    """
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetCurrentProcess.argtypes = []
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.SetPriorityClass.restype = wintypes.BOOL
    k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    k32.GetPriorityClass.restype = wintypes.DWORD
    return k32


def _query_image(k32, handle):
    """这个进程句柄跑的是哪个 exe。**查不到返回 None**（不是空串当成"随便"）。"""
    import ctypes
    from ctypes import wintypes

    size = wintypes.DWORD(32768)
    buf = ctypes.create_unicode_buffer(size.value)
    if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return buf.value or None
    return None


def _creation_time(k32, handle):
    """进程的创建时刻（FILETIME，100 纳秒为单位的整数）。查不到返回 None。"""
    import ctypes
    from ctypes import wintypes

    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.GetProcessTimes.restype = wintypes.BOOL
    made, gone, kern, user = (wintypes.FILETIME() for _ in range(4))
    if not k32.GetProcessTimes(handle, ctypes.byref(made), ctypes.byref(gone),
                               ctypes.byref(kern), ctypes.byref(user)):
        return None
    return (made.dwHighDateTime << 32) | made.dwLowDateTime


def process_born(pid):
    """这个 pid **此刻**对应的进程是什么时候创建的。已退出或查不到返回 None。

    记 pid 的时候顺手记下它，杀之前再比一次 —— pid 会被系统回收再分配，
    而"同一个 pid、同一个创建时刻"才是同一个进程。见 `kill_pid` 的 ``born``。
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            return _creation_time(k32, handle)
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def kill_pid(pid, expect_image=None, born=None):
    """按 pid 强杀一个进程，返回是不是真的杀掉了（本来就没在跑返回 False）。

    给「关掉界面后任务管理器里还剩一堆工作进程」兜底用：池子自己那份进程名单
    在收尾时会被清空，只有我们自己记下来的 pid 还认得它们。

    **pid 会被系统回收再分配**，所以动手前有三道校验，任何一道过不了就不杀：

    1. 映像名要和我们自己的解释器（打包后就是 exe 本身）同名；
    2. **映像名查不到也不杀。** 原来查询失败时直接跳过校验往下走 ——
       等于"不知道它是谁，那就杀吧"；
    3. 传了 ``born``（记 pid 时拿到的创建时刻，见 `process_born`）的话，
       现在的创建时刻必须和它一致。光比名字拦不住"pid 被回收给了**另一个
       同名进程**" —— 用户同时开着第二个窗口时，它的工作进程和我们同名。

    ``expect_image`` 只在测试里传，用来模拟"名字对不上"的情形。
    """
    expect = os.path.basename(expect_image or sys.executable).lower()

    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)          # 先探活，不发真信号
        except (ProcessLookupError, PermissionError, OSError):
            return False
        try:
            import signal

            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False

    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_TERMINATE = 0x0001
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                           ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        handle = k32.OpenProcess(
            PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False              # 已经退出了，或者没权限
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                if code.value != STILL_ACTIVE:
                    return False      # 已经死了，不用管

            image = _query_image(k32, handle)
            if image is None or os.path.basename(image).lower() != expect:
                # 名字对不上是 pid 被回收给了别的程序；查不到则是不知道它是谁。
                # 两种都绝不能杀。
                return False
            if born is not None and _creation_time(k32, handle) != born:
                # 同名、但不是我们当初记下的那个进程（pid 被回收给了另一个
                # 同名实例的进程）。
                return False

            return bool(k32.TerminateProcess(handle, 1))
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def _process_children():
    """一次 Windows 进程快照，返回父 PID -> 子 PID 列表；失败返回空字典。"""
    if not IS_WINDOWS:
        return {}

    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE = wintypes.HANDLE(-1).value

        # 调用 Windows 系统接口要用的数据格式：描述「一个进程」。
        # 用来找出本程序开出的所有子进程，关窗口时好把它们一个不漏地结束掉。
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ('dwSize', wintypes.DWORD),
                ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
                ('th32ModuleID', wintypes.DWORD),
                ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD),
                ('pcPriClassBase', ctypes.c_long),
                ('dwFlags', wintypes.DWORD),
                ('szExeFile', wintypes.WCHAR * 260),
            ]

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE or not snap:
            return {}

        children = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                children.setdefault(entry.th32ParentProcessID, []).append(
                    entry.th32ProcessID)
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return {}
    return children


def child_pids_of(pids):
    """只读查询后代 PID；用于统计，不把结果直接当成可以终止的进程。

    打包版 worker 可能有引导器和 Python 子进程两层。需要认领并收尾时，
    用 `owned_descendants` 校验整条父子关系和创建时间。
    """
    children = _process_children()
    pids = set(pids)

    found = set()
    stack = list(pids)
    while stack:
        pid = stack.pop()
        for kid in children.get(pid, ()):
            if kid not in found and kid not in pids:
                found.add(kid)
                stack.append(kid)
    return found


def owned_descendants(identities):
    """从已知的 {PID: 创建时间} 发现后代，返回同样的身份映射。

    先验证根身份再展开，查不到或失配的根不能认领任何后代。查询期间保留
    进程句柄，避免已校验的 PID 被回收；打开后代句柄后再取快照核对父子关系，
    避免第一次快照里的子 PID 已被分给无关进程。孩子也不能早于父进程出生。
    返回后，终止仍须把创建时间传给 `kill_pid` 作最后校验。
    """
    if not IS_WINDOWS or not identities:
        return {}
    import ctypes
    from ctypes import wintypes

    handles, births = [], {}
    try:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        def retain(pid, expected=None, minimum=None):
            handle = k32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            handles.append(handle)
            made = _creation_time(k32, handle)
            if (made is None or (expected is not None and made != expected)
                    or (minimum is not None and made < minimum)):
                return False
            births[pid] = made
            return True

        roots = {pid for pid, made in list(identities.items())
                 if made is not None and retain(pid, expected=made)}
        if not roots:
            return {}
        children = _process_children()
        parents, stack = {}, list(roots)
        while stack:
            parent = stack.pop()
            for kid in children.get(parent, ()):
                if kid not in births and retain(kid, minimum=births[parent]):
                    parents[kid] = parent
                    stack.append(kid)

        # 已打开的进程对象不会换人；再确认它现在的父 PID 确实指向已验证的根/后代。
        current = _process_children()
        found, stack = {}, list(roots)
        while stack:
            parent = stack.pop()
            for kid in current.get(parent, ()):
                if parents.get(kid) == parent and kid not in found:
                    found[kid] = births[kid]
                    stack.append(kid)
        return found
    except Exception:
        return {}
    finally:
        for handle in handles:
            k32.CloseHandle(handle)


def current_priority():
    """读当前进程的优先级类，给测试和日志用。非 Windows 返回 None。"""
    if not IS_WINDOWS:
        return None
    try:
        k32 = _kernel32()
        return k32.GetPriorityClass(k32.GetCurrentProcess())
    except Exception:
        return None


def init_worker(level):
    """ProcessPoolExecutor 的 initializer：子进程一起来就把自己降级。"""
    apply_priority(level)


# 每个子进程处理这么多张就回收重建。Pillow 解一张 6000x4000 要几十上百 MB，
# 而子进程是复用的 —— Python 把大块内存还给系统并不积极，进程的 RSS 会一路
# 爬到它经手过的最大那张图，再也降不下来。定期换新的进程才能把内存放掉。
#
# **但图小的时候根本没有爬升可言，回收就是纯亏。** 8 个工人是同时起步、
# 同时干活的，于是也同时撞到这条线、一起被回收 —— 打包版每次重建都要重跑
# 一遍 bootloader，那段时间池子几乎是空的。用户 3469 张的那次是 87 次集体
# 重建，界面上表现为「24 个还在处理」而任务管理器里一个工作子进程都没有。
#
# 实测 2400 张、单张预估 16 MB：
#   阈值 80  -> 87.0 张/秒，利用率 60%，进程树峰值 453 MB
#   阈值 500 -> 122.8 张/秒，利用率 88%，进程树峰值 475 MB
# 吞吐 +41%，内存几乎没动。
TASKS_PER_CHILD = 80          # 大图那一档的下限，见下面

TASKS_PER_CHILD_LIGHT = 600   # 小图：回收没有意义
TASKS_PER_CHILD_MID = 240


def tasks_per_child(workers, avg_peak_mb, budget_mb):
    """按「这批图有多大」决定多久回收一次子进程。

    判据是 ``工人数 x 单张平均峰值`` —— 一轮下来每个工人的 RSS 大约就是它
    见过最大那张图的峰值，全部加起来还远小于内存预算时，回收纯属白烧。
    图大的时候维持原来的 80，安全性不变。
    """
    if not avg_peak_mb or not budget_mb:
        return TASKS_PER_CHILD
    churn = max(workers, 1) * avg_peak_mb
    if churn < budget_mb * 0.25:
        return TASKS_PER_CHILD_LIGHT
    if churn < budget_mb * 0.6:
        return TASKS_PER_CHILD_MID
    return TASKS_PER_CHILD

# 可用内存低于这个比例就告警。低于一半再降并发。
MEMORY_WARN_RATIO = 0.15
MEMORY_CRITICAL_RATIO = 0.07


def memory_status():
    """返回 (已用比例, 可用字节, 总字节)。拿不到就返回 (None, None, None)。"""
    if not IS_WINDOWS:
        try:
            total = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
            avail = os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
            return (total - avail) / total, avail, total
        except (AttributeError, ValueError, OSError, ZeroDivisionError):
            return None, None, None

    try:
        import ctypes
        from ctypes import wintypes

        # 调用 Windows 系统接口要用的数据格式：描述「当前内存用了多少、还剩多少」。
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ('dwLength', wintypes.DWORD),
                ('dwMemoryLoad', wintypes.DWORD),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.WinDLL('kernel32').GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None, None, None
        return stat.dwMemoryLoad / 100.0, stat.ullAvailPhys, stat.ullTotalPhys
    except Exception:
        return None, None, None


# 各模式允许「同时在解码的图片」占多少内存。真正把内存打爆的不是任务总数，
# 而是几个工作进程同时抓到巨图 —— 8000x8000 一张解出来就是几百 MB，八个一起
# 就是四五个 GB。按可用内存的一个比例设上限，并且封顶。
# **封顶值放宽过一次。** 原来是 768 / 2048 / 3072 的绝对值，在大内存机器上
# 永远是这个常数在生效，比例形同虚设：23 GB 内存、13 GB 可用的机器，均衡档
# 只肯用 2 GB —— 可用内存的 15%。实测这直接压死了 CPU 利用率（大图语料上
# 2 GB→18%、4 GB→35%、8 GB→55%，几乎线性）。
# 现在封顶值只在**非常大**的机器上才会生效，小内存机器仍然由比例兜底：
# 8 GB 内存、4 GB 可用的机器，均衡档 = min(6144, 4096x0.35) = 1434 MB，
# 跟改之前一样保守。
MEMORY_BUDGET = {
    ECO: (0.20, 2048),
    BALANCED: (0.35, 6144),
    TURBO: (0.45, 10240),
    CUSTOM: (0.45, 10240),
}


# 界面上给的固定档位。用具体数值而不是百分比 —— 百分比看不出到底会占多少，
# 也没法让人按自己机器的情况卡死一个上限。0 表示「自动」。
MEMORY_LIMIT_CHOICES = [
    ('自动 (按可用内存)', 0),
    ('512 MB', 512),
    ('1 GB', 1024),
    ('2 GB', 2048),
    ('4 GB', 4096),
    ('6 GB', 6144),
    ('8 GB', 8192),
]


def memory_budget_mb(mode, explicit_mb=0):
    """这一轮允许同时占用多少 MB 来解码图片。

    ``explicit_mb`` 非 0 时就用它，用户说多少就是多少，不再按比例算。
    """
    if explicit_mb:
        return max(256, float(explicit_mb))

    ratio, cap = MEMORY_BUDGET.get(mode, MEMORY_BUDGET[BALANCED])
    _used, avail, _total = memory_status()
    if not avail:
        return cap
    return max(256, min(cap, avail / 1048576 * ratio))


def memory_pressure():
    """返回 'ok' / 'warn' / 'critical' / None（读不到）。"""
    used, avail, total = memory_status()
    if used is None or not total:
        return None
    free_ratio = avail / total
    if free_ratio <= MEMORY_CRITICAL_RATIO:
        return 'critical'
    if free_ratio <= MEMORY_WARN_RATIO:
        return 'warn'
    return 'ok'


def describe(mode, custom=None):
    """给日志用的一行说明。"""
    info = MODES.get(mode, MODES[CUSTOM])
    n = worker_count(mode, custom)
    tag = {'background': '后台优先级（含磁盘 I/O）',
           'below': '低于普通优先级'}.get(info['priority'], '普通优先级')
    return f"{info['label']}：并发 {n}，{tag}"
