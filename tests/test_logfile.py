"""系统日志：操作、处理记录、异常，三样都必须落到磁盘。

界面上那个日志框关掉窗口就没了。用户报问题时能拿出来的只有磁盘上这个文件，
所以它必须能回答三个问题：**当时点了什么、跑了什么参数、哪里出的错。**

顺带盯住第三轮报告的两个严重项：
  X-01 点开头的文件不许凭空消失（Windows 靠隐藏属性判断，不是靠文件名）
  X-02 导出目录落在来源里会让文件每跑一次翻一倍，必须拦下

用法:  python tests\\test_logfile.py
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

from imgwb import api, logfile
from imgwb import app as app_mod
from imgwb.app import ImageProcessorApp
from imgwb.tasks import output_escape_error

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None
app_mod.messagebox.askokcancel = lambda *a, **k: True

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


work = tempfile.mkdtemp()
src = os.path.join(work, '素材')
os.makedirs(src)
for i in range(4):
    Image.new('RGB', (60, 40), (i * 50, 90, 160)).save(
        os.path.join(src, f'p{i}.jpg'), quality=95)
# 一张损坏的，用来验证"失败也要进日志"
with open(os.path.join(src, 'broken.jpg'), 'wb') as fh:
    fh.write(b'\xff\xd8\xff\xe0 not really a jpeg')

path = logfile.setup()
print(f'日志文件: {path}')
check('日志文件路径可用', bool(path) and os.path.isdir(os.path.dirname(path)))


def read_log():
    if not path or not os.path.exists(path):
        return ''
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read()


before = len(read_log())

print('\n=== 1. 操作要进日志 ===')
logfile.op('测试操作', '细节在这里')
text = read_log()
check('操作行写进去了', '[操作] 测试操作' in text and '细节在这里' in text)

print('\n=== 2. 异常要带完整调用栈 ===')
try:
    raise ValueError('故意抛的')
except ValueError as exc:
    logfile.exception('单元测试', exc)
text = read_log()
check('异常写进去了', '[异常] 单元测试' in text)
check('带了调用栈', 'Traceback' in text and 'ValueError: 故意抛的' in text,
      '只有一句 str(exc) 的话，报问题的人给不出栈，我们只能靠猜')

print('\n=== 3. 跑一批：参数、每一行处理记录、每一个失败都要在 ===')
app = ImageProcessorApp()
app.root.withdraw()
app._ask_merge_choice = lambda *a, **k: 'merge'
app.src_folder.set(src)
app.out_mode.set('new')
app.new_folder_path.set(os.path.join(work, 'out'))
app.compress_only.set(True)
app.start_processing()

deadline = time.time() + 180
def poll():
    if not app.is_running or time.time() > deadline:
        app.root.after(300, app.root.quit)
        return
    app.root.after(50, poll)
app.root.after(50, poll)
app.root.mainloop()

text = read_log()
check('记录了「开始执行」和完整参数',
      '[操作] 开始执行' in text and '仅压缩 True' in text and '画质' in text,
      next((ln for ln in text.splitlines() if '开始执行' in ln), '没有'))
check('界面日志框的行也落盘了', '开始扫描文件' in text and '完成' in text)
check('失败的文件单独记了一条',
      '[失败]' in text and 'broken.jpg' in text,
      next((ln for ln in text.splitlines() if '[失败]' in ln), '没有'))

app.request_cancel() if app.is_running else None
app.shutdown_workers()

print('\n=== 4. X-02 导出目录不能落在来源里 ===')
inside = os.path.join(src, '输出')
check('导出=来源本身 -> 拒绝', bool(output_escape_error(src, src, True)))
check('导出在来源里面 -> 拒绝', bool(output_escape_error(src, inside, True)))
check('导出在来源旁边 -> 放行',
      output_escape_error(src, os.path.join(work, 'out'), True) == '')
check('原地修改不受影响', output_escape_error(src, src, False) == '')

try:
    api.plan(source=src, output='new', dest=src)
    check('API 也拒绝', False, '居然放行了')
except api.OptionError as exc:
    check('API 也拒绝（OptionError）', True, str(exc)[:40] + '...')

print('\n=== 5. X-01 点开头的文件不许凭空消失 ===')
dotdir = os.path.join(work, '点开头')
os.makedirs(dotdir)
for name in ('.jpg', '..jpg', '...jpg', '正常.jpg'):
    # 必须显式给 format：splitext('.jpg') == ('.jpg', '')，Pillow 推断不出格式。
    # 这个坑正是这批文件在流水线里的连带问题，pipeline 那边也做了同样的兜底。
    Image.new('RGB', (40, 30), (200, 40, 40)).save(
        os.path.join(dotdir, name), format='JPEG')
disk = sorted(os.listdir(dotdir))
res = api.plan(source=dotdir, output='new', dest=os.path.join(work, 'out2'),
               name='keep')
seen = sorted(os.path.basename(t['src']) for t in res['tasks'])
check('磁盘上有几个就该看到几个', len(seen) == len(disk),
      f'磁盘 {len(disk)} 个 {disk} / 看到 {len(seen)} 个 {seen}')

app.root.destroy()
shutil.rmtree(work, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
