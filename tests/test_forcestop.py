"""强制停止要立刻见效，而且一个子进程都不许留下。

用户实拍的两个问题：
  1. 先点「取消」（直写模式）、再点「强制停止」，界面卡在「正在强制停止...」
     几分钟不动。根因：收尾用的是 `with executor:`，它的 __exit__ 是
     shutdown(wait=True) —— 在途任务不跑完就不返回，强制停止等于没点。
  2. 关掉界面后任务管理器里还留着一堆工作进程。根因：只按
     executor._processes 去杀，而 max_tasks_per_child 会不停换新进程，
     池子一收尾那个字典就空了，于是「杀了 0 个」还报告成功。

用法:  python tests\test_forcestop.py
会真的起子进程，需要有桌面会话。
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image

from imgwb import app as app_mod
from imgwb import perf
from imgwb.app import ImageProcessorApp

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None
app_mod.messagebox.askokcancel = lambda *a, **k: True

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def alive(pids):
    """这些 pid 里还有几个活着。用 kill_pid 的探活分支，不真的杀。"""
    n = 0
    for pid in pids:
        try:
            # expect_image 故意给一个不可能匹配的名字：kill_pid 会走到「名字对
            # 不上就不动手」那条分支，于是只探活、不杀。
            perf.kill_pid(pid, expect_image='__never_matches__.exe')
        except Exception:
            pass
    # 上面那轮只是确认调用不炸；真正的存活判断用下面这个
    for pid in pids:
        if _is_alive(pid):
            n += 1
    return n


def _is_alive(pid):
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == 259          # STILL_ACTIVE
        return False
    finally:
        k32.CloseHandle(h)


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, '素材')
    os.makedirs(src)
    # 必须超过 PROCESS_THRESHOLD 才会走多进程 —— 这个测试要的就是真子进程。
    # 图要够大，保证点下去的时候确实有任务在跑。
    N = 260
    print(f'准备 {N} 张 1400x1400 ...')
    base = Image.new('RGB', (1400, 1400))
    px = base.load()
    for y in range(0, 1400, 7):
        for x in range(0, 1400, 3):
            px[x, y] = ((x * 3) % 256, (y * 5) % 256, (x ^ y) % 256)
    for i in range(N):
        base.save(os.path.join(src, f'p{i:04d}.jpg'), quality=92)

    app = ImageProcessorApp()
    app.root.withdraw()
    app.src_folder.set(src)
    app.out_mode.set('new')
    app.new_folder_path.set(os.path.join(work, 'out'))
    app.compress_only.set(True)
    app.quality_val.set('极致保真 (100%)')       # 编码慢一点，好抓到在跑的任务
    app.safe_write.set(False)                    # 直写模式 —— 就是出问题的那档
    app.timeout_val.set('关闭 (一直等)')

    app.start_processing()

    # 必须跑真正的 mainloop —— 工作线程全靠 root.after() 回到 UI 线程，
    # 用 update() 硬转会直接抛「main thread is not in main loop」。
    seen = set()
    state = {'phase': 1, 'forced_at': None, 'stopped_at': None}
    deadline = time.time() + 90

    def poll():
        if time.time() > deadline:
            app.root.quit()
            return

        if state['phase'] == 1:
            app._remember_children()
            seen.update(app._child_pids)
            if len(seen) >= 4 and app.processed_count > 0:
                # 先取消（直写模式下不杀进程，收尾会去等在途任务），再强制停止。
                # 这正是用户的操作顺序，也是老版本卡死的那条路径。
                app.request_cancel()
                state['phase'] = 2
                app.root.after(300, poll)
                return

        elif state['phase'] == 2:
            state['forced_at'] = time.time()
            app.force_stop()
            state['phase'] = 3

        elif state['phase'] == 3:
            if not app.is_running:
                state['stopped_at'] = time.time()
                app.root.after(200, app.root.quit)
                return

        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()

    check('真的起了多个工作子进程', len(seen) >= 4, f'{len(seen)} 个')
    print(f'  子进程 pid: {sorted(seen)}')

    if state['forced_at'] and state['stopped_at']:
        stop_secs = state['stopped_at'] - state['forced_at']
    else:
        stop_secs = float('inf')
    check('强制停止在 10 秒内真的停下来', not app.is_running and stop_secs < 10,
          f'{stop_secs:.1f} 秒' if stop_secs != float('inf')
          else '超时都没停下来')

    # terminate() 是异步的，给系统一点时间收尸
    for _ in range(40):
        if alive(seen) == 0:
            break
        time.sleep(0.1)

    left = alive(seen)
    check('工作子进程一个不剩', left == 0, f'还活着 {left} 个')

    print('\n=== 关窗口后不能留下孤儿进程 ===')
    app.on_close()
    for _ in range(40):
        if alive(seen) == 0:
            break
        time.sleep(0.1)
    left2 = alive(seen)
    check('关窗后仍然一个不剩', left2 == 0, f'还活着 {left2} 个')

    # kill_pid 的防误杀：名字对不上时绝不动手。pid 会被系统回收再分配，
    # 少了这道校验，收尾时可能一刀砍在无关进程上。
    me = os.getpid()
    check('名字对不上就不杀（防 pid 回收误伤）',
          perf.kill_pid(me, expect_image='__never_matches__.exe') is False
          and _is_alive(me))

    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing

    multiprocessing.freeze_support()
    main()
