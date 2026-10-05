"""超时触发重建进程池时，绝不能把已完成的成果和源文件一起弄丢。

**这是用户真实日志复现出来的最严重一条。** 原地修改 + 安全写入，2831 张跑到
90% 时有一张卡住：

    [19:45:57] ⏱ IMG_065.jpg 超过 120 秒还没好，放弃处理、改名直接复制
    [19:45:58] 已重建工作进程池，5 个任务重排
    [19:46:22] ⚠️ 2560 个失败

链条是：
  1. 安全写入下，2560 张已完成的产物以 `.~xxxxxxxx` 临时文件躺在目录里等提交
  2. 超时分支重建池子前调 `sweep_temps(输出目录)` **无差别清扫**，把它们全删了
  3. 收尾时 `commit_staged` 在原地模式下**先删源文件、再改名**
  4. 源文件删了、临时文件又不在 —— 2560 张图永久消失，界面只报「失败」

同一条链在取消时也留了痕迹：第 1 轮处理了 2580 张，取消时却只
「↩️ 已撤回 19 个改动」—— 其余 2561 个早被那次清扫扫掉了。

用法:  python tests\\test_timeout_loss.py
会真的起子进程，需要有桌面会话，约一分钟。
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
from imgwb.pipeline import process_single_image as real_worker

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None
app_mod.messagebox.askokcancel = lambda *a, **k: True

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


STUCK = 'p0210.jpg'          # 这一张假装卡住，触发超时


def sticky(args):
    """模块级函数，spawn 的子进程能重新 import 到。

    只让一张图卡住 —— 复刻用户日志里「1 张超过 120 秒」的形态。
    降级成复制之后（copy_only）就不再卡，否则会无限打转。
    """
    src = args[0]
    copy_only = args[4] if len(args) > 4 else False
    if os.path.basename(src) == STUCK and not copy_only:
        time.sleep(30)
    return real_worker(args)


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, '原地')
    os.makedirs(src)

    N = 260                    # 超过 200 才走多进程，跟真实用法一致
    base = Image.new('RGB', (900, 700))
    px = base.load()
    for y in range(0, 700, 3):
        for x in range(0, 900, 2):
            px[x, y] = ((x * 3) % 256, (y * 7) % 256, (x ^ y) % 256)
    seed = os.path.join(src, 'seed.jpg')
    base.save(seed, quality=98, subsampling=0)   # 高画质，保证真的重编码
    for i in range(N):
        shutil.copy2(seed, os.path.join(src, f'p{i:04d}.jpg'))
    os.remove(seed)
    start = sorted(os.listdir(src))
    print(f'起始 {len(start)} 个文件（原地修改 + 前缀编号 + 安全写入）')
    print(f'其中 {STUCK} 会卡住 30 秒，超时门槛设为 3 秒\n')

    app_mod.process_single_image = sticky
    app_mod.TIMEOUT_CHOICES = [('测试 3 秒', 3)]

    app = ImageProcessorApp()
    app.root.withdraw()
    app._ask_merge_choice = lambda *a, **k: 'merge'
    app.src_folder.set(src)
    app.out_mode.set('original')          # 原地修改 —— 提交会删源文件
    app.name_mode.set('prefix')
    app.compress_only.set(True)
    app.quality_val.set('通用高清 (95%)')
    app.safe_write.set(True)              # 安全写入 —— 产物先躺在临时文件里
    app.timeout_val.set('测试 3 秒')

    t0 = time.time()
    app.start_processing()

    def poll():
        if not app.is_running or time.time() - t0 > 300:
            app.root.after(400, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()

    log = app.log_text.get('1.0', 'end')
    end = sorted(n for n in os.listdir(src)
                 if os.path.isfile(os.path.join(src, n)))

    check('超时确实触发了、也重建了池子',
          '超过' in log and '重建工作进程池' in log,
          next((l.strip() for l in log.splitlines() if '重建' in l), '没触发'))
    check('**文件一张都没丢**', len(end) == len(start),
          f'{len(start)} -> {len(end)}')
    check('没有失败', len(app.error_list) == 0,
          f'{len(app.error_list)} 个'
          + (f'：{app.error_list[0][1][:70]}' if app.error_list else ''))
    check('全部处理完', app.processed_count == app.total_count,
          f'{app.processed_count}/{app.total_count}')
    check('都改名成 IMG_ 前缀了',
          all(n.startswith('IMG_') for n in end),
          str([n for n in end if not n.startswith('IMG_')][:4]))
    check('没有残留临时文件', not any('.~' in n for n in end),
          str([n for n in end if '.~' in n][:3]))

    app_mod.process_single_image = real_worker
    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
