# -*- coding: utf-8 -*-
"""一条命令跑完所有确认项，只出一份报告。

用法:  python 完整自检.py
       python 完整自检.py --skip-build     跳过打包（只验源码层）
退出码 0 = 全绿，1 = 有项目没过，2 = 已经有一套在跑、这一套没开始。

**为什么要有这个脚本**：确认不该是"开一个看一眼、再开一个看一眼"。
散着跑的问题有三个 —— 中途改了代码会污染还在跑的那一轮、结论散落在十几条
输出里对不齐、而且很容易漏掉其中一项还以为都查过了。这里把八项固定下来，
一次跑完、一份结论。

**这个脚本只动它自己启动的进程。** 上一版收尾和打包前都是
`taskkill /F /T /IM ImageWorkbenchV4ce.exe` —— 按**名字**强杀，用户另开着
一个窗口在处理图片的话会被一起杀掉，直写模式下还会留下半截文件；而且带
`--skip-build`（根本不启动程序）时也照杀不误。
现在：每个启动的进程都记下 pid 和整棵进程树，收尾只结束这些；打包目标被别的
实例占着就**报出来并停下**，不替用户关窗口。
"""
import fnmatch
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time

from imgwb import perf

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RESULTS = []

EXE_NAME = 'ImageWorkbenchV4ce.exe'
ONEFILE_EXE = os.path.join(HERE, 'dist', EXE_NAME)
ROOT_COPY = os.path.join(HERE, EXE_NAME)
ONEDIR_DIR = os.path.join(HERE, 'dist', 'ImageWorkbenchV4ce_dir')
ONEDIR_EXE = os.path.join(ONEDIR_DIR, EXE_NAME)

# 本轮**自己启动**的进程：pid 集合（含后代），以及它们的映像路径。
# 收尾时只结束这里面的，而且动手前再核一遍映像路径 —— pid 会被系统回收。
OWN_PIDS = set()
OWN_IMAGES = set()
OWN_BORN = {}                  # PID -> 创建时间；同路径的新实例也不能误认

SMOKE_WINDOW_WAIT = 45      # 等主窗口出现，秒（单文件版要先解包，冷启动慢）
SMOKE_SETTLE = 2            # 窗口出现后再观察一会儿，看进程是不是还活着
SMOKE_EXIT_WAIT = 30        # 发了关闭消息之后等它自己退出
# 日志里出现这些就说明启动过程出了事，哪怕窗口看着是开了
LOG_TROUBLE = ('[未捕获异常]', '[界面回调异常]', 'Traceback', ' ERROR ')


def run(cmd, env=None, timeout=1800):
    e = dict(os.environ, PYTHONIOENCODING='utf-8')
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=HERE, env=e, capture_output=True,
                          text=True, encoding='utf-8', errors='replace',
                          timeout=timeout)


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


# ------------------------------------------------------------ 进程与窗口
# 全部走 Win32 API，不解析 tasklist 的输出：那是系统 OEM 代码页（本机 GBK），
# 中文程序名按 utf-8 解码会变乱码，"有没有这个进程"的判断就永远是"没有"
# —— 上一版因此出过一条假绿。

def _proc_table():
    """全系统进程快照，返回 [(pid, 父 pid)]。"""
    import ctypes
    from ctypes import wintypes

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

    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def _tree(root_pid):
    """`root_pid` 的全部后代。单文件版是引导器 + 真正的程序两个进程，
    窗口属于后者，所以找窗口、收尾都要按整棵树来。"""
    found = perf.owned_descendants({root_pid: OWN_BORN.get(root_pid)})
    OWN_BORN.update(found)
    return set(found)


def _open(pid, access):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32, k32.OpenProcess(access, False, pid)


def _image_path(pid):
    """这个 pid 现在跑的是哪个 exe（完整路径）。已退出或查不到返回空串。"""
    import ctypes
    from ctypes import wintypes
    k32, handle = _open(pid, 0x1000)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ''
    try:
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                           ctypes.POINTER(wintypes.DWORD)]
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(handle, ctypes.byref(code)) \
                and code.value != 259:      # STILL_ACTIVE
            return ''
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ''
    finally:
        k32.CloseHandle(handle)


def _terminate(pid):
    born = OWN_BORN.get(pid)
    image = _image_path(pid)
    if born is None or not image or _norm(image) not in OWN_IMAGES:
        return False
    return perf.kill_pid(pid, expect_image=image, born=born)


def holders(paths):
    """**不是本轮启动的**进程里，谁正跑着这些 exe。返回 [(pid, 路径)]。"""
    want = {_norm(p) for p in paths}
    out = []
    for pid, _ppid in _proc_table():
        if pid in OWN_PIDS:
            continue
        image = _image_path(pid)
        if image and _norm(image) in want:
            out.append((pid, image))
    return out


def _own_alive():
    return [pid for pid in OWN_PIDS
            if OWN_BORN.get(pid) is not None
            and perf.process_born(pid) == OWN_BORN[pid]
            and (lambda im: im and _norm(im) in OWN_IMAGES)(_image_path(pid))]


def stop_own(wait=10):
    """结束本轮自己启动的进程，**只结束这些**。全没了返回 True。

    没启动过任何进程时（比如 `--skip-build`）`OWN_PIDS` 是空的，这里什么都
    不做 —— 这正是和上一版按名字强杀的区别。动手前逐个核对映像路径：
    同时核对创建时间：即使 PID 被回收给同路径的另一个实例，也不碰它。
    """
    deadline = time.time() + wait
    while True:
        alive = _own_alive()
        if not alive:
            return True
        if time.time() > deadline:
            return False
        for pid in alive:
            _terminate(pid)
        time.sleep(0.5)


def _windows_of(pids):
    """这些进程的**可见、带标题**的顶层窗口，返回 [(hwnd, pid, 标题)]。"""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL('user32', use_last_error=True)
    proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.EnumWindows.argtypes = [proc_t, wintypes.LPARAM]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    found = []

    def each(hwnd, _lparam):
        owner = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, buf, 256)
            if buf.value:
                found.append((hwnd, owner.value, buf.value))
        return True

    u32.EnumWindows(proc_t(each), 0)
    return found


def _ask_close(hwnd):
    """给窗口发一条普通的关闭消息 —— 等同于用户点右上角的叉。"""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL('user32', use_last_error=True)
    u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
    u32.PostMessageW(hwnd, 0x0010, 0, 0)    # WM_CLOSE


def record(name, ok, detail=''):
    RESULTS.append((name, ok, detail))
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''),
          flush=True)


# ---------------------------------------------------------------- 1 回归
def check_tests():
    print('\n[1] 全量回归（tests/run_all.py）', flush=True)
    r = run([PY, os.path.join('tests', 'run_all.py')])
    m = re.search(r'通过 (\d+) / (\d+)', r.stdout or '')
    got = m.group(0) if m else '没解析到结果'
    record('全部测试通过', r.returncode == 0, got)
    if r.returncode != 0:
        for line in (r.stdout or '').splitlines():
            if line.strip().startswith('FAIL') or '✗' in line:
                print('      ', line.strip()[:140])


# ---------------------------------------------------------------- 2 翻译
def check_i18n():
    print('\n[2] 英文界面翻译覆盖', flush=True)
    r = run([PY, '检查翻译覆盖.py'])
    m = re.search(r'还没翻的：(\d+) 条', r.stdout or '')
    record('界面文案全部有英文', r.returncode == 0,
           (m.group(0) if m else '') )
    if r.returncode != 0:
        print((r.stdout or '')[-600:])


# ---------------------------------------------------------------- 3/4 打包
def check_builds(skip):
    print('\n[3] 两种形态打包', flush=True)
    if skip:
        record('打包', True, '按 --skip-build 跳过')
        return
    # 打包要覆盖这三个 exe。有别的实例正跑着它们的话文件被占住、覆盖不了。
    # **那是用户开着的窗口，不是我们的 —— 报出来停下，不替他关。**
    busy = holders([ONEFILE_EXE, ROOT_COPY, ONEDIR_EXE])
    record('打包目标没有被正在运行的程序占着', not busy,
           '' if not busy else
           '请先关掉这些窗口再跑（自检不会替你关）: '
           + ', '.join(f'PID {pid} {image}' for pid, image in busy[:4]))
    if busy:
        record('单文件版打包成功且是本轮新产物', False, '目标被占用，没有打包')
        record('目录版打包成功，三个库是独立文件', False, '目标被占用，没有打包')
        return
    started = time.time()
    r = run([PY, '-m', 'PyInstaller', '--noconfirm', '--distpath', 'dist',
             '--workpath', os.path.join(WORK_TEMP, 'pyi_build'),
             'ImageWorkbenchV4ce.spec'],
            env={'TEMP': WORK_TEMP, 'TMP': WORK_TEMP})
    one = ONEFILE_EXE
    # **不能只看 PyInstaller 的退出码。** 还要确认 exe 确实是这一轮新写的 ——
    # 覆盖失败时它可能照样退出 0，留下一个旧文件。
    fresh = os.path.isfile(one) and os.path.getmtime(one) >= started - 2
    record('单文件版打包成功且是本轮新产物',
           r.returncode == 0 and fresh,
           (f'{os.path.getsize(one):,} 字节' if os.path.isfile(one) else '没生成')
           + ('' if fresh else '，但文件不是本轮写的（旧产物？）'))
    if os.path.isfile(one):
        import shutil
        try:
            shutil.copyfile(one, ROOT_COPY)
        except OSError as exc:
            record('根目录副本已更新', False, str(exc))

    r = run([PY, '-m', 'PyInstaller', '--noconfirm', '--distpath', 'dist',
             '--workpath', os.path.join(WORK_TEMP, 'pyi_build_onedir'),
             'ImageWorkbenchV4ce.spec'],
            env={'IMGWB_ONEDIR': '1', 'TEMP': WORK_TEMP, 'TMP': WORK_TEMP})
    d = ONEDIR_DIR
    dlls = []
    if os.path.isdir(d):
        for root, _dirs, files in os.walk(d):
            dlls += [f for f in files
                     if any(k in f.lower()
                            for k in ('libx265', 'libheif', 'libde265'))]
    fresh_dir = (os.path.isfile(ONEDIR_EXE)
                 and os.path.getmtime(ONEDIR_EXE) >= started - 2)
    record('目录版打包成功，三个库是独立文件',
           r.returncode == 0 and len(dlls) == 3 and fresh_dir,
           f'找到 {sorted(dlls)}'
           + ('' if fresh_dir else '，但 exe 不是本轮写的（旧产物？）'))


# ---------------------------------------------------------------- 5 许可
def check_license(skip):
    print('\n[4] 打包里的许可材料', flush=True)
    if skip:
        record('许可核对', True, '按 --skip-build 跳过')
        return
    for label, exe in [('单文件版', ONEFILE_EXE), ('目录版', ONEDIR_EXE)]:
        r = run([PY, '检查打包许可.py', exe])
        record(label + '许可材料存在且内容与本地一致', r.returncode == 0,
               '仅材料核验，不代表完整发布合规' if r.returncode == 0 else '')
        if r.returncode != 0:
            print((r.stdout or '')[-600:])


# ---------------------------------------------------------------- 6 启动
# 自检用的工作目录：打包的中间文件、喂给 exe 的样图、单文件版的解包目录都在
# 这底下。默认是系统临时目录；想放到别的盘就设环境变量 IMGWB_SELFCHECK_TEMP。
WORK_TEMP = os.environ.get('IMGWB_SELFCHECK_TEMP') or tempfile.gettempdir()


def own_workdir(kind):
    """本轮这一项**独占**的工作目录：`WORK_TEMP\imgwb-selfcheck-<kind>-随机串`。

    原来是固定名字，开头先 `rmtree` 一遍再用。固定名字意味着这个目录不是
    "本轮建的"，只是"叫这个名字的" —— 和自测 `--work` 那个问题
    是同一类：删的是一个自己没建过的东西。现在每次新建一个，通过了删掉，
    没通过留着给人看（名字里的随机串不同，下一轮不会去动它）。
    """
    import tempfile
    return tempfile.mkdtemp(prefix=f'imgwb-selfcheck-{kind}-', dir=WORK_TEMP)


def default_temp():
    """用户**双击 exe** 时系统给它的 TEMP：注册表里用户环境变量那一份。

    不能读 `os.environ['TEMP']` —— 跑自检的这个进程自己的 TEMP 可能已经被
    改到别处了，那就不是用户真正会遇到的那个目录。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            raw, _kind = winreg.QueryValueEx(key, 'TEMP')
        path = os.path.expandvars(raw)
    except OSError:
        path = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Temp')
    return path if os.path.isdir(path) else ''


def _unpack_dirs(temp):
    """TEMP 下 PyInstaller 单文件版的解包目录（`_MEI` 开头）。"""
    try:
        return {n for n in os.listdir(temp)
                if n.startswith('_MEI') and os.path.isdir(os.path.join(temp, n))}
    except OSError:
        return set()


def smoke(label, cmd, cwd, log, temp=WORK_TEMP):
    """把程序真的启动一次，**用四件事**一起判断它起没起来：

    1. 进程树里出现了可见的主窗口；
    2. 窗口出现之后进程还活着；
    3. 本轮新增的日志里没有未捕获异常；
    4. 发一条普通的关闭消息，它自己退出，而且退出码是 0。

    另外看一眼 `temp` 下有没有留下本轮新增的 `_MEI` 解包目录 —— 单文件版
    正常退出时引导器会把它删掉，留着就说明退出过程不干净。

    上一版只看"9 秒后日志非空"。可入口是**先写启动横幅、再建窗口**的 ——
    之后 import 失败、Tcl/Tk 初始化失败、窗口构建抛异常，日志都已经非空了，
    于是启动后崩溃被记成"能启动"（两种形态各 10/10）。
    """
    offset = 0
    try:
        os.remove(log)
    except FileNotFoundError:
        pass
    except OSError:
        # 删不掉就只读本轮新增的那一段，别把上一轮的内容当成这一轮的。
        try:
            offset = os.path.getsize(log)
        except OSError:
            offset = 0

    OWN_IMAGES.add(_norm(cmd[0]))
    env = dict(os.environ, TEMP=temp, TMP=temp)
    unpacked_before = _unpack_dirs(temp)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env)
    OWN_PIDS.add(proc.pid)
    OWN_BORN[proc.pid] = perf.process_born(proc.pid)

    problems, title, hwnd = [], '', None
    deadline = time.time() + SMOKE_WINDOW_WAIT
    while time.time() < deadline and proc.poll() is None:
        family = {proc.pid} | _tree(proc.pid)
        OWN_PIDS.update(family)
        wins = _windows_of(family)
        if wins:
            hwnd, _pid, title = wins[0]
            break
        time.sleep(0.5)

    if hwnd is None:
        if proc.poll() is not None:
            problems.append(f'没建出窗口就退出了（退出码 {proc.returncode}）')
        else:
            problems.append(f'{SMOKE_WINDOW_WAIT} 秒内没出现主窗口')
    else:
        time.sleep(SMOKE_SETTLE)
        OWN_PIDS.update(_tree(proc.pid))
        if proc.poll() is not None:
            problems.append(f'窗口出现后进程又退出了（退出码 {proc.returncode}）')
        else:
            _ask_close(hwnd)
            try:
                code = proc.wait(timeout=SMOKE_EXIT_WAIT)
                if code != 0:
                    problems.append(f'正常关闭的退出码是 {code}，不是 0')
            except subprocess.TimeoutExpired:
                problems.append(f'发了关闭消息 {SMOKE_EXIT_WAIT} 秒还没退出')

    text = ''
    try:
        with open(log, encoding='utf-8', errors='replace') as fh:
            fh.seek(offset)
            text = fh.read()
    except OSError:
        pass
    if not text.strip():
        problems.append('本轮没有写出日志')
    trouble = [ln.strip() for ln in text.splitlines()
               if any(mark in ln for mark in LOG_TROUBLE)]
    if trouble:
        problems.append(f'日志里有异常: {trouble[0][-110:]}')

    if not stop_own():
        problems.append('收尾后还有本轮启动的进程没退干净')
    # 引导器删解包目录是在子进程退出之后，给它一点时间
    left = set()
    for _ in range(10):
        left = _unpack_dirs(temp) - unpacked_before
        if not left:
            break
        time.sleep(0.5)
    if left:
        problems.append(f'{temp} 下留下了解包目录没清掉: {sorted(left)}')
    record(f'{label}能启动、建出窗口并正常退出', not problems,
           '；'.join(problems) if problems else f'窗口「{title}」，退出码 0')
    return not problems


def check_smoke(skip):
    print('\n[5] 两种形态能不能启动', flush=True)
    if skip:
        record('启动冒烟', True, '按 --skip-build 跳过')
        return
    # 单文件版要跑两遍。它启动时先往 TEMP 解包，**解包到哪儿是会影响能不能
    # 起来的**（在另一个环境里量到过：默认 TEMP 下 Tcl 初始化失败，
    # 换个 TEMP 就正常）。只在 WORK_TEMP 下验的话，用户双击时真正走的那条路
    # 就没人验过。目录版不解包，一遍就够。
    runs = [('单文件版', os.path.join(HERE, 'dist'), WORK_TEMP)]
    real_temp = default_temp()
    if not real_temp:
        record('单文件版（系统默认 TEMP）能启动、建出窗口并正常退出', False,
               '没取到系统默认 TEMP，用户双击时走的那条路这次没验到')
    elif _norm(real_temp) != _norm(WORK_TEMP):
        runs.append(('单文件版（系统默认 TEMP）',
                     os.path.join(HERE, 'dist'), real_temp))
    # 两者相同的话，上面那一遍验的就已经是系统默认 TEMP 了
    runs.append(('目录版', ONEDIR_DIR, WORK_TEMP))
    for label, exe_dir, temp in runs:
        exe = os.path.join(exe_dir, EXE_NAME)
        if not os.path.isfile(exe):
            record(f'{label}能启动、建出窗口并正常退出', False, '找不到 exe')
            continue
        smoke(label, [exe], exe_dir,
              os.path.join(exe_dir, 'logs', 'imgwb.log'), temp)


def exe_batch(label, exe, cwd):
    """让打包出来的 exe **真的处理一批图**，而不只是"窗口能打开"。

    之前每一轮自检对 exe 只验了启动。可用户拿到手的是 exe，不是源码：
    打包版的工作进程是「引导器 + 真正的 Python」两层，处理代码、HEIC 库、
    多进程启动方式都和源码运行不一样 —— 这些从来没被真正跑过。

    走 exe 的 `--cli` 入口（见 `融合_v4ce.py`），230 多张图 —— 超过 200 张才会
    用多进程，工作进程才是打包版自己的。源照片的修改时间设成两天前
    （**只设源照片**）：产物要保留这个时间，而且临时文件不能因此被当成垃圾。
    处理期间不断看输出目录里的临时文件名，核对主进程和所有工作进程用的是
    **同一个实例标记**。
    """
    import json
    import shutil

    from PIL import Image

    from imgwb.tasks import temp_owner
    from imgwb.utils import UNKNOWN_SCOPE, instance_alive

    work = own_workdir('batch')
    src, out = os.path.join(work, 'src'), os.path.join(work, 'out')
    os.makedirs(src)
    stamp = int(time.time() - 2 * 86400)
    base = Image.new('RGB', (96, 64))
    base.putdata([((x * 3) % 256, (y * 4) % 256, (x ^ y) % 256)
                  for y in range(64) for x in range(96)])
    names = []
    for i in range(230):
        names.append(f'p{i:03d}.jpg')
        base.save(os.path.join(src, names[-1]), quality=90)
    frames = [Image.new('RGB', (96, 64), c) for c in ('red', 'green', 'blue')]
    frames[0].save(os.path.join(src, 'anim.gif'), save_all=True,
                   append_images=frames[1:], duration=[80, 90, 100], loop=0)
    names.append('anim.gif')
    heic = False
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        base.save(os.path.join(src, 'phone.heic'), quality=90)
        names.append('phone.heic')
        heic = True
    except Exception:
        pass                                # 构建环境没装 HEIC 支持就不带这张
    for n in names:
        os.utime(os.path.join(src, n), (stamp, stamp))

    result_file = os.path.join(work, 'result.json')
    OWN_IMAGES.add(_norm(exe))
    env = dict(os.environ, TEMP=WORK_TEMP, TMP=WORK_TEMP)
    proc = subprocess.Popen(
        [exe, '--cli', 'run', '--source', src, '--output', 'new', '--dest', out,
         '--name', 'keep', '--rotate', 'cw90', '--json',
         '--cli-out', result_file], cwd=cwd, env=env)
    OWN_PIDS.add(proc.pid)
    OWN_BORN[proc.pid] = perf.process_born(proc.pid)

    owners, peak = set(), 0
    final = os.path.join(out, 'src_已处理')
    deadline = time.time() + 240
    while proc.poll() is None and time.time() < deadline:
        OWN_PIDS.update(_tree(proc.pid))
        try:
            found = [temp_owner(n) for n in os.listdir(final)]
        except OSError:
            found = []
        found = [o for o in found if o is not None]
        owners.update(found)
        peak = max(peak, len(found))
        time.sleep(0.05)

    problems = []
    if proc.poll() is None:
        problems.append('240 秒还没跑完')
    elif proc.returncode != 0:
        problems.append(f'退出码 {proc.returncode}')
    stop_own()
    try:
        with open(result_file, encoding='utf-8') as fh:
            result = json.load(fh)
    except (OSError, ValueError) as exc:
        result = {}
        problems.append(f'没读到结果文件: {exc}')
    if result and result.get('ok') is not True:
        problems.append(f'处理报失败: {str(result.get("failed"))[:160]}')
    if result and result.get('processed') != len(names):
        problems.append(f'处理了 {result.get("processed")} 张，应为 {len(names)}')

    try:
        produced = sorted(os.listdir(final))
    except OSError:
        produced = []
    want = sorted(('phone.jpg' if n == 'phone.heic' else n) for n in names)
    if produced != want:
        extra = [n for n in produced if n not in want][:3]
        lost = [n for n in want if n not in produced][:3]
        problems.append(f'产物不对：多出 {extra}，缺 {lost}')
    else:
        try:
            with Image.open(os.path.join(final, 'p000.jpg')) as im:
                im.load()
                if im.size != (64, 96):
                    problems.append(f'没旋转：{im.size}')
            with Image.open(os.path.join(final, 'anim.gif')) as im:
                if getattr(im, 'n_frames', 1) != 3 or im.size != (64, 96):
                    problems.append(f'动图不对：{getattr(im, "n_frames", 1)} 帧 {im.size}')
            if heic:
                with Image.open(os.path.join(final, 'phone.jpg')) as im:
                    im.load()
                    if im.size != (64, 96):
                        problems.append(f'HEIC 产物不对：{im.size}')
        except Exception as exc:
            problems.append(f'产物打不开: {type(exc).__name__}: {exc}')
        drift = [n for n in produced
                 if abs(int(os.path.getmtime(os.path.join(final, n))) - stamp) > 2]
        if drift:
            problems.append(f'{len(drift)} 个产物没保留源照片的修改时间')

    # 实例标记：整批（主进程 + 所有工作进程）应当只出现一个主人，
    # 而且不是"拿不到凭据"的那种；进程退出之后凭据应当消失。
    if peak < 50:
        problems.append(f'处理期间最多只看到 {peak} 个临时文件，没采到样')
    elif len(owners) != 1:
        problems.append(f'临时文件出现了 {len(owners)} 个不同的主人: {sorted(owners)[:3]}')
    else:
        tag, scope = next(iter(owners))
        if not tag or scope == UNKNOWN_SCOPE:
            problems.append(f'实例没拿到存活凭据（标记 {tag!r} 范围 {scope!r}）')
        elif instance_alive(scope, tag):
            problems.append('进程退出后它的存活凭据还在')

    record(f'{label}真的处理一批图（{len(names)} 张，多进程'
           + ('，含 HEIC' if heic else '，不含 HEIC') + '）', not problems,
           '；'.join(problems) if problems
           else f'全部成功，处理中同时有 {peak} 个临时文件、主人唯一')
    if not problems:
        shutil.rmtree(work, ignore_errors=True)
    return not problems


def exe_report_inside(label, exe, cwd):
    """`--cli-out` 报告放在来源文件夹里。

    报告在扫描之前就建好了，原来会被当成"不支持的文件"扫进来：原地编号时
    去改一个自己还开着的文件，照片改完名、命令却报失败；导出时多拷出一份
    0 字节的报告。上面 `exe_batch` 的报告放在来源外面，测不到。
    和最初复现它的命令一个写法：cwd 就是来源，两个路径都是相对的。跑两遍 ——
    第二遍报告已经在那儿了；第三遍不用 `--cli-out`，改成把 stdout 重定向到
    来源里的文件。再导出一遍。
    """
    import hashlib
    import json
    import shutil

    from PIL import Image

    def digests(folder, skip=()):
        out = []
        for n in os.listdir(folder):
            if n not in skip:
                with open(os.path.join(folder, n), 'rb') as fh:
                    out.append(hashlib.sha256(fh.read()).hexdigest())
        return sorted(out)

    work = own_workdir('report')
    src, out = os.path.join(work, 'photos'), os.path.join(work, 'out')
    os.makedirs(src)
    Image.new('RGB', (32, 24), 'red').save(os.path.join(src, 'a.png'))
    Image.new('RGB', (32, 24), 'blue').save(os.path.join(src, 'b.png'))
    with open(os.path.join(src, 'notes.json'), 'w', encoding='utf-8') as fh:
        fh.write('{"mine": true}')              # 用户自己的 JSON 附件，照常带走
    want = digests(src)
    OWN_IMAGES.add(_norm(exe))
    env = dict(os.environ, TEMP=WORK_TEMP, TMP=WORK_TEMP)
    problems = []

    def go(what, args, redirect=False):
        try:
            if redirect:
                # 和命令行里写 `> result.json` 是一回事：文件由调用方先建好、
                # 开着，句柄交给程序当 stdout；路径没人告诉它。
                with open(os.path.join(src, 'result.json'), 'wb') as fh:
                    rc = subprocess.run([exe, '--cli'] + args, cwd=src, env=env,
                                        stdout=fh, timeout=120).returncode
            else:
                rc = subprocess.run([exe, '--cli'] + args, cwd=src, env=env,
                                    timeout=120).returncode
        except subprocess.TimeoutExpired:
            problems.append(f'{what}：120 秒还没跑完')
            return
        try:
            with open(os.path.join(src, 'result.json'), encoding='utf-8') as fh:
                result = json.load(fh)
        except (OSError, ValueError) as exc:
            problems.append(f'{what}：没读到报告: {exc}')
            return
        if rc != 0 or result.get('ok') is not True:
            problems.append(f'{what}：退出码 {rc}，{str(result.get("failed"))[:120]}')
        if result.get('scanned') != 3 or result.get('processed') != 3:
            problems.append(f'{what}：扫到 {result.get("scanned")}、处理 '
                            f'{result.get("processed")}，应为 3（报告自己不算）')
        if digests(src, skip=('result.json',)) != want:
            problems.append(f'{what}：来源里的文件内容变了或数目不对')

    inplace = ['run', '--source', '.', '--name', 'number', '--json',
               '--cli-out', 'result.json']
    go('原地编号', inplace)
    first = sorted(os.listdir(src))
    go('原地编号第二遍', inplace)
    go('原地编号第三遍（不用 --cli-out，改用 > 重定向到来源里）',
       inplace[:-2], redirect=True)
    if sorted(os.listdir(src)) != first or len(first) != 4:
        problems.append(f'原地编号：三遍之后目录是 {sorted(os.listdir(src))}，'
                        f'第一遍是 {first}')
    go('导出', ['run', '--source', '.', '--output', 'new', '--dest', out,
              '--no-keep-structure', '--name', 'keep', '--json',
              '--cli-out', 'result.json'])
    try:
        exported = sorted(os.listdir(out))
    except OSError:
        exported = []
    if len(exported) != 3 or 'result.json' in exported or digests(out) != want:
        problems.append(f'导出：产物是 {exported}，应当正好是来源那 3 个')
    stop_own()

    record(f'{label}：报告放在来源文件夹里也不把它当输入', not problems,
           '；'.join(problems) if problems
           else '原地编号三遍（一遍用 > 重定向）、导出一遍，各扫到 3 个，内容未变')
    if not problems:
        shutil.rmtree(work, ignore_errors=True)
    return not problems


def check_exe_batch(skip):
    print('\n[5b] 打包版真的处理一批图', flush=True)
    if skip:
        record('打包版处理一批图', True, '按 --skip-build 跳过')
        return
    for label, exe_dir in (('单文件版', os.path.join(HERE, 'dist')),
                           ('目录版', ONEDIR_DIR)):
        exe = os.path.join(exe_dir, EXE_NAME)
        if not os.path.isfile(exe):
            record(f'{label}真的处理一批图', False, '找不到 exe')
            continue
        exe_batch(label, exe, exe_dir)
        exe_report_inside(label, exe, exe_dir)


def exe_selftest(label, exe, cwd, extra):
    """让 exe 跑它自带的自测（`--selftest`，见 imgwb/selftest.py）。

    和上面 `exe_batch` 的区别：那个是从外面喂样图、走命令行入口；这个是 exe
    **自己**造样图、不开窗口跑一批、再**开真的窗口按真的「开始执行」按钮**
    跑一批，最后走正常的关窗流程。界面上的"点按钮处理"以前只在源码层测过，
    打包版里从没跑过 —— 这一项补的就是它。
    同一条命令拿到别的电脑上也能跑（干净系统、英文系统、高缩放），
    报告里记着是在什么环境下通过的。
    """
    import json
    import shutil

    work = own_workdir('selftest')
    report_file = os.path.join(work, 'report.json')
    # `--work` 指向一个**已经放着东西**的文件夹：自测只能用它底下自己建的
    # 子目录，原有的文件跑完必须原样还在（原来整个文件夹被删）。
    data = os.path.join(work, 'data')
    os.makedirs(os.path.join(data, 'nested'))
    bystanders = {os.path.join(data, 'unrelated-notes.txt'): b'USER NOTES',
                  os.path.join(data, 'nested', 'important.txt'): b'USER CONTENT'}
    for path, body in bystanders.items():
        with open(path, 'wb') as fh:
            fh.write(body)
    OWN_IMAGES.add(_norm(exe))
    env = dict(os.environ, TEMP=WORK_TEMP, TMP=WORK_TEMP)
    proc = subprocess.Popen(
        [exe, '--selftest', '--cli-out', report_file,
         '--work', data] + list(extra), cwd=cwd, env=env)
    OWN_PIDS.add(proc.pid)
    OWN_BORN[proc.pid] = perf.process_born(proc.pid)
    deadline = time.time() + 480
    while proc.poll() is None and time.time() < deadline:
        OWN_PIDS.update(_tree(proc.pid))
        time.sleep(0.5)
    problems = []
    if proc.poll() is None:
        problems.append('480 秒还没跑完')
    elif proc.returncode != 0:
        problems.append(f'退出码 {proc.returncode}')
    stop_own()
    try:
        with open(report_file, encoding='utf-8') as fh:
            report = json.load(fh)
    except (OSError, ValueError) as exc:
        report = {}
        problems.append(f'没读到自测报告: {exc}')
    problems += [str(p)[:200] for p in report.get('problems', [])[:4]]
    for path, body in bystanders.items():
        try:
            with open(path, 'rb') as fh:
                intact = fh.read() == body
        except OSError:
            intact = False
        if not intact:
            problems.append(f'--work 文件夹里原有的 {os.path.basename(path)} 没了或变了')
    if not problems and sorted(os.listdir(data)) != ['nested', 'unrelated-notes.txt']:
        problems.append(f'自测通过了却在 --work 文件夹里留了东西: {os.listdir(data)[:4]}')
    gui = report.get('gui') or {}
    env_info = report.get('environment') or {}
    if report and not gui:
        problems.append('报告里没有界面那一半')
    elif gui and gui.get('processed') != gui.get('files'):
        problems.append(f'界面批处理了 {gui.get("processed")}/{gui.get("files")}')
    want_lang = extra[extra.index('--lang') + 1] if '--lang' in extra else None
    if want_lang and env_info.get('app_language') != want_lang:
        problems.append(f'界面语言是 {env_info.get("app_language")}，应为 {want_lang}')
    scale = (gui.get('layout_advanced') or {}).get('ui_scale')
    if '--scale' in extra and not (scale and scale > 1.5):
        problems.append(f'没有按放大后的比例画界面（ui_scale={scale}）')
    record(f'{label}：自测通过（含真窗口按按钮处理 {gui.get("files", "?")} 张）',
           not problems,
           '；'.join(problems) if problems else
           f'语言 {env_info.get("app_language")}，界面比例 {scale}，'
           f'窗口 {(gui.get("layout_advanced") or {}).get("window")}，'
           f'界面批 {gui.get("seconds")} 秒，状态栏「{gui.get("status_text")}」')
    if not problems:
        shutil.rmtree(work, ignore_errors=True)
    return not problems


def check_exe_selftest(skip):
    print('\n[5c] 打包版自测：真窗口、真按钮', flush=True)
    if skip:
        record('打包版自测', True, '按 --skip-build 跳过')
        return
    onefile = os.path.join(HERE, 'dist')
    for label, exe_dir, extra in (
            ('单文件版 · 中文界面', onefile, ['--lang', 'zh']),
            ('目录版 · 英文界面', ONEDIR_DIR, ['--lang', 'en']),
            ('目录版 · 模拟 200% 界面比例', ONEDIR_DIR, ['--lang', 'zh', '--scale', '2'])):
        exe = os.path.join(exe_dir, EXE_NAME)
        if not os.path.isfile(exe):
            record(f'{label}：自测通过', False, '找不到 exe')
            continue
        exe_selftest(label, exe, exe_dir, extra)


# ---------------------------------------------------------------- 7 静态
def _gitignored(name, patterns):
    """根目录下这个文件名会不会被 .gitignore 挡住（只看不带目录的简单模式，
    够用来核对报告文件；不是完整的 gitignore 实现）。"""
    hit = False
    for pat in patterns:
        negate = pat.startswith('!')
        body = pat[1:] if negate else pat
        if body.endswith('/'):
            continue
        if fnmatch.fnmatchcase(name, body.lstrip('/')):
            hit = not negate
    return hit


# 随代码分发的文档。根目录下别的 .md / .html 都是开发过程材料，不进仓库。
SHIPPED_DOCS = ('README.md', 'README.en.md', 'CHANGELOG.md', '代码导读.md', '第三方许可声明.md',
                '发布与重建.md')


def check_static():
    print('\n[6] 源码与文档的静态检查', flush=True)
    import glob
    ctrl = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]|\r(?!$)')
    bad = []
    for pat in ('*.md', '*.py', '*.spec', 'tests/*.py', 'imgwb/*.py',
                '发布附件/*.md'):
        for p in glob.glob(os.path.join(HERE, pat)):
            with open(p, encoding='utf-8', newline='') as fh:
                s = fh.read()
            for i, line in enumerate(s.split('\n'), 1):
                line = line.rstrip('\r')
                if ctrl.search(line) or '\t' in line:
                    bad.append(f'{os.path.basename(p)}:{i}')
    record('没有控制字符损坏（heredoc 老毛病）', not bad, str(bad[:4]))

    scripts = glob.glob(os.path.join(HERE, 'tests', 'test_*.py'))
    noexit = [os.path.basename(p) for p in scripts
              if not re.search(r'sys\.exit\(', open(p, encoding='utf-8').read())]
    record('每个测试脚本失败时都返回非零', not noexit,
           f'{len(scripts)} 个脚本，缺的: {noexit or "无"}')

    patterns = []
    gi = os.path.join(HERE, '.gitignore')
    if os.path.isfile(gi):
        with open(gi, encoding='utf-8') as fh:
            patterns = [ln.strip() for ln in fh
                        if ln.strip() and not ln.lstrip().startswith('#')]

    # 留在仓库的文档不能指向不进仓库的文件：文档里提到的每个 .md / .html
    # 文件名，都拿忽略规则核一遍。
    dead = []
    for name in SHIPPED_DOCS:
        p = os.path.join(HERE, name)
        if not os.path.isfile(p):
            continue
        s = open(p, encoding='utf-8').read()
        mentioned = set(re.findall(r'[\w.\-]+\.(?:md|html)\b', s))
        dead += [f'{name}->{g}' for g in sorted(mentioned)
                 if _gitignored(g, patterns)]
    record('留在仓库的文档没有死链', not dead, str(dead[:3]))

    # 开发过程材料带着完整的本机路径，一份都不能进仓库。曾经有一份
    # 就是因为文件名和忽略规则对不上漏出去的——
    # 所以不靠"记得加规则"，每次都拿实际文件名核一遍。
    # 根目录下的文档，除了明确要随代码分发的那几份，都算开发过程材料。
    local_only = [n for n in os.listdir(HERE)
                  if os.path.isfile(os.path.join(HERE, n))
                  and n.lower().endswith(('.md', '.html'))
                  and n not in SHIPPED_DOCS]
    leaked = [n for n in local_only if not _gitignored(n, patterns)]
    record('开发过程材料都被 .gitignore 挡住', not leaked,
           f'{len(local_only)} 份' + (f'，漏了 {leaked}' if leaked else ''))


# ---------------------------------------------------------------- 8 产物
def check_artifacts(skip):
    print('\n[7] 交付产物', flush=True)
    if skip:
        record('产物核对', True, '按 --skip-build 跳过')
        return
    a = ONEFILE_EXE
    b = ROOT_COPY
    same = False
    sha = ''
    if os.path.isfile(a) and os.path.isfile(b):
        da, db = open(a, 'rb').read(), open(b, 'rb').read()
        same = da == db
        sha = hashlib.sha256(da).hexdigest()
    record('根目录副本和 dist 一致', same, f'SHA-256 {sha[:16]}…' if sha else '')

    # 文件名 -> SHA-256。后面三项是**实际二进制对应的**那几份（MSYS2 的签名
    # 源码包和 libheif 上游发布包），哈希钉死 —— 换了依赖版本这里会红，
    # 提醒重新对账（见《第三方许可声明.md》第二节）。
    want = {
        'x265-4.2+1-e444744.tar.gz': None,
        'libheif-1.23.1.tar.gz': None,
        'libde265-1.1.1.tar.gz': None,
        'pillow-heif-1.5.0.tar.gz': None,
        '说明.md': None,
        'libheif-1.23.1-release.tar.gz':
            '0de0327f60fcd47de90d5654c6fe152232738d60d84fe084ec3e0f35e03b166a',
        'msys2-recipes/mingw-w64-x265-4.2-2.src.tar.zst':
            '8d292144ac8fa15c8fcd27bb68daa60345918a155096d2cc61b64aeb042ee98e',
        'msys2-recipes/mingw-w64-x265-4.2-2.src.tar.zst.sig': None,
        'msys2-recipes/mingw-w64-libde265-1.1.1-1.src.tar.zst':
            'fd4cf7d04006cb984f92579a09d34839469057ec0fb4a04a979484d748b60190',
        'msys2-recipes/mingw-w64-libde265-1.1.1-1.src.tar.zst.sig': None,
    }
    d = os.path.join(HERE, '发布附件')
    miss, wrong = [], []
    for name, digest in want.items():
        p = os.path.join(d, *name.split('/'))
        if not os.path.isfile(p):
            miss.append(name)
        elif digest:
            with open(p, 'rb') as fh:
                if hashlib.sha256(fh.read()).hexdigest() != digest:
                    wrong.append(name)
    record('所列 HEIC 源码附件存在且哈希对得上（非全部对应源码证明）', not miss and not wrong,
           f'{len(want)} 个文件'
           + (f'，缺 {miss}' if miss else '')
           + (f'，哈希不对 {wrong}' if wrong else ''))


def only_one_run():
    """同一时间只许跑一套完整自检。已经有一套在跑就返回 False。

    两套同时跑没有任何一项结论可信：打包往同一个 `dist` 里写、冒烟删同一个
    日志文件、对方启动的 exe 在这边看来是"别的实例占着打包目标"。所以不是
    "尽量别互相干扰"，而是直接不让第二套开始。用具名互斥体：进程怎么死
    系统都会收回，不会留下陈旧锁。句柄故意不关。
    """
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    handle = k32.CreateMutexW(None, False, 'Local\imgwb-selfcheck-running')
    if not handle:
        return True                         # 查不了就不拦
    globals()['_run_mutex'] = handle
    return ctypes.get_last_error() != 183   # ERROR_ALREADY_EXISTS


def main(argv):
    skip = '--skip-build' in argv
    if not only_one_run():
        print('已经有一套完整自检在跑。两套同时跑会互相弄坏对方的打包和结论 —— '
              '等那一套跑完再来。', flush=True)
        return 2
    t0 = time.time()
    print('=' * 62)
    print('完整自检', time.strftime('%Y-%m-%d %H:%M:%S'))
    print('=' * 62, flush=True)
    try:
        check_tests()
        check_i18n()
        check_builds(skip)
        check_license(skip)
        check_smoke(skip)
        check_exe_batch(skip)
        check_exe_selftest(skip)
        check_static()
        check_artifacts(skip)
    finally:
        # 收尾：别给用户留下后台进程。**只收本轮自己启动的** ——
        # 没启动过就什么都不做（见文件顶部）。
        stop_own()
    bad = [n for n, ok, _d in RESULTS if not ok]
    print('\n' + '=' * 62)
    print(f'{len(RESULTS) - len(bad)} / {len(RESULTS)} 项通过，'
          f'耗时 {time.time() - t0:.0f}s')
    if bad:
        print('\n没过的项目：')
        for n in bad:
            print('   ✗', n)
    else:
        print('\n全部通过')
    return 1 if bad else 0


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main(sys.argv[1:]))
