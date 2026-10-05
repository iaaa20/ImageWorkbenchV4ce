"""跑完之后状态栏必须说实话 —— 尤其是崩了的时候。

用户报「一点开始执行直接结束」，截图上状态栏写着绿色的**「完成 0/975」**。
真相是工作线程在任何任务完成之前就抛了异常：那时 error_list 还是空的，
于是 finally 里的分支判断掉进了「完成」那一支。报错其实弹过窗、也写进了
日志框，但日志框只有七行，用户再点一次执行就把它挤没了 —— 于是"完成"成了
唯一还留在屏幕上的信息，把人引向完全错误的方向。

四种结局各有各的说法，一种都不许串味：
    崩溃   -> 出错了，停在 N/M（红）
    取消   -> 已取消，停在 N/M
    有失败 -> 完成，N 个失败（黄）
    没跑完 -> 异常结束，只处理了 N/M（红）
    正常   -> 完成 N/M（绿）

用法:  python tests\\test_outcome.py
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

boxes = []
app_mod.messagebox.showinfo = lambda *a, **k: boxes.append(('info',) + a)
app_mod.messagebox.showwarning = lambda *a, **k: boxes.append(('warn',) + a)
app_mod.messagebox.showerror = lambda *a, **k: boxes.append(('error',) + a)
app_mod.messagebox.askokcancel = lambda *a, **k: True

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


work = tempfile.mkdtemp()


def make_batch(n, broken=0):
    d = tempfile.mkdtemp(dir=work)
    for i in range(n):
        Image.new('RGB', (60, 40), (i * 7 % 256, 90, 160)).save(
            os.path.join(d, f'p{i}.jpg'), quality=95)
    for i in range(broken):
        with open(os.path.join(d, f'bad{i}.jpg'), 'wb') as fh:
            fh.write(b'\xff\xd8\xff\xe0 not a jpeg at all')
    return d


def run(app, src, patch=None):
    boxes.clear()
    app.src_folder.set(src)
    app.out_mode.set('new')
    app.new_folder_path.set(tempfile.mkdtemp(dir=work))
    app.compress_only.set(True)
    saved = app_mod.process_single_image
    if patch:
        app_mod.process_single_image = patch
    try:
        t0 = time.time()
        app.start_processing()

        def poll():
            if not app.is_running or time.time() - t0 > 120:
                app.root.after(300, app.root.quit)
                return
            app.root.after(50, poll)

        app.root.after(50, poll)
        app.root.mainloop()
    finally:
        app_mod.process_single_image = saved
    return app.status.cget('text')


app = ImageProcessorApp()
app.root.withdraw()
app._ask_merge_choice = lambda *a, **k: 'merge'

print('=== 1. 正常跑完 ===')
text = run(app, make_batch(6))
check('说「完成 N/M」', text.startswith('完成 6/6'), repr(text))

print('\n=== 2. 有文件失败 ===')
text = run(app, make_batch(4, broken=2))
check('说「完成，N 个失败」', '失败' in text, repr(text))

print('\n=== 3. 工作线程崩了（任何任务完成之前）===')
# 这正是用户遇到的那次：error_list 是空的，旧代码会报「完成 0/N」。
def boom(args):
    raise RuntimeError('模拟崩溃')


real_submit = app_mod.ProcessPoolExecutor


class BoomPool:
    def __init__(self, *a, **k):
        raise RuntimeError('模拟：建进程池就炸')


src = make_batch(5)
app_mod.ProcessPoolExecutor = BoomPool
try:
    # 5 个文件走线程池，所以直接让派发循环炸：替掉 futures_wait
    real_wait = app_mod.futures_wait
    app_mod.futures_wait = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('模拟：派发循环炸了'))
    text = run(app, src)
    app_mod.futures_wait = real_wait
finally:
    app_mod.ProcessPoolExecutor = real_submit

check('不许报「完成」', not text.startswith('完成'), repr(text))
check('要说「出错」并指向日志', '出错' in text and '日志' in text, repr(text))
check('弹了错误窗', any(b[0] == 'error' for b in boxes),
      str([b[0] for b in boxes]))
err = next((b for b in boxes if b[0] == 'error'), None)
check('错误窗里带了日志路径', bool(err) and 'imgwb.log' in str(err),
      str(err)[:110] if err else '没有')

print('\n=== 4. 取消 ===')
big = make_batch(40)


def slow(args):
    time.sleep(0.05)
    return app_mod.process_single_image_real(args)


app_mod.process_single_image_real = app_mod.process_single_image
app.src_folder.set(big)
app.out_mode.set('new')
app.new_folder_path.set(tempfile.mkdtemp(dir=work))
boxes.clear()
app_mod.process_single_image = slow
t0 = time.time()
app.start_processing()


def cancel_soon():
    if app.processed_count >= 3:
        app.request_cancel()
    elif app.is_running and time.time() - t0 < 60:
        app.root.after(30, cancel_soon)


def poll2():
    if not app.is_running or time.time() - t0 > 90:
        app.root.after(300, app.root.quit)
        return
    app.root.after(50, poll2)


app.root.after(30, cancel_soon)
app.root.after(50, poll2)
app.root.mainloop()
app_mod.process_single_image = app_mod.process_single_image_real
text = app.status.cget('text')
check('说「已取消，停在 N/M」', '已取消' in text, repr(text))

app.shutdown_workers()
app.root.destroy()
shutil.rmtree(work, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
