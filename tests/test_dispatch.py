"""派发要「挑一张放得下的」，而不是死等队首那张巨图。

用户的原话：「让一张图片处理完之后马上接下一张，保证同一时间的内存及 CPU
利用高」。原来的派发是**严格按队列顺序**的 ——

    if in_flight and in_flight_mb + next_mb > budget_mb:
        break          # 整批停摆

队首是张巨图、额度又不够时，后面明明还排着一堆小图能立刻开工，工人却全闲着。
预扫描已经把每张图的预估内存都算好了，挑一张是 O(前瞻长度) 的事，几乎不要钱。
输出文件名在 `build_tasks` 阶段就定死了，跟派发顺序无关，重排不影响编号。

**这个文件同时钉住一个差点发出去的 bug：** 改成候选缓冲之后，派发循环的条件
写成了 `while not exhausted and ...` —— `pending` 一取空就不再派发，而缓冲里
还压着几十张，**那些图会被静默丢掉**。条件必须是 `not exhausted or ready`。

用法:  python tests\test_dispatch.py
需要桌面会话，约半分钟。
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
app_mod.messagebox.askokcancel = lambda *a, **k: True

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def noisy(w, h):
    im = Image.new('RGB', (w, h))
    px = im.load()
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            px[x, y] = ((x * 7) % 256, (y * 13) % 256, (x ^ y) % 256)
    return im


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, 'src')
    os.makedirs(src)

    # 每 10 张里有 1 张巨图。巨图单张就吃掉大半额度，小图很省 ——
    # 严格按顺序派发的话，轮到巨图时整批就得停下来等排空。
    small = noisy(700, 500)
    big = noisy(3600, 2700)
    N_SMALL, N_BIG = 216, 24
    for i in range(N_SMALL):
        small.save(os.path.join(src, f's{i:04d}.jpg'), quality=100,
                   subsampling=0)
    for i in range(N_BIG):
        big.save(os.path.join(src, f'b{i:04d}.jpg'), quality=100,
                 subsampling=0)
    total = N_SMALL + N_BIG
    print(f'{total} 张（{N_BIG} 张巨图 + {N_SMALL} 张小图），内存上限 1 GB\n')

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
    app.max_workers.set(8)
    app.mem_limit_val.set('1 GB')      # 卡紧，逼出「放不下」的局面

    t0 = time.time()
    app.start_processing()

    def poll():
        if not app.is_running or time.time() - t0 > 600:
            app.root.after(400, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()

    out = os.path.join(work, 'out')
    made = sum(len(fs) for _, _, fs in os.walk(out)) if os.path.isdir(out) else 0

    print('一、一张都不能少')
    check('**产出数等于输入数**', made == total, f'{total} -> {made}')
    check('全部处理完', app.processed_count == app.total_count,
          f'{app.processed_count}/{app.total_count}')
    check('没有失败', len(app.error_list) == 0,
          f'{len(app.error_list)} 个'
          + (f'：{app.error_list[0][1][:60]}' if app.error_list else ''))

    print('\n二、真的走了「往后挑」这条路')
    check('至少挑过一次（否则这个测试没测到东西）',
          app._skipped_ahead > 0, f'{app._skipped_ahead} 次')

    print('\n三、挑归挑，闸门不许被突破')
    check('同时在飞的预估内存没超预算',
          app.peak_in_flight_mb <= app._budget_mb * 1.001,
          f'峰值 {app.peak_in_flight_mb:.0f} MB / 预算 {app._budget_mb:.0f} MB')

    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
