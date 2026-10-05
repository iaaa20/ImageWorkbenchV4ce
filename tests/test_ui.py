"""界面回归：接线与状态、设置快照、重入保护、起始编号校验、TIFF 画质。

用法:  python tests\test_ui.py
会真的建出窗口（随即隐藏），所以需要有桌面会话。
"""
import os
import shutil
import sys

# 中文 Windows 控制台默认 cp936，编不了下面用到的 ✅ / ❌，直接照抄
# README 里的命令会当场 UnicodeEncodeError。这里就地把输出转成 UTF-8。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
import tempfile
import time

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from imgwb import app as app_mod
from imgwb import tasks
from imgwb.config import ProcessConfig
from imgwb.pipeline import get_quality_params, process_single_image

mod = app_mod          # 兼容下面用 mod.ImageProcessorApp / mod.messagebox 的写法

fails = []


def check(name, ok, detail=''):
    print(f'  {"✅" if ok else "❌"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


work = tempfile.mkdtemp()
Image.new('RGB', (40, 30), (200, 40, 40)).save(os.path.join(work, 'a.jpg'))

app = mod.ImageProcessorApp()
app.root.withdraw()
app.src_folder.set(work)
app.out_mode.set('original')

def pack_order():
    """step4 里各行的排列顺序。"""
    names = []
    for w in app.ico_detail_frame.master.pack_slaves():
        if w is app.ico_detail_frame:
            names.append('ICO面板')
        elif w is app.row_perf:
            names.append('并发数')
        else:
            names.append('其它')
    return names


print('=== 0a. ICO 选项面板的位置 ===')
check('初始隐藏', 'ICO面板' not in pack_order(), str(pack_order()))
app.is_ico.set(True)
app.root.update_idletasks()
order = pack_order()
check('勾选后插回「并发数」之前',
      'ICO面板' in order and order.index('ICO面板') < order.index('并发数'), str(order))
app.is_ico.set(False)
app.root.update_idletasks()
check('取消后再次隐藏', 'ICO面板' not in pack_order(), str(pack_order()))

print('\n=== 0b. 命名模式下的控件禁用状态 ===')
for mode, want in [('prefix', ('normal', 'normal', 'readonly')),
                   ('number', ('disabled', 'normal', 'readonly')),
                   ('keep', ('disabled', 'disabled', 'disabled'))]:
    app.name_mode.set(mode)
    app.update_ui()
    app.root.update_idletasks()
    got = (str(app.ent_pre.cget('state')), str(app.ent_start.cget('state')),
           str(app.cb_digit.cget('state')))
    check(f'{mode}: 前缀/起始编号/位数', got == want, f'实际 {got}')

print('\n=== 0c. 写入模式提示、ICO 尺寸、预览、关窗处理器、日志 ===')
app.safe_write.set(True)
app.update_ui()
t1 = app.lbl_safe.cget('text')
app.safe_write.set(False)
app.update_ui()
t2 = app.lbl_safe.cget('text')
check('两种写入模式提示不同且非空', bool(t1) and bool(t2) and t1 != t2, f'{t1!r} / {t2!r}')
app.safe_write.set(True)

labels = []
for row in app.ico_detail_frame.winfo_children():
    for w in row.winfo_children():
        try:
            labels.append(str(w.cget('text')))
        except Exception:
            pass
check('ICO 尺寸里没有 512（格式上限是 256）',
      '512' not in labels and '自动 512' not in labels)

app.name_mode.set('prefix')
app.prefix_val.set('IMG')
app.start_num.set(1)
app.update_ui()
check('编号预览正确', 'IMG_001' in app.lbl_preview.cget('text'),
      app.lbl_preview.cget('text'))
check('已注册 WM_DELETE_WINDOW', bool(app.root.protocol('WM_DELETE_WINDOW')))

app.log('测试一条')
app.flush_logs()
check('日志写入且队列清空',
      '测试一条' in app.log_text.get('1.0', 'end') and not app.log_queue)

print('\n=== 1. 设置快照：跑起来之后再改界面，不影响本批任务 ===')
app.prefix_val.set('BEFORE')
app.start_num.set(7)
snap = app.snapshot_settings()
app.prefix_val.set('AFTER')
app.start_num.set(999)
check('快照保留取快照那一刻的值',
      snap.prefix_val.get() == 'BEFORE' and snap.start_num.get() == 7,
      f'快照 prefix={snap.prefix_val.get()} start={snap.start_num.get()}，'
      f'界面已改成 {app.prefix_val.get()}/{app.start_num.get()}')

files = [os.path.join(work, 'a.jpg')]
tasks = tasks.build_tasks(files, work, work, snap)
check('build_tasks 用的是快照',
      os.path.basename(tasks[0][1]).startswith('BEFORE_007'),
      os.path.basename(tasks[0][1]))

print('\n=== 1b. 「动图也重编码」开关一路传到 ProcessConfig ===')
import imgwb.tasks as tasks_mod

check('开关在高级面板里', hasattr(app, 'chk_anim'))
check('默认关闭（动图原样带过）', app.reencode_anim.get() is False)

snap = app.snapshot_settings()
cfg_off = tasks_mod.build_tasks(files, work, work, snap)[0].config
check('默认下 ProcessConfig.reencode_animated 为 False',
      cfg_off.reencode_animated is False)

app.reencode_anim.set(True)
snap = app.snapshot_settings()
cfg_on = tasks_mod.build_tasks(files, work, work, snap)[0].config
check('打开后传到了 ProcessConfig', cfg_on.reencode_animated is True)

# 一键按钮面向不看参数的用户，必须把它复位 —— 否则上次手动打开的开关会
# 悄悄跟着一键压缩跑，动图又变回又慢又胀。
app.reencode_anim.set(True)
try:
    mod.messagebox.showinfo = lambda *a, **k: None
    real_start, app.start_processing = app.start_processing, lambda: None
    app.apply_preset('compress')
finally:
    app.start_processing = real_start
check('一键按钮会把它复位成关闭', app.reencode_anim.get() is False)

print('\n=== 1c. 导出目录非空时要先问一句（别静默混批）===')
import imgwb.app as _app_mod

conflict = tempfile.mkdtemp()
src_dir = os.path.join(conflict, '素材')
os.makedirs(src_dir)
Image.new('RGB', (20, 20), (9, 9, 9)).save(os.path.join(src_dir, 'x.jpg'))
out_root = os.path.join(conflict, 'out')
done_dir = os.path.join(out_root, '素材_已处理')
os.makedirs(done_dir)
with open(os.path.join(done_dir, '上一批.jpg'), 'wb') as f:
    f.write(b'x' * 10)

app.src_folder.set(src_dir)
app.out_mode.set('new')
app.new_folder_path.set(out_root)
app.keep_structure.set(True)
app.dir_suffix_val.set('_已处理')

check('算得出会写进哪个导出目录',
      app._output_dir_candidates(src_dir, out_root) == [done_dir],
      str(app._output_dir_candidates(src_dir, out_root)))

asked = []
app._ask_merge_choice = lambda total, where: (asked.append(total), None)[1]
check('用户取消就不开跑', app._confirm_output_dirs(src_dir, out_root) is False)
check('确认框收到了正确的文件数', asked == [1], str(asked))

app._ask_merge_choice = lambda total, where: 'merge'
check('选「并入」继续跑', app._confirm_output_dirs(src_dir, out_root) is True)

app._ask_merge_choice = lambda total, where: 'new'
app.dir_suffix_val.set('_已处理')
check('选「另建」会换一个空后缀', app._confirm_output_dirs(src_dir, out_root) is True)
check('后缀确实换了', app.dir_suffix_val.get() == '_已处理_2',
      app.dir_suffix_val.get())
# 空目录不该问
app.dir_suffix_val.set('_已处理')
os.remove(os.path.join(done_dir, '上一批.jpg'))
app._ask_merge_choice = lambda total, where: (_ for _ in ()).throw(
    AssertionError('空目录不该弹确认框'))
check('导出目录是空的就不问', app._confirm_output_dirs(src_dir, out_root) is True)
del app._ask_merge_choice
shutil.rmtree(conflict, ignore_errors=True)
app.src_folder.set(work)
app.out_mode.set('original')

print('\n=== 1d. 自动位数封顶 6 位 ===')
import imgwb.tasks as _t


class _V:
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


def padding_for(start, n):
    snap = app.snapshot_settings()
    snap.digit_val = _V('自动 (按数量)')
    snap.start_num = _V(start)
    snap.name_mode = _V('number')
    snap.out_mode = _V('original')
    files = [os.path.join(work, f'p{i}.jpg') for i in range(n)]
    for f in files:
        if not os.path.exists(f):
            Image.new('RGB', (8, 8)).save(f)
    return len(os.path.basename(_t.build_tasks(files, work, work, snap)[0].dst)) - 4


check('1234 个文件 -> 5 位', padding_for(1, 1234) == 5, str(padding_for(1, 1234)))
check('起始编号 99999 也只到 6 位（原来 7 位带两个前导零）',
      padding_for(99999, 3) == 6, str(padding_for(99999, 3)))
for f in os.listdir(work):
    if f.startswith('p') and f.endswith('.jpg'):
        os.remove(os.path.join(work, f))

print('\n=== 2. 快速点两次「开始执行」只能起一个任务 ===')
started = []


def fake_worker(*a, **k):
    started.append(1)
    time.sleep(0.4)


app.prefix_val.set('IMG')
app.start_num.set(1)
app.process_worker = fake_worker
app.start_processing()
running_now = app.is_running
app.start_processing()          # 紧接着第二次点击
time.sleep(0.6)
check('第一次点击后 is_running 立即为真（同步置位）', running_now is True)
check('只起了一个 worker', len(started) == 1, f'实际起了 {len(started)} 个')
del app.process_worker
app.is_running = False

print('\n=== 3. 起始编号被清空时给出明确提示，而不是抛 Tcl 错误 ===')
captured = []
orig_showerror = mod.messagebox.showerror
mod.messagebox.showerror = lambda title, msg, **k: captured.append(msg)
app.name_mode.set('number')
app.ent_start.configure(state='normal')
app.start_num.set(1)
app.ent_start.delete(0, 'end')          # 用户把框清空了
started.clear()
app.process_worker = fake_worker
app.start_processing()
time.sleep(0.1)
mod.messagebox.showerror = orig_showerror
check('弹出可读的提示', any('起始编号' in m for m in captured), str(captured))
check('没有启动处理', not started)
del app.process_worker
app.is_running = False

print('\n=== 4. TIFF 的画质选项不再是空操作 ===')
gq = get_quality_params
check('网络压缩 -> LZW', gq('.tif', '网络压缩 (85%)').get('compression') == 'tiff_lzw',
      str(gq('.tif', '网络压缩 (85%)')))
check('极致保真 -> 不动压缩方式', 'compression' not in gq('.tiff', '极致保真 (100%)'),
      str(gq('.tiff', '极致保真 (100%)')))

src = os.path.join(work, 'big.tif')
im = Image.new('RGB', (600, 400))
px = im.load()
for y in range(400):
    for x in range(600):
        px[x, y] = (x % 256, y % 256, 90)
im.save(src)                                     # 未压缩
dst = os.path.join(work, 'small.tif')
cfg = ProcessConfig('不旋转', '网络压缩 (85%)', True, False)
r = process_single_image((src, dst, cfg, True))
if r.success:
    os.replace(r.tmp_path, dst)
    a, b = os.path.getsize(src), os.path.getsize(dst)
    check('网络压缩确实变小', b < a, f'{a:,} -> {b:,} 字节 ({b / a:.0%})')
else:
    check('网络压缩确实变小', False, r.error_msg)

print('\n=== 5. 滚轮 ===')
canvas = app._canvas
# 主界面内容不多，默认尺寸下几乎没得滚（一格就被夹到底），会让下面的用例
# 失去意义。故意把窗口压小，制造出足够的可滚范围再测。窗口必须先显示出来 ——
# withdraw 状态下 Tk 不跑几何管理，geometry() 改了也不会反映到子控件上。
app.root.deiconify()
app.root.geometry('720x300')
app.root.update()
app.root.update_idletasks()
total = float(str(canvas.cget('scrollregion')).split()[3])
visible = canvas.winfo_height()
print(f'  内容 {total:.0f}px / 可视 {visible}px / 可滚 {total - visible:.0f}px')


def wheel(delta, widget=None):
    app._on_mousewheel(type('E', (), {'delta': delta,
                                      'widget': widget or canvas})())
    app.root.update_idletasks()


def moved(delta, start=0.0):
    canvas.yview_moveto(start)
    app.root.update_idletasks()
    before = canvas.yview()[0]
    wheel(delta)
    return (canvas.yview()[0] - before) * total


if total <= visible:
    check('内容未超出可视区，跳过滚轮用例', True, f'{total:.0f}px / {visible}px')
else:
    check('标准一格滚 60px', abs(moved(-120) - 60) < 2, f'{moved(-120):.1f}px')

    # 修复前 int(-40/120) == 0，小 delta 会被整除成 0 直接丢掉
    check('小 delta 不被丢弃（精密触控板）', moved(-40) > 0, f'{moved(-40):.1f}px')

    canvas.yview_moveto(0.0)
    app.root.update_idletasks()
    before = canvas.yview()[0]
    for _ in range(6):
        wheel(-20)
    acc = (canvas.yview()[0] - before) * total
    check('6 次 delta=-20 累积成一整格', abs(acc - 60) < 2, f'{acc:.1f}px')

    canvas.yview_moveto(0.0)
    app.root.update_idletasks()
    for _ in range(30):
        wheel(-120)
    bottom_ok = canvas.yview()[1] <= 1.0001
    for _ in range(30):
        wheel(120)
    check('反复滚到两端不越界',
          bottom_ok and canvas.yview()[0] >= -0.0001,
          f'底 {canvas.yview()[1]:.4f} / 顶 {canvas.yview()[0]:.4f}')

    canvas.yview_moveto(0.05)
    app.root.update_idletasks()
    before = canvas.yview()[0]
    wheel(-120, app.log_text)
    check('日志框上的滚轮不被主画布抢走',
          abs(canvas.yview()[0] - before) < 1e-9)

# 修复前是在画布的 <Enter>/<Leave> 里 bind_all/unbind_all，而指针移进画布内的
# 子控件时 Tk 也会发 <Leave>，绑定会被解除，滚轮时灵时不灵。
canvas.event_generate('<Leave>')
app.root.update()
check('鼠标移出画布后滚轮绑定仍在', '<MouseWheel>' in app.root.bind_all())

app.root.destroy()
shutil.rmtree(work, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
