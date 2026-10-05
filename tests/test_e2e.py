"""端到端：跑真实的 process_worker，验证用户报的两个现象都已修复。"""
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

from PIL import Image, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from imgwb import app as app_mod

mod = app_mod          # 兼容下面用 mod.ImageProcessorApp / mod.messagebox 的写法

# 每轮处理结束都会弹「完成」对话框，无人值守时它会一直等下去 —— mainloop 被
# 模态框卡住，整套测试要熬到超时才结束。测试里一律屏蔽。
app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None

fails = []


def check(name, ok, detail=''):
    print(f'  {"✅" if ok else "❌"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def pump(app, timeout=120):
    """跑真正的 mainloop 直到处理结束 —— 工作线程的 after() 依赖它在运行。"""
    deadline = time.time() + timeout

    def poll():
        if not app.is_running or time.time() > deadline:
            app.root.after(400, app.root.quit)   # 让剩余回调排完再退出
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()
    return not app.is_running


def run_once(app, timeout=120):
    app.start_processing()
    return pump(app, timeout)


work = tempfile.mkdtemp()
N = 12
for i in range(1, N + 1):
    ex = Image.Exif()
    ex[0x0112] = 6               # 手机竖拍常见的方向标记
    Image.new('RGB', (200, 100), (i * 15 % 256, 90, 160)).save(
        os.path.join(work, f'DSC_{i:04d}.jpg'), exif=ex)

app = mod.ImageProcessorApp()
app.root.withdraw()
app.src_folder.set(work)
app.out_mode.set('original')     # 原地修改
app.name_mode.set('number')      # 纯数字编号
app.start_num.set(1)
app.compress_only.set(True)      # 仅压缩（会走 Pillow 重存，触发 Exif 那条）
app.rotate_val.set('不旋转')
app.safe_write.set(True)

print(f'=== 第一次运行（{N} 张 DSC_xxxx.jpg，原地 + 纯数字编号 + 仅压缩）===')
ok = run_once(app)
first = sorted(os.listdir(work))
check('跑完', ok)
check('编号从 001 开始', first[0] == '001.jpg', f'{first[:3]} … 共 {len(first)} 个')
check('文件数不变', len(first) == N, f'{len(first)} 个')

print('\n=== 第二次运行同一流程（这就是你报的 bug）===')
ok = run_once(app)
second = sorted(os.listdir(work))
check('跑完', ok)
check('第一张仍是 001.jpg（不是 1001.jpg）', second[0] == '001.jpg',
      f'{second[:3]} … 共 {len(second)} 个')
check('文件名与第一次完全一致', second == first, f'{second[:3]}')

print('\n=== 第三次：验证照片没被转歪（Exif 双重旋转）===')
ok = run_once(app)
third = sorted(os.listdir(work))
check('文件名依旧稳定', third == first)
sample = os.path.join(work, third[0])
with Image.open(sample) as im:
    tag, stored = im.getexif().get(0x0112), im.size
    shown = ImageOps.exif_transpose(im).size
check('反复处理后方向仍然正确', shown == (100, 200),
      f'存储{stored} Orientation={tag} 显示{shown}（源图应显示 100x200）')

print('\n=== 第四次：中途取消，检查是否完整撤回 ===')
before = {n: os.path.getsize(os.path.join(work, n)) for n in sorted(os.listdir(work))}
app.prefix_val.set('X')
app.name_mode.set('prefix')      # 换个命名方式，确保会产生改动
app.start_processing()
app.root.after(30, lambda: setattr(app, 'cancel_requested', True))
pump(app)
after = {n: os.path.getsize(os.path.join(work, n)) for n in sorted(os.listdir(work))}
check('取消后目录完全没变', after == before,
      f'取消前 {len(before)} 个 / 取消后 {len(after)} 个')
leftover = [n for n in os.listdir(work) if '.~' in n]
check('没有残留临时文件', not leftover, str(leftover))

app.root.destroy()
shutil.rmtree(work, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
