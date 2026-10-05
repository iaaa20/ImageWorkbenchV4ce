"""超时必须只算「真正在跑」的时间，不能把排队时间也算进去。

**用户症状：「太大了的图处理不了就直接罢工了，等到 120 秒过了之后再处理」。**
真相是那些图并不需要 120 秒 —— 它们只是在进程池的队列里排了很久，而超时的表
从**提交**时刻就开始走了。

机制：`window = max(workers * 3, workers + 4)`，8 核就是一次派 24 个，但工人
只有 8 个 —— 另外 16 个躺在 `ProcessPoolExecutor` 内部队列里等着。
`submitted_at[fut] = time.time()` 记的是提交时刻，于是排队等待（约 2× 单张耗时）
被算成了处理时间。加上自己跑的 1×，**任何真实耗时超过门槛 1/3 的图都会被误判**。
默认门槛 120 秒 → 超过约 35 秒的图必中。

后果是连锁的：误判 → 拆掉整个进程池重建 → 在途任务全部作废重排 → 队列更长 →
更多误判。而且被误判的图会降级成 `copy_only`，**原样复制、根本没压缩**。

修复前后（220 张、每张恰好 1.0 秒、门槛 2 秒，本机实测）：

| | 误判超时 | 池子重建 | 墙上 |
|---|---|---|---|
| 修复前 | 214 次 | 18 次 | 40.7 秒（理论 28 秒）|
| 修复后 | 0 次 | 0 次 | ~28 秒 |

用法:  python tests\test_timeout_queue.py
会真的起子进程，需要有桌面会话，约半分钟。
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
from imgwb.config import TaskResult

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None
app_mod.messagebox.askokcancel = lambda *a, **k: True

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


EACH = 1.0        # 每张图的真实耗时，写死，绝不超过门槛
LIMIT = 2         # 门槛是单张真实耗时的 2 倍
WORKERS = 8
N = 220           # 必须超过 200，否则走线程模式、超时压根不启用


def fake_worker(args):
    """模块级函数，spawn 的子进程 import 得到。每张图恰好 EACH 秒。

    用假 worker 而不是真图：这样「单张耗时」是**写死**的，
    断言「没有一张超过 EACH 秒」才站得住，测试也不受机器快慢影响。
    """
    src, dst = args[0], args[1]
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    time.sleep(EACH)
    shutil.copy2(src, dst)
    return TaskResult(success=True, src_path=src, dst_path=dst)


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, 'src')
    os.makedirs(src)
    seed = os.path.join(src, 's.jpg')
    Image.new('RGB', (200, 150), (90, 140, 200)).save(seed, quality=90)
    for i in range(N):
        shutil.copy2(seed, os.path.join(src, f'q{i:03d}.jpg'))
    os.remove(seed)

    app_mod.process_single_image = fake_worker
    app_mod.TIMEOUT_CHOICES = [(f'测试 {LIMIT} 秒', LIMIT)]

    app = ImageProcessorApp()
    app.root.withdraw()
    app._ask_merge_choice = lambda *a, **k: 'merge'
    app.src_folder.set(src)
    app.out_mode.set('new')
    app.new_folder_path.set(os.path.join(work, 'out'))
    app.name_mode.set('keep')
    app.compress_only.set(True)
    app.quality_val.set('通用高清 (95%)')
    app.perf_mode.set('custom')
    app.max_workers.set(WORKERS)
    app.timeout_val.set(f'测试 {LIMIT} 秒')

    ideal = N * EACH / WORKERS
    print(f'{N} 张，每张真实耗时写死 {EACH} 秒，并发 {WORKERS}，门槛 {LIMIT} 秒')
    print(f'理论 {ideal:.0f} 秒；单张 {EACH} 秒 < 门槛 {LIMIT} 秒 —— 一张都不该超时\n')

    t0 = time.time()
    app.start_processing()

    def poll():
        if not app.is_running or time.time() - t0 > 300:
            app.root.after(400, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()
    wall = time.time() - t0

    log = app.log_text.get('1.0', 'end')
    timeouts = [l for l in log.splitlines() if '还没好，放弃处理' in l]
    rebuilds = [l for l in log.splitlines() if '重建工作进程池' in l]

    check('**没有误判超时**', len(timeouts) == 0,
          f'{len(timeouts)} 次'
          + (f'，例如 {timeouts[0].strip()[:56]}' if timeouts else ''))
    check('没有拆池子重建', len(rebuilds) == 0, f'{len(rebuilds)} 次')
    check('没有失败', len(app.error_list) == 0,
          f'{len(app.error_list)} 个'
          + (f'：{app.error_list[0][1][:50]}' if app.error_list else ''))
    check('全部处理完', app.processed_count == app.total_count,
          f'{app.processed_count}/{app.total_count}')
    # 墙上时间：**只拦"并发塌掉"这一种，不拦机器忙。**
    #
    # 原来的阈值是 1.35 倍，太紧了 —— 实测机器上同时在打包时，这一条会
    # 冲到 4.17 倍而上面三条全绿（没有误判超时、没有拆池子、没有失败）。
    # 一个会因为别的程序在跑就变红的断言，是在喊狼来了：真出问题的那次
    # 没人会再信它。
    #
    # 那它还有什么独立价值？上面三条直接盯住了"误判超时"和"拆池子重建"，
    # 但盯不住"并发整个塌成串行"（比如工人数被改成 1）。那种情况下墙上时间
    # 会涨到并发数那个量级 —— 8 个工人塌成 1 就是 8 倍。
    # 所以阈值定在 4 倍：机器忙造成的 4.17 倍……还是会误报。定在 5 倍，
    # 既能抓住 8 倍的串行化，又容得下实测到的负载抖动。
    print(f'   ⏱ 墙上 {wall:.1f} 秒 / 理论 {ideal:.0f} 秒 = {wall / ideal:.2f} 倍'
          f'（机器忙时会偏高，只有超过 5 倍才算并发塌了）')
    check('并发没有塌成串行', wall < ideal * 5,
          f'{wall / ideal:.2f} 倍，理论 {ideal:.0f} 秒')

    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
