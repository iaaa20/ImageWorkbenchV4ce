"""窗口尺寸：启动定一次，切模式绝不改；内容装不下交给滚动条。

用法:  python tests\test_window.py
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


def size(app):
    app.root.update_idletasks()
    return app.root.winfo_width(), app.root.winfo_height()


def need(app):
    app.root.update_idletasks()
    return app._scrollable_frame.winfo_reqheight()


app = ImageProcessorApp()
app.root.update()

print('=== 启动尺寸：按两种模式里较大的那个定，且不越出屏幕 ===')
w, h = size(app)
sw, sh = app.root.winfo_screenwidth(), app.root.winfo_screenheight()
x, y = app.root.winfo_x(), app.root.winfo_y()
check('窗口整个在屏幕里', x + w <= sw and y + h <= sh,
      f'窗口 {w}x{h}+{x}+{y}，屏幕 {sw}x{sh}')
check('简易模式内容不用滚就能看全', h >= need(app) - 2,
      f'窗口高 {h}，内容需要 {need(app)}px')

print('\n=== 切模式绝不改变窗口尺寸（用户明确要求）===')
simple_size = size(app)
app.toggle_mode()
app.root.update()
adv_size = size(app)
check('切到高级尺寸不变', adv_size == simple_size, f'{simple_size} -> {adv_size}')

adv_content = need(app)
limit = sh - 80
check('高级模式内容要么装得下，要么可以滚',
      adv_content <= adv_size[1] + 2 or float(
          str(app._canvas.cget('scrollregion')).split()[3]) >= adv_content - 2,
      f'内容 {adv_content}px，窗口 {adv_size[1]}px，'
      f'滚动区 {app._canvas.cget("scrollregion")}')

app.toggle_mode()
app.root.update()
check('切回简易尺寸仍不变', size(app) == simple_size,
      f'{adv_size} -> {size(app)}')

print('\n=== 反复切换不会让内容越涨越大 ===')
# 曾经踩过：内容被拉伸到画布高度之后，再拿它的 reqheight 当"自然高度"去算下一次
# 拉伸，一轮比一轮大，几次之后凭空多出几百像素的滚动条。
heights = []
for _ in range(4):
    app.toggle_mode()
    app.root.update()
    heights.append(app._scrollable_frame.winfo_height())
check('内层框高度稳定', len(set(heights)) <= 2, str(heights))
if app.advanced_mode:
    app.toggle_mode()
    app.root.update()

print('\n=== 用户手动调过之后，切模式不再重置尺寸 ===')
app.root.geometry('900x700')
app.root.update()
app.root.update_idletasks()
check('已识别为用户手动调整', app._user_resized is True)

manual = size(app)
app.toggle_mode()
app.root.update()
after_toggle = size(app)
check('切到高级模式尺寸不变', after_toggle == manual,
      f'{manual} -> {after_toggle}')

app.toggle_mode()
app.root.update()
check('切回简易模式尺寸仍不变', size(app) == manual,
      f'{manual} -> {size(app)}')

print('\n=== 自动位数的预览不再是问号 ===')
app.root.withdraw()
app.name_mode.set('number')
app.digit_val.set('自动 (按数量)')
app.update_preview()
text = app.lbl_preview.cget('text')
check('预览有内容且不是 ???', '???' not in text and '0001' in text, text)
# 规则说明放在灰字提示里 —— 预览标签是 Consolas，混中文会挤成一团
check('规则写在提示行里', '自动' in app.lbl_digit_hint.cget('text'),
      app.lbl_digit_hint.cget('text'))

app.digit_val.set('3位 (001)')
app.update_preview()
check('固定位数预览照常', '001' in app.lbl_preview.cget('text'),
      app.lbl_preview.cget('text'))

app.root.destroy()
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
