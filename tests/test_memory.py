"""内存闸门：几张巨图不能同时被解码。

用法:  python tests\test_memory.py
会真的起子进程，需要桌面会话。
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
from imgwb import perf
from imgwb.app import ImageProcessorApp
from imgwb.pipeline import estimate_peak_mb

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None
app_mod.messagebox.askokcancel = lambda *a, **k: True

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def rss_mb():
    """本进程 + 所有子进程的工作集合计，MB。"""
    import ctypes

    class PMC(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_uint32), ('PageFaultCount', ctypes.c_uint32),
                    ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t),
                    ('PeakPagefileUsage', ctypes.c_size_t)]

    psapi = ctypes.WinDLL('psapi')
    k32 = ctypes.WinDLL('kernel32')
    total = 0
    pids = [os.getpid()]
    ex = app.executor if 'app' in globals() else None
    pids += [p.pid for p in (getattr(ex, '_processes', None) or {}).values()]
    for pid in pids:
        # GetProcessMemoryInfo 要 PROCESS_QUERY_INFORMATION，
        # 用 QUERY_LIMITED 会静默失败，子进程就统计不到
        h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not h:
            continue
        counters = PMC()
        counters.cb = ctypes.sizeof(PMC)
        if psapi.GetProcessMemoryInfo(h, ctypes.byref(counters), counters.cb):
            total += counters.WorkingSetSize
        k32.CloseHandle(h)
    return total / 1048576


def main():
    global app

    print('=== 估算函数：只读文件头，不解码 ===')
    d0 = tempfile.mkdtemp()
    small = os.path.join(d0, 'small.jpg')
    huge = os.path.join(d0, 'huge.jpg')
    Image.new('RGB', (400, 300), (10, 20, 30)).save(small)
    Image.new('RGB', (5000, 4000), (10, 20, 30)).save(huge)

    t0 = time.time()
    est_small = estimate_peak_mb(small)
    est_huge = estimate_peak_mb(huge)
    elapsed = time.time() - t0
    check('小图估算很小', est_small < 5, f'{est_small:.1f} MB')
    check('大图估算够大', est_huge > 150, f'{est_huge:.0f} MB (5000x4000)')
    check('两次估算都很快（没解码像素）', elapsed < 0.5, f'{elapsed * 1000:.0f} ms')
    check('坏文件不报错只返回 0', estimate_peak_mb(os.path.join(d0, '没有这个')) == 0)
    shutil.rmtree(d0, ignore_errors=True)

    print('\n=== 各模式的内存预算 ===')
    for mode in (perf.ECO, perf.BALANCED, perf.TURBO):
        mb = perf.memory_budget_mb(mode)
        ratio, cap = perf.MEMORY_BUDGET[mode]
        check(f'{perf.MODES[mode]["label"]} 预算合理',
              256 <= mb <= cap, f'{mb:.0f} MB（上限 {cap}）')
    check('节能预算 < 高性能预算',
          perf.memory_budget_mb(perf.ECO) < perf.memory_budget_mb(perf.TURBO))

    print('\n=== 一堆巨图：闸门必须真的被触发 ===')
    # 关键：要选一个「不限流就会爆」的尺寸。8000x8000 单张估算 ~610 MB，
    # 节能模式预算才 768 MB —— 闸门只会放行 1 张，而并发是 4，差距足够明显。
    # 用 4000x3000 是测不出来的：8 张并发也才 916 MB，压根撞不到闸门。
    app_mod.PROCESS_THRESHOLD = 5      # 少量文件也走多进程，省得造几百张巨图

    work = tempfile.mkdtemp()
    src = os.path.join(work, '巨图')
    os.makedirs(src)
    big = Image.new('RGB', (8000, 8000))
    px = big.load()
    for y in range(0, 8000, 11):
        for x in range(0, 8000, 11):
            px[x, y] = (x % 256, y % 256, 90)
    N = 12
    for i in range(N):
        big.save(os.path.join(src, f'b{i:04d}.jpg'), quality=85)
    del big, px
    print(f'  准备了 {N} 张 8000x8000')

    app = ImageProcessorApp()
    app.root.withdraw()
    app.src_folder.set(src)
    app.perf_mode.set(perf.ECO)
    app.apply_preset('compress')

    peak = 0
    samples = []
    deadline = time.time() + 300

    def sample():
        nonlocal peak
        cur = rss_mb()
        peak = max(peak, cur)
        samples.append(cur)
        if not app.is_running or time.time() > deadline:
            app.root.after(200, app.root.quit)
            return
        app.root.after(150, sample)

    app.root.after(150, sample)
    app.root.mainloop()

    budget = perf.memory_budget_mb(perf.ECO)
    workers = perf.worker_count(perf.ECO)
    one = estimate_peak_mb(os.path.join(src, 'b0000.jpg'))
    unbounded = one * workers
    print(f'  单张估算 {one:.0f} MB × {workers} 并发 = {unbounded:.0f} MB（没有闸门时的量级）')
    print(f'  闸门 {budget:.0f} MB，实测峰值 {peak:.0f} MB（{len(samples)} 次采样）')

    check('处理正常完成', not app.is_running and app.processed_count == N,
          f'{app.processed_count}/{N}')
    # 峰值要包含进程本身的基础开销，给足余量；关键是别到「无闸门」那个量级
    check('闸门确实会被触发（不然这条用例没意义）', unbounded > budget,
          f'不限流 {unbounded:.0f} MB > 闸门 {budget:.0f} MB')

    # 直接验闸门自己的账：同时在解码的图片总量有没有超预算。
    # 这比读 Windows 的进程内存可靠 —— 后者受权限、时机、采样间隔影响太大。
    gated = app.peak_in_flight_mb
    print(f'  闸门记录的同时在解码总量峰值 {gated:.0f} MB')
    check('同时在解码的总量没超预算（单张超预算时允许放行一张）',
          gated <= max(budget, one) + 1,
          f'{gated:.0f} MB vs 预算 {budget:.0f} MB')
    check('并发确实被压下来了（不是 4 张一起上）', gated < unbounded * 0.75,
          f'{gated:.0f} MB，不限流会是 {unbounded:.0f} MB')
    check('日志写明了内存上限', '内存上限' in app.log_text.get('1.0', 'end'))

    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing

    multiprocessing.freeze_support()
    main()
