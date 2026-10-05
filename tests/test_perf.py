"""性能模式：并发数、进程优先级、界面联动，以及日志的信息量。

用法:  python tests\test_perf.py
会真的建出窗口，需要有桌面会话。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from PIL import Image

from imgwb import app as app_mod
from imgwb import perf
from imgwb.app import ImageProcessorApp

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def pump(app, timeout=180):
    deadline = time.time() + timeout

    def poll():
        if not app.is_running or time.time() > deadline:
            app.root.after(400, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()


cpu = os.cpu_count() or 4
print(f'本机 {cpu} 个逻辑核\n')

print('=== 各模式的并发数 ===')
eco = perf.worker_count(perf.ECO)
bal = perf.worker_count(perf.BALANCED)
turbo = perf.worker_count(perf.TURBO)
custom = perf.worker_count(perf.CUSTOM, 3)
print(f'  节能 {eco} / 均衡 {bal} / 高性能 {turbo} / 自定义(3) {custom}')
check('节能最省', 1 <= eco <= max(1, cpu // 4))
check('高性能用满核心', turbo == cpu)
check('节能 < 高性能', eco < turbo or cpu == 1)
check('自定义听用户的', custom == 3)
check('并发数不会是 0', all(n >= 1 for n in (eco, bal, turbo, custom)))

print('\n=== 优先级级别 ===')
check('节能用后台优先级（含磁盘 I/O）',
      perf.priority_of(perf.ECO) == 'background')
check('高性能仍低于普通优先级', perf.priority_of(perf.TURBO) == 'below')
check('自定义也不抢占前台', perf.priority_of(perf.CUSTOM) == 'below')

print('\n=== 优先级真的落到子进程上了吗 ===')
probe = textwrap.dedent(r'''
    import sys
    sys.path.insert(0, r"%s")
    from imgwb import perf
    base = perf.current_priority()
    bg = perf.apply_priority("background")
    print("bg", base, bg, perf.current_priority())

    import subprocess
    inner = ("import sys; sys.path.insert(0, r'%s'); from imgwb import perf; "
             "print('below', perf.apply_priority('below'), perf.current_priority())")
    print(subprocess.run([sys.executable, "-c", inner],
                         capture_output=True, text=True).stdout.strip())
''' % (ROOT, ROOT))
r = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True)
lines = {ln.split()[0]: ln.split() for ln in r.stdout.strip().splitlines() if ln.split()}

if 'bg' in lines:
    # 后台模式不改优先级类，只能靠返回值判断成没成
    check('节能：后台模式调用成功', lines['bg'][2] in ('background', 'idle'),
          f'返回 {lines["bg"][2]}')
else:
    check('节能：后台模式调用成功', False, r.stderr.strip()[:150])

if 'below' in lines:
    # 这个会真的改优先级类，可以从外部观测到
    check('高性能/自定义：优先级类降到 BELOW_NORMAL',
          lines['below'][1] == 'below' and int(lines['below'][2]) == 0x4000,
          f'返回 {lines["below"][1]}，实测 0x{int(lines["below"][2]):X}')
else:
    check('高性能/自定义：优先级类降到 BELOW_NORMAL', False, r.stderr.strip()[:150])

print('\n=== 界面联动 ===')
app = ImageProcessorApp()
app.set_mode(advanced=True)
app.root.update()

check('默认是均衡模式', app.perf_mode.get() == perf.BALANCED, app.perf_mode.get())
app.perf_mode.set(perf.ECO)
app.update_ui()
app.root.update()
check('非自定义时并发数输入框置灰',
      str(app.spin_workers.cget('state')) == 'disabled',
      str(app.spin_workers.cget('state')))
check('说明文字跟着变', '节能' in app.lbl_perf.cget('text'), app.lbl_perf.cget('text'))

app.perf_mode.set(perf.CUSTOM)
app.update_ui()
app.root.update()
check('自定义时可以改并发数',
      str(app.spin_workers.cget('state')) == 'normal',
      str(app.spin_workers.cget('state')))

print('\n=== 日志框高度要适中 ===')
# 太矮看不到几条；太高会在小窗口下占满可视区，滚轮全被它吃掉、页面翻不动
# （滚到头后的接力见 test_multiroot.py）。
lines = int(app.log_text.cget('height'))
check('日志高度在 5~8 行之间', 5 <= lines <= 8, f'{lines} 行')

print('\n=== 跑一批，看日志有没有实质内容 ===')
app.root.withdraw()
work = tempfile.mkdtemp()
src = os.path.join(work, '批量')
os.makedirs(src)
for i in range(120):
    Image.new('RGB', (48, 36), (i * 2 % 256, 90, 150)).save(
        os.path.join(src, f'p{i:04d}.jpg'))

app.perf_mode.set(perf.ECO)
app.src_folder.set(src)
app.apply_preset('compress')
pump(app)

text = app.log_text.get('1.0', 'end')
check('日志写明了性能模式', '性能模式' in text and '节能' in text)
check('日志有进度行', '进度 ' in text and '张/秒' in text,
      [l for l in text.splitlines() if '进度 ' in l][:1])
check('日志有完成总结', '完成！' in text and '耗时' in text)
count = len([l for l in text.splitlines() if l.strip()])
check('日志行数明显多于以前的几条', count >= 8, f'{count} 行')

app.root.destroy()
shutil.rmtree(work, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
