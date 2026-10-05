"""暂停/断点继续、强制停止，以及内存相关的保护措施。

用法:  python tests\test_pause.py
会真的起子进程，需要桌面会话。
"""
import inspect
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image

from imgwb import app as app_mod
from imgwb import perf
from imgwb.app import ImageProcessorApp
from imgwb.config import PROCESS_THRESHOLD

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None
app_mod.messagebox.askokcancel = lambda *a, **k: True

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def spin(app, seconds):
    """跑 mainloop 一小段时间，让工作线程的 after() 回调有机会执行。"""
    app.root.after(int(seconds * 1000), app.root.quit)
    app.root.mainloop()


def main():
    print('=== 内存读数 ===')
    used, avail, total = perf.memory_status()
    check('读得到系统内存', used is not None and total,
          f'已用 {used:.0%}，可用 {(avail or 0) / 2**30:.1f} GB / '
          f'{(total or 0) / 2**30:.1f} GB' if used is not None else '读不到')
    check('压力等级合法', perf.memory_pressure() in ('ok', 'warn', 'critical'),
          str(perf.memory_pressure()))

    print('\n=== 子进程回收（内存不再一路涨的关键）===')
    supported = ('max_tasks_per_child'
                 in inspect.signature(ProcessPoolExecutor.__init__).parameters)
    print(f'  当前 Python {sys.version.split()[0]}，支持 max_tasks_per_child: {supported}')
    check('回收阈值设了个合理值', 20 <= perf.TASKS_PER_CHILD <= 500,
          f'每 {perf.TASKS_PER_CHILD} 个任务换一次子进程')

    # 要够慢才观察得到暂停：几百张小图一秒就跑完了。用大图 + 节能模式
    # （并发压到最低）把节奏拉长到十几秒。
    work = tempfile.mkdtemp()
    src = os.path.join(work, '批量')
    os.makedirs(src)
    N = PROCESS_THRESHOLD + 40
    base = Image.new('RGB', (2400, 1800))
    px = base.load()
    for y in range(0, 1800, 3):
        for x in range(0, 2400, 3):
            px[x, y] = (x % 256, y % 256, 128)
    for i in range(N):
        base.save(os.path.join(src, f'p{i:05d}.jpg'), quality=92)
    print(f'\n准备了 {N} 个 2400x1800 的文件')

    app = ImageProcessorApp()
    app.root.withdraw()
    app.src_folder.set(src)
    # 必须在 apply_preset 之前设：它会立刻取设置快照并开跑
    app.perf_mode.set(perf.ECO)
    app.apply_preset('compress')

    print('\n=== 暂停 ===')
    spin(app, 2.5)
    check('处理已经跑起来了', app.is_running and app.processed_count > 0,
          f'{app.processed_count}/{app.total_count}')
    check('暂停按钮可用', str(app.btn_pause.cget('state')) == 'normal')
    check('强制停止按钮可用', str(app.btn_stop.cget('state')) == 'normal')

    app.toggle_pause()
    check('已进入暂停', app.paused is True)
    check('按钮变成「继续」', '继续' in app.btn_pause.cget('text'),
          app.btn_pause.cget('text'))

    spin(app, 1.0)
    at_pause = app.processed_count
    spin(app, 2.0)
    drift = app.processed_count - at_pause
    check('暂停后不再推进', drift <= 0,
          f'暂停 3 秒里又多完成了 {drift} 个（在跑的那几张收尾属正常）')
    check('暂停期间处理仍未结束', app.is_running)

    print('\n=== 断点继续 ===')
    before_resume = app.processed_count
    app.toggle_pause()
    check('已恢复', app.paused is False)
    spin(app, 2.0)
    check('从断点接着往下走', app.processed_count > before_resume,
          f'{before_resume} -> {app.processed_count}')
    check('没有重头再来', app.processed_count > before_resume,
          f'继续后 {app.processed_count}，断点是 {before_resume}')

    print('\n=== 强制停止 ===')
    procs = list((getattr(app.executor, '_processes', None) or {}).values())
    alive_before = [p for p in procs if p.is_alive()]
    t0 = time.time()
    app.force_stop()

    deadline = time.time() + 20

    def poll():
        if not app.is_running or time.time() > deadline:
            app.root.after(300, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()
    took = time.time() - t0

    check('起过子进程', len(alive_before) > 0, f'{len(alive_before)} 个')
    check('强制停止后处理结束', not app.is_running)
    check('停得够快', took < 10, f'{took:.1f} 秒')

    for _ in range(50):
        if not any(p.is_alive() for p in alive_before):
            break
        time.sleep(0.1)
    still = [p.pid for p in alive_before if p.is_alive()]
    check('全部子进程已终止', not still, f'仍活着 {still}' if still else '')

    out = os.path.join(work, '批量_已处理')
    leftover = [n for n in os.listdir(out) if '.~' in n] if os.path.isdir(out) else []
    check('没有残留临时文件', not leftover, f'{len(leftover)} 个')
    check('源目录一个没少', len(os.listdir(src)) == N, f'{len(os.listdir(src))}/{N}')

    print('\n=== 按钮在收尾后都禁用了 ===')
    app.root.update()
    check('暂停按钮已禁用', str(app.btn_pause.cget('state')) == 'disabled')
    check('强制停止已禁用', str(app.btn_stop.cget('state')) == 'disabled')

    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing

    multiprocessing.freeze_support()
    main()
