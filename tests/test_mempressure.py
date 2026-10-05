"""内存告急时必须**当场**降并发，不能等下一次检查。

曾经写成 `if warn or critical: ... elif critical: 降并发`，而 critical 也落在
前一个分支里，elif 根本不求值 —— 第一次撞到 critical 只告警不降并发，真正降
要等下一次检查（100 张之后）。系统那时已经在换页，这 100 张可能就是压垮它的
那一下。

用法:  python tests\test_mempressure.py
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


def main():
    # 内存检查每 100 个任务做一次，所以要跑得够多才轮得到第二次检查 ——
    # 这样才能区分「第一次就降」和「等到第二次才降」。
    work = tempfile.mkdtemp()
    src = os.path.join(work, '批量')
    os.makedirs(src)
    N = 260
    for i in range(N):
        Image.new('RGB', (60, 45), (i % 256, 90, 150)).save(
            os.path.join(src, f'p{i:04d}.jpg'))
    print(f'准备了 {N} 个小文件（内存检查每 100 个一次）')

    # 假装系统内存一直告急
    calls = []
    original = perf.memory_pressure

    def fake_pressure():
        calls.append(len(calls))
        return 'critical'

    app_mod.perf.memory_pressure = fake_pressure

    app = ImageProcessorApp()
    app.root.withdraw()
    app.src_folder.set(src)
    app.apply_preset('compress')

    deadline = time.time() + 180

    def poll():
        if not app.is_running or time.time() > deadline:
            app.root.after(300, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()
    app_mod.perf.memory_pressure = original

    log = app.log_text.get('1.0', 'end')
    lines = [ln for ln in log.splitlines() if ln.strip()]

    print(f'\n内存检查被调用了 {len(calls)} 次')
    check('处理跑完了', not app.is_running and app.processed_count == N,
          f'{app.processed_count}/{N}')
    check('内存检查确实执行过', len(calls) >= 2,
          f'{len(calls)} 次（{N} 个任务，每 100 一次）')

    reduce_at = next((i for i, ln in enumerate(lines) if '并发压到 1' in ln), None)
    warn_at = next((i for i, ln in enumerate(lines) if '系统内存吃紧' in ln), None)

    check('critical 时降了并发', reduce_at is not None,
          lines[reduce_at] if reduce_at is not None else '日志里没有降并发的记录')
    # 关键断言：降并发必须是「撞到 critical 的那一次」就发生。修复前这条会挂 ——
    # 第一次只会打出泛泛的「系统内存吃紧」，降并发要等下一次检查。
    check('没有先打泛泛告警、把降并发拖到下一次', warn_at is None,
          f'先出现了「系统内存吃紧」（第 {warn_at} 行）' if warn_at is not None else '')

    print('\n=== 只是 warn（还没到 critical）时不该降并发 ===')
    for name in os.listdir(work):
        if name != '批量':
            shutil.rmtree(os.path.join(work, name), ignore_errors=True)

    app_mod.perf.memory_pressure = lambda: 'warn'
    app.src_folder.set(src)
    app.log_text.configure(state='normal')
    app.log_text.delete('1.0', 'end')
    app.log_text.configure(state='disabled')
    app.apply_preset('compress')

    deadline = time.time() + 180
    app.root.after(50, poll)
    app.root.mainloop()
    app_mod.perf.memory_pressure = original

    log2 = app.log_text.get('1.0', 'end')
    check('warn 时只告警、不降并发', '并发压到 1' not in log2 and '系统内存吃紧' in log2,
          '降了并发' if '并发压到 1' in log2 else
          ('没有告警' if '系统内存吃紧' not in log2 else ''))

    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing

    multiprocessing.freeze_support()
    main()
