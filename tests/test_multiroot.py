"""拖入多个文件夹时各算一条源路径；日志框的滚动链。

用法:  python tests\test_multiroot.py
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

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


class Drop:
    """伪造一个 tkdnd 的拖拽事件。"""

    def __init__(self, data):
        self.data = data


work = tempfile.mkdtemp()
# 两个位置完全不相邻的文件夹，各自还有子目录
a = os.path.join(work, '相册A')
b = os.path.join(work, '别处', '相册B')
os.makedirs(os.path.join(a, '春天'))
os.makedirs(os.path.join(b, '夏天'))
for d, n in ((a, 2), (os.path.join(a, '春天'), 2), (b, 2), (os.path.join(b, '夏天'), 2)):
    for i in range(n):
        Image.new('RGB', (40, 30), (i * 80 % 256, 90, 150)).save(
            os.path.join(d, f'p{i}.jpg'))

app = ImageProcessorApp()
app.root.withdraw()

print('=== 拖入两个文件夹 ===')
app.handle_drop(Drop('{%s} {%s}' % (a, b)))
check('识别出两条源路径', len(app.dropped_roots) == 2,
      str([os.path.basename(r) for r in app.dropped_roots]))
check('两边的文件都收进来了', len(app.dropped_paths) == 8,
      f'{len(app.dropped_paths)} 个')
check('来源框显示路径条数', '2 个路径' in app.src_folder.get(), app.src_folder.get())
log = app.log_text.get('1.0', 'end')
check('日志把两条路径都列出来了', a in log and b in log)

print('\n=== 导出：各自保留结构、各得一个输出文件夹 ===')
out = os.path.join(work, '输出')
os.makedirs(out)
app.out_mode.set('new')
app.new_folder_path.set(out)
app.keep_structure.set(True)
app.dir_suffix_val.set('_已处理')
app.name_mode.set('number')
app.rotate_val.set('不旋转')
app.compress_only.set(False)
app.is_ico.set(False)

snap = app.snapshot_settings()
from imgwb.tasks import build_tasks, scan_files

files = scan_files(a, snap, None)
tasks = build_tasks(files, a, out, snap)
dests = sorted({os.path.relpath(os.path.dirname(t.dst), out) for t in tasks})
print('  输出目录:')
for d in dests:
    print(f'    {d}')

check('两个文件夹各得一个输出根',
      any(x.startswith('相册A_已处理') for x in dests)
      and any(x.startswith('相册B_已处理') for x in dests), str(dests))
check('各自的子目录结构保留',
      any(x.endswith(os.path.join('相册A_已处理', '春天')) or
          x == os.path.join('相册A_已处理', '春天') for x in dests)
      and any(x == os.path.join('相册B_已处理', '夏天') for x in dests), str(dests))
check('没有跑到输出目录外面',
      all(not x.startswith('..') for x in dests), str(dests))

print('\n=== 文本输入父子目录：真实处理只输出每个文件一次 ===')
app.dropped_paths = []
app.dropped_roots = []
app.src_folder.set(a + ';' + os.path.join(a, '春天'))
nested_out = os.path.join(work, '父子目录输出')
app.new_folder_path.set(nested_out)
app.name_mode.set('keep')
app.scan_subdirs.set(True)
app.start_processing()
deadline = time.monotonic() + 20

def poll_nested():
    if not app.is_running or time.monotonic() > deadline:
        app.root.after(200, app.root.quit)
    else:
        app.root.after(30, poll_nested)

app.root.after(30, poll_nested)
app.root.mainloop()
check('父子来源处理按时结束', not app.is_running)
nested_files = [os.path.join(d, n) for d, _, fs in os.walk(nested_out)
                for n in fs if n.endswith('.jpg')]
check('四个输入只产生四个输出', len(nested_files) == 4, str(nested_files))
check('处理总数也是四个', app.total_count == 4, str(app.total_count))
check('子来源仍使用自己的输出根',
      sum(os.path.basename(os.path.dirname(p)) == '春天_已处理'
          for p in nested_files) == 2)

# 不递归时父目录和子目录的直接文件都要保留，不能靠删掉子根来去重。
snap = app.snapshot_settings()
app.scan_subdirs.set(False)
shallow = scan_files(a, app.snapshot_settings(), None)
check('不递归时两条来源的直接文件都保留', len(shallow) == 4, str(shallow))
app.scan_subdirs.set(True)

from imgwb import api
one = os.path.join(a, 'p0.jpg')
duplicates = [one, os.path.join(a, '.', 'p0.jpg')]
if sys.platform == 'win32':
    duplicates.append(one.upper())
plan = api.plan(duplicates, output='new', dest=os.path.join(work, 'api输出'))
check('API 重复文件及 Windows 大小写别名只计划一次', plan['scanned'] == 1, str(plan))
result = api.run(duplicates, output='new', dest=os.path.join(work, 'api输出'),
                 name='keep', workers=1)
check('API 实际也只导出一份', result['ok'] and len(result['outputs']) == 1,
      str(result))

print('\n=== 日志框：小一点，且滚到头能带动页面 ===')
app.root.deiconify()
app.root.geometry('720x300')
app.root.update()
app.root.update_idletasks()

check('日志框行数已收窄', int(app.log_text.cget('height')) <= 8,
      f"height={app.log_text.cget('height')}")

canvas = app._canvas
total = float(str(canvas.cget('scrollregion')).split()[3])
visible = canvas.winfo_height()
print(f'  内容 {total:.0f}px / 可视 {visible}px / 可滚 {total - visible:.0f}px')

for i in range(60):
    app.log(f'第 {i} 行日志，用来把日志框填满')
app.flush_logs()
app.root.update_idletasks()


class Wheel:
    def __init__(self, delta):
        self.delta = delta
        self.widget = app.log_text


# 日志里有内容可滚时，滚轮**归日志**，页面一动不动。
# 早先的规则是「滚到头就让给页面」，但日志每来一行就自动滚到底 ——
# 它几乎永远处在「到头」状态，于是用户在日志上往下滚、动的却是整个页面，
# 感觉就像日志的滚轮被绑到窗口上了。用户报过这一条。
app.log_text.see('end')
app.root.update_idletasks()
canvas.yview_moveto(0.2)
app.root.update_idletasks()
top = canvas.yview()[0]
r = app._on_log_wheel(Wheel(-120))
app.root.update_idletasks()
moved = (canvas.yview()[0] - top) * total
first, last = app.log_text.yview()
has_scroll = not (first <= 0.001 and last >= 0.999)
check('日志有内容可滚时，滚轮不许带动页面',
      has_scroll and moved == 0 and r is None,
      f'可滚={has_scroll} 页面移动 {moved:.1f}px, 返回 {r!r}')

# 日志整个装得下、根本没得滚时，才把滚轮让给页面 —— 否则小窗口下
# 日志占满可视区，滚轮全被它吃掉，页面根本翻不动。
app.log_text.configure(state='normal')
_saved = app.log_text.get('1.0', 'end')
app.log_text.delete('1.0', 'end')
app.log_text.insert('end', '一行' + chr(10))
app.log_text.configure(state='disabled')
app.root.update_idletasks()
top = canvas.yview()[0]
r2 = app._on_log_wheel(Wheel(-120))
app.root.update_idletasks()
moved2 = (canvas.yview()[0] - top) * total
check('日志没得滚时，滚轮让给页面', moved2 > 0 and r2 == 'break',
      f'{moved2:.1f}px, 返回 {r2!r}')

# **把日志内容还回去** —— 后面还有断言依赖「日志有得滚」这个前提，
# 不还原的话它们会莫名其妙地红，而且看起来像产品坏了。
app.log_text.configure(state='normal')
app.log_text.delete('1.0', 'end')
app.log_text.insert('end', _saved)
app.log_text.configure(state='disabled')
app.root.update_idletasks()

# 日志还能自己滚时，页面不该动
app.log_text.yview_moveto(0.0)
app.root.update_idletasks()
canvas.yview_moveto(0.2)
app.root.update_idletasks()
top = canvas.yview()[0]
r = app._on_log_wheel(Wheel(-120))
app.root.update_idletasks()
check('日志没到头时页面不动',
      abs(canvas.yview()[0] - top) < 1e-9 and r is None, f'返回 {r!r}')

app.root.destroy()
shutil.rmtree(work, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
