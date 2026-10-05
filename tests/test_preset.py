"""一键预设与高级设置窗口。

用法:  python tests\test_preset.py
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
from imgwb.presets import PRESETS

# 处理完成会弹「完成」对话框，无人值守时会一直等下去
app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None

fails = []


def pump(app, timeout=120):
    """跑真正的 mainloop 直到处理结束 —— 工作线程的 after() 依赖它在运行。"""
    deadline = time.time() + timeout

    def poll():
        if not app.is_running or time.time() > deadline:
            app.root.after(400, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


work = tempfile.mkdtemp()
src = os.path.join(work, '相册')
os.makedirs(src)
ex = Image.Exif()
ex[0x0112] = 6
for i in range(1, 4):
    Image.new('RGB', (200, 100), (i * 60 % 256, 90, 150)).save(
        os.path.join(src, f'DSC_{i:04d}.jpg'), exif=ex)
f = [Image.new('RGB', (40, 30), (i * 80 % 256, 60, 180)) for i in range(4)]
f[0].save(os.path.join(src, 'anim.webp'), format='WEBP', save_all=True,
          append_images=f[1:], duration=70, loop=0)
snapshot = sorted(os.listdir(src))

app = ImageProcessorApp()
app.root.update()

print('=== 主界面 ===')
check('六个一键按钮都在', len(app.preset_buttons) == 6, str(list(app.preset_buttons)))
app.root.update_idletasks()
check('窗口装得下简易模式的全部内容',
      app.root.winfo_height() >= app._scrollable_frame.winfo_reqheight(),
      f'窗口 {app.root.geometry()}，内容需要 '
      f'{app._scrollable_frame.winfo_reqheight()}px')

print('\n=== 简易 / 高级 两种模式互斥 ===')


def visible():
    """当前露在外面的执行入口有哪些。"""
    entries = []
    if app.simple_frame.winfo_ismapped():
        entries.append('一键按钮')
    if app.btn_run.winfo_ismapped():
        entries.append('开始执行')
    return entries


check('启动是简易模式', app.advanced_mode is False)
check('简易模式显示一键面板', app.simple_frame.winfo_ismapped())
check('简易模式不显示高级面板', not app.advanced_frame.winfo_ismapped())
check('简易模式下只有一键按钮能执行', visible() == ['一键按钮'], str(visible()))

app.toggle_mode()
app.root.update()
check('切到高级模式', app.advanced_mode is True)
check('高级模式收起一键面板', not app.simple_frame.winfo_ismapped())
check('高级模式展开高级面板', app.advanced_frame.winfo_ismapped())
check('高级模式下只有开始执行能执行', visible() == ['开始执行'], str(visible()))
check('高级控件都在主窗口里，不是独立窗口',
      app.ent_new.winfo_toplevel() is app.root
      and app.spin_workers.winfo_toplevel() is app.root)

app.toggle_mode()
app.root.update()
check('切回简易模式', app.advanced_mode is False and visible() == ['一键按钮'],
      str(visible()))
check('来回切换后控件都还在', app.ent_new.winfo_exists()
      and all(b.winfo_exists() for b in app.preset_buttons.values()))
app.root.withdraw()

print('\n=== 每个预设：点了就能用 ===')
for preset in PRESETS:
    for name in os.listdir(work):
        if name != '相册':
            shutil.rmtree(os.path.join(work, name), ignore_errors=True)

    app.dropped_paths = []
    app.src_folder.set(src)
    app.apply_preset(preset['key'])

    pump(app)

    out_dir = os.path.join(work, '相册_已处理')
    produced = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    kept = sorted(os.listdir(src))
    ok = bool(produced) and kept == snapshot and not app.error_list
    check(f'{preset["label"]}', ok,
          f'产出 {len(produced)} 个，源目录未变={kept == snapshot}'
          + (f'，错误 {app.error_list[:1]}' if app.error_list else ''))

print('\n=== 一键编号的结果 ===')
for name in os.listdir(work):
    if name != '相册':
        shutil.rmtree(os.path.join(work, name), ignore_errors=True)
app.src_folder.set(src)
app.apply_preset('number')
pump(app)
produced = sorted(os.listdir(os.path.join(work, '相册_已处理')))
first = str(1).zfill(len(str(len(produced))) + 1)   # 智能位数
check(f'按顺序编号（从 {first} 起）', produced[0].startswith(first), str(produced))
check('动图仍是 4 帧',
      Image.open(os.path.join(work, '相册_已处理', first + '.webp')).n_frames == 4)

print('\n=== 一键转正：Exif 方向被烘焙进像素 ===')
for name in os.listdir(work):
    if name != '相册':
        shutil.rmtree(os.path.join(work, name), ignore_errors=True)
app.src_folder.set(src)
app.apply_preset('upright')
pump(app)
out = os.path.join(work, '相册_已处理', 'DSC_0001.jpg')
with Image.open(out) as im:
    check('像素已摆正且方向标记已清', im.size == (100, 200)
          and im.getexif().get(0x0112) in (None, 1),
          f'{im.size} Orientation={im.getexif().get(0x0112)}')

app.root.destroy()
shutil.rmtree(work, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
