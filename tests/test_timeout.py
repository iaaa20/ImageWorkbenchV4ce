"""单张超时：超过就放弃处理、改名直接复制，整批不许被一张拖住。

跑着的任务没法单独取消，所以实现上是「杀掉整个进程池 → 超时那张降级成复制 →
被牵连的在途任务原样重排 → 重建池子接着跑」。这套流程的坑在于：
  - 重排的任务不能重复计数，也不能丢
  - 降级后的那张必须真的是**原样复制**（逐字节一致），而不是又去解码
  - 连复制都超时的，要老老实实记成失败，不能无限降级打转

用法:  python tests\test_timeout.py
会真的起子进程，需要有桌面会话。
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


def sticky_worker(args):
    """模块级函数，spawn 出来的子进程能重新 import 到它。

    名字里带 stuck 的那张假装卡死 —— 这正是用户遇到的形态：文件本身不大，
    可就是几分钟出不来结果。**降级成复制之后就不该再卡**（copy_only=True），
    所以这里只在需要解码的那次睡。
    """
    src = args[0]
    copy_only = args[4] if len(args) > 4 else False
    if 'stuck' in os.path.basename(src) and not copy_only:
        time.sleep(60)
    return real_worker(args)


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, '素材')
    os.makedirs(src)

    # 必须超过 PROCESS_THRESHOLD 才走多进程 —— 超时功能只在多进程下有效
    N = 240
    for i in range(N):
        Image.new('RGB', (64, 48), (i % 256, 80, 160)).save(
            os.path.join(src, f'p{i:04d}.jpg'))
    stuck = os.path.join(src, 'stuck_one.jpg')
    Image.new('RGB', (64, 48), (10, 200, 90)).save(stuck)
    stuck_bytes = open(stuck, 'rb').read()
    total = N + 1

    app_mod.process_single_image = sticky_worker
    # 把档位换成秒级，不然一套测试要跑两分钟
    app_mod.TIMEOUT_CHOICES = [('测试 3 秒', 3)]

    app = ImageProcessorApp()
    app.root.withdraw()
    app.src_folder.set(src)
    app.out_mode.set('new')
    out_root = os.path.join(work, 'out')
    app.new_folder_path.set(out_root)
    app.compress_only.set(True)
    app.safe_write.set(False)
    app.name_mode.set('keep')          # 保留原名，好对得上是哪一张
    app.timeout_val.set('测试 3 秒')

    started = time.time()
    app.start_processing()

    deadline = time.time() + 180

    def poll():
        if not app.is_running or time.time() > deadline:
            app.root.after(400, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()
    elapsed = time.time() - started

    log = app.log_text.get('1.0', 'end')
    lines = [ln for ln in log.splitlines() if ln.strip()]
    for ln in lines:
        if '超时' in ln or '⏱' in ln or '重排' in ln or '完成' in ln:
            print('   ' + ln)

    check('整批跑完了，没有被一张拖死',
          not app.is_running and app.processed_count == total,
          f'{app.processed_count}/{total}，耗时 {elapsed:.0f} 秒')
    # 卡死那张要睡 60 秒；3 秒超时后必须马上放弃，全程远小于 60 秒
    check('没有傻等那张卡死的图', elapsed < 45, f'{elapsed:.0f} 秒')

    check('日志说明了哪张超时、怎么处理的',
          any('放弃处理、改名直接复制' in ln for ln in lines),
          next((ln for ln in lines if '放弃处理' in ln), '没有这一行'))
    check('日志报了池子重建', any('重排' in ln for ln in lines),
          next((ln for ln in lines if '重排' in ln), '没有这一行'))

    out_dir = os.path.join(out_root, '素材_已处理')
    produced = set(os.listdir(out_dir)) if os.path.isdir(out_dir) else set()
    check('输出一个不少', len(produced) == total, f'{len(produced)}/{total}')

    got = os.path.join(out_dir, 'stuck_one.jpg')
    check('超时那张确实被带出来了', os.path.exists(got))
    if os.path.exists(got):
        # 关键：是**原样复制**，不是又解码重编码了一遍
        check('超时那张与源逐字节一致（真的只是复制）',
              open(got, 'rb').read() == stuck_bytes,
              f'{os.path.getsize(got)} vs 源 {len(stuck_bytes)} 字节')

    check('没有任务被重复计数', app.processed_count == total,
          f'processed={app.processed_count} total={total}')

    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing

    multiprocessing.freeze_support()
    main()
