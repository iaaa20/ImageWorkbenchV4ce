"""取消要立刻见效（安全写入时），且不留残渣。

用法:  python tests\test_cancel.py
会真的起子进程，需要桌面会话。
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

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, '批量')
    os.makedirs(src)
    N = PROCESS_THRESHOLD + 120
    for i in range(N):
        Image.new('RGB', (400, 300), (i % 256, 90, 150)).save(
            os.path.join(src, f'p{i:05d}.jpg'))
    before = sorted(os.listdir(src))
    print(f'{N} 个文件')

    app = ImageProcessorApp()
    app.root.withdraw()
    app.src_folder.set(src)
    app.apply_preset('compress')          # 预设会开安全写入

    # 等子进程起来
    t0 = time.time()

    def wait():
        procs = (getattr(app.executor, '_processes', None) or {}) if app.executor else {}
        if procs or time.time() - t0 > 30:
            app.root.quit()
            return
        app.root.after(50, wait)

    app.root.after(50, wait)
    app.root.mainloop()
    print(f'  子进程 {len((getattr(app.executor, "_processes", None) or {}))} 个')

    print('\n=== 点取消，计时 ===')
    cancel_at = time.time()
    app.request_cancel()

    deadline = time.time() + 30

    def poll():
        if not app.is_running or time.time() > deadline:
            app.root.after(300, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()
    took = time.time() - cancel_at

    check('取消后处理确实停了', not app.is_running)
    check('取消在 8 秒内完成', took < 8, f'{took:.1f} 秒')
    check('源目录一个文件没少', sorted(os.listdir(src)) == before,
          f'{len(os.listdir(src))}/{len(before)}')

    out = os.path.join(work, '批量_已处理')
    leftover = [n for n in os.listdir(out) if '.~' in n] if os.path.isdir(out) else []
    check('没有残留临时文件', not leftover, f'{len(leftover)} 个')

    log = app.log_text.get('1.0', 'end')
    check('日志说明了终止了几个进程', '已终止' in log and '撤回' in log,
          [l for l in log.splitlines() if '已终止' in l][:1])

    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing

    multiprocessing.freeze_support()
    main()
