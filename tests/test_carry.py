"""不支持的文件原样带过去、智能位数、高级设置按钮的位置。

用法:  python tests\test_carry.py
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


def pump(app, timeout=120):
    deadline = time.time() + timeout

    def poll():
        if not app.is_running or time.time() > deadline:
            app.root.after(400, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()


work = tempfile.mkdtemp()
src = os.path.join(work, '相册')
os.makedirs(src)

# 3 张普通图 + 1 个真 .gif 动图 + 1 个非图片文件
for i in range(1, 4):
    Image.new('RGB', (60, 40), (i * 60 % 256, 90, 150)).save(
        os.path.join(src, f'DSC_{i:04d}.jpg'))
f = [Image.new('RGB', (40, 30), (i * 80 % 256, 60, 180)) for i in range(3)]
f[0].save(os.path.join(src, 'zz_anim.gif'), format='GIF', save_all=True,
          append_images=f[1:], duration=90, loop=0)
with open(os.path.join(src, 'zz_note.txt'), 'w', encoding='utf-8') as fh:
    fh.write('这不是图片')

gif_path = os.path.join(src, 'zz_anim.gif')
gif_bytes = open(gif_path, 'rb').read()
gif_mtime = os.stat(gif_path).st_mtime
before = sorted(os.listdir(src))

app = ImageProcessorApp()
app.root.update()

print('=== 高级设置按钮的位置 ===')
bar_y = app.btn_mode.winfo_rooty()
panel_y = app.mode_slot.winfo_rooty()
status_y = app.log_text.winfo_rooty()
check('按钮在一键面板上方', bar_y < panel_y, f'按钮 y={bar_y} 面板 y={panel_y}')
check('按钮远高于日志区（不再埋在底部）', bar_y < status_y - 100,
      f'按钮 y={bar_y} 日志 y={status_y}')
check('模式说明文字在', bool(app.lbl_mode.cget('text')), app.lbl_mode.cget('text'))
app.root.withdraw()

print('\n=== 一键编号：不支持的文件一起带过去并参与编号 ===')
app.src_folder.set(src)
app.apply_preset('number')
pump(app)

out = os.path.join(work, '相册_已处理')
produced = sorted(os.listdir(out))
check('一个都没落下', len(produced) == 5, f'{produced}')
check('源目录原样未动', sorted(os.listdir(src)) == before)
check('gif 扩展名保留', any(n.endswith('.gif') for n in produced), str(produced))
check('txt 也带过来了', any(n.endswith('.txt') for n in produced), str(produced))

out_gif = [n for n in produced if n.endswith('.gif')][0]
gp = os.path.join(out, out_gif)
check('gif 内容逐字节相同（没被重编码）', open(gp, 'rb').read() == gif_bytes)
check('gif 修改时间保留（文件属性没变）',
      abs(os.stat(gp).st_mtime - gif_mtime) < 2,
      f'{os.stat(gp).st_mtime:.0f} vs {gif_mtime:.0f}')

print('\n=== 智能位数 ===')
nums = sorted(os.path.splitext(n)[0] for n in produced)
check('5 个文件 -> 2 位编号（4位够用，多留一位）',
      all(len(n) == 2 for n in nums), str(nums))

for name in os.listdir(work):
    if name != '相册':
        shutil.rmtree(os.path.join(work, name), ignore_errors=True)
big = os.path.join(work, '大批')
os.makedirs(big)
for i in range(1, 13):
    Image.new('RGB', (20, 20), (i * 20 % 256, 80, 120)).save(
        os.path.join(big, f'p{i:03d}.jpg'))
app.src_folder.set(big)
app.apply_preset('number')
pump(app)
produced = sorted(os.listdir(os.path.join(work, '大批_已处理')))
nums = [os.path.splitext(n)[0] for n in produced]
check('12 个文件 -> 3 位编号', all(len(n) == 3 for n in nums), str(nums[:4]))
check('从 001 开始到 012', nums[0] == '001' and nums[-1] == '012',
      f'{nums[0]}…{nums[-1]}')

print('\n=== 高级设置里能关掉「带上不支持的文件」 ===')
app.set_mode(advanced=True)
app.root.update()
check('新开关在高级面板里', app.chk_others.winfo_toplevel() is app.root)
check('位数下拉里有「自动」',
      any('自动' in v for v in app.cb_digit.cget('values')),
      str(app.cb_digit.cget('values')))

for name in os.listdir(work):
    if name not in ('相册', '大批'):
        shutil.rmtree(os.path.join(work, name), ignore_errors=True)
app.copy_others.set(False)
app.digit_val.set('3位 (001)')
app.src_folder.set(src)
app.out_mode.set('new')
app.new_folder_path.set(work)
app.name_mode.set('number')
app.rotate_val.set('不旋转')
app.compress_only.set(False)
app.is_ico.set(False)
app.start_processing()
pump(app)
produced = sorted(os.listdir(os.path.join(work, '相册_已处理')))
# GIF 现在是**受支持的输入格式**（`VALID_EXTENSIONS` 里有它），所以关掉
# 「带上不支持的文件」之后留下的是 3 张 jpg + 1 个 gif，只有 .txt 被排除。
# 用户报过这个：勾了「动图也重编码」拖进一个 GIF，界面却说「格式不支持，已忽略」
# —— 因为动图写回那套一直是齐的，只是 .gif 没进输入白名单。
check('关掉后只留图片（GIF 算图片）', len(produced) == 4, str(produced))
check('gif 没被当成"不支持"排除掉',
      any(n.endswith('.gif') for n in produced), str(produced))
check('只有 txt 被排除', not any(n.endswith('.txt') for n in produced),
      str(produced))
check('位数回到手动 3 位', all(len(os.path.splitext(n)[0]) == 3 for n in produced),
      str(produced))

app.root.destroy()
shutil.rmtree(work, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
