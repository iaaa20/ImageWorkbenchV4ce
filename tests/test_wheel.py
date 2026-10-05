"""滚轮行为：翻页正常，但绝不能顺手改掉任何设置。

用法:  python tests\test_wheel.py
会真的建出窗口，需要有桌面会话。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from imgwb.app import ImageProcessorApp

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


app = ImageProcessorApp()
app.set_mode(advanced=True)          # 下拉框都在高级面板里
app.root.geometry('720x420')         # 压小，制造出真实的可滚范围
app.root.update()
app.root.update_idletasks()

canvas = app._canvas
total = float(str(canvas.cget('scrollregion')).split()[3])
visible = canvas.winfo_height()
print(f'内容 {total:.0f}px / 可视 {visible}px / 可滚 {total - visible:.0f}px')

# 界面上所有会影响处理结果的设置
SETTINGS = ('src_folder', 'scan_subdirs', 'out_mode', 'new_folder_path',
            'keep_structure', 'dir_suffix_val', 'name_mode', 'prefix_val',
            'start_num', 'digit_val', 'rotate_val', 'quality_val',
            'compress_only', 'is_ico', 'ico_mode_val', 'ico_format_val',
            'max_workers', 'safe_write', 'copy_others')


def snapshot():
    return {n: getattr(app, n).get() for n in SETTINGS}


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


print('\n=== 在每一个控件上滚一下，看有没有设置被改掉 ===')
widgets = list(walk(app.root))
print(f'  遍历 {len(widgets)} 个控件')

before = snapshot()
touched = []
for w in widgets:
    try:
        w.event_generate('<MouseWheel>', delta=-120, x=5, y=5)
    except Exception:
        continue
app.root.update()
after = snapshot()

for name in SETTINGS:
    if before[name] != after[name]:
        touched.append(f'{name}: {before[name]!r} -> {after[name]!r}')

check('滚轮没有改掉任何设置', not touched, '；'.join(touched) or '全部保持原值')

print('\n=== 下拉框/微调框上的滚轮仍然能翻页 ===')
for label, widget in (('画质下拉', app.cb_quality),
                      ('旋转下拉', app.cb_rotate),
                      ('位数下拉', app.cb_digit),
                      ('并发数微调', app.spin_workers)):
    canvas.yview_moveto(0.2)
    app.root.update_idletasks()
    top = canvas.yview()[0]
    widget.event_generate('<MouseWheel>', delta=-120, x=5, y=5)
    app.root.update_idletasks()
    moved = (canvas.yview()[0] - top) * total
    check(f'{label}上滚一格翻页 60px', abs(moved - 60) < 2, f'{moved:.1f}px')

print('\n=== 页面空白处照常翻页 ===')
canvas.yview_moveto(0.2)
app.root.update_idletasks()
top = canvas.yview()[0]
app._on_mousewheel(type('E', (), {'delta': -120, 'widget': canvas})())
app.root.update_idletasks()
moved = (canvas.yview()[0] - top) * total
check('空白处滚一格 60px', abs(moved - 60) < 2, f'{moved:.1f}px')

print('\n=== 日志框仍然滚自己，不带动页面 ===')
canvas.yview_moveto(0.2)
app.root.update_idletasks()
top = canvas.yview()[0]
app._on_mousewheel(type('E', (), {'delta': -120, 'widget': app.log_text})())
app.root.update_idletasks()
check('日志框上不翻页', abs(canvas.yview()[0] - top) < 1e-9)

app.root.destroy()
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
