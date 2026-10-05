"""关窗口后不能留下后台进程。

这是个会真实起子进程的用例：跑一批超过 PROCESS_THRESHOLD 的任务，等子进程
起来后模拟点关闭，然后确认它们都没了。

用法:  python tests\test_shutdown.py
会真的建出窗口，需要有桌面会话。
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
from imgwb.app import ImageProcessorApp
from imgwb.config import PROCESS_THRESHOLD

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None
app_mod.messagebox.askokcancel = lambda *a, **k: True     # 关窗确认一律「是」

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def children_of(app):
    ex = app.executor
    procs = (getattr(ex, '_processes', None) or {}) if ex is not None else {}
    return list(procs.values())


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, '大批量')
    os.makedirs(src)
    N = PROCESS_THRESHOLD + 60          # 必须超过阈值才会走多进程
    for i in range(N):
        Image.new('RGB', (240, 180), (i % 256, 90, 150)).save(
            os.path.join(src, f'p{i:05d}.jpg'))
    print(f'准备了 {N} 个文件（多进程阈值 {PROCESS_THRESHOLD}）')

    app = ImageProcessorApp()
    app.root.withdraw()

    # 空闲时先验一次：没有线程池时不该报错，也没什么可杀的
    check('空闲时 shutdown_workers 返回 0 且不报错', app.shutdown_workers() == 0)

    app.src_folder.set(src)
    app.apply_preset('compress')

    # 等子进程真的起来
    alive = []
    deadline = time.time() + 30


    def wait_for_children():
        if time.time() > deadline or (app.executor is not None and children_of(app)):
            app.root.quit()
            return
        app.root.after(50, wait_for_children)


    app.root.after(50, wait_for_children)
    app.root.mainloop()
    alive = [p for p in children_of(app) if p.is_alive()]
    pids = [p.pid for p in alive]
    print(f'\n起了 {len(alive)} 个工作子进程: {pids}')
    check('确实进入了多进程模式', len(alive) > 0, f'{len(alive)} 个')

    print('\n=== 处理途中关掉窗口 ===')
    app.on_close()

    # on_close 会排 _await_worker_exit，跑掉这些回调
    deadline2 = time.time() + 12


    def spin():
        try:
            if time.time() > deadline2 or not app.root.winfo_exists():
                raise SystemExit
        except Exception:
            return
        app.root.after(100, spin)


    try:
        app.root.after(100, spin)
        app.root.mainloop()
    except Exception:
        pass

    # 给操作系统一点回收时间
    for _ in range(50):
        if not any(p.is_alive() for p in alive):
            break
        time.sleep(0.1)

    still = [p.pid for p in alive if p.is_alive()]
    check('关窗后没有残留的工作子进程', not still, f'仍活着: {still}' if still else '全部已退出')

    # 同一进程里销毁 Tk 之后再建第二个 app 会炸在 ttkbootstrap 的样式注册表上
    # （它绑在首个 Tk 实例），所以窗口销毁这一项就在这个实例上验。
    try:
        destroyed = not app.root.winfo_exists()
    except Exception:
        destroyed = True
    check('关窗后窗口确实销毁了', destroyed)

    print('\n=== 临时文件有没有清干净 ===')
    out = os.path.join(work, '大批量_已处理')
    leftover = []
    if os.path.isdir(out):
        leftover = [n for n in os.listdir(out) if '.~' in n]
    check('没有残留的 .~ 临时文件', not leftover, f'{len(leftover)} 个' if leftover else '')

    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing

    # 必须有：ProcessPoolExecutor 用 spawn 起子进程，子进程会重新 import
    # 本模块。没有这层保护的话整个测试会被递归执行一遍又一遍。
    multiprocessing.freeze_support()
    main()
