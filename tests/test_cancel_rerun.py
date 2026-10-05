"""取消之后马上再跑一轮 —— 上一轮的延迟清扫绝不能碰新一轮的文件。

用户报的最严重一条：**「多次处理中普通取消会损失大量图片而无提示」**，
配置是「原地修改 + 前缀编号 + 仅压缩 + 安全写入」。

机制：`request_cancel` 在安全写入模式下会安排一次**延迟**清扫
（`sweep_when_idle`，10 秒 deadline）。原来的实现到点就扫、不管还在不在跑 ——
用户取消完马上又开一轮，十秒后这次清扫落在**新一轮**头上，把它已经完成、
正躺在目录里等提交的临时文件全删光。而原地修改的提交是"先删源文件、再改名"，
于是源图和产物一起没，还一句提示都没有。

**这个文件所有代码都在 main() 里，而且有 `if __name__` 保护 —— 不能省。**
第一版写成模块级代码，进程池 spawn 出来的子进程把整个模块重新 import 了一遍：
又建 Tk 窗口、又开新一轮，然后崩掉，表现成一堆「A process in the process pool
was terminated abruptly」。看起来像产品 bug，其实是测试自己踩了
「调用方必须有 `if __name__` 保护」那个坑，白查了两轮。

用法:  python tests\\test_cancel_rerun.py
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

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None
app_mod.messagebox.askokcancel = lambda *a, **k: True

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, '原地')
    os.makedirs(src)

    N = 260          # 超过 200 才走多进程，跟真实用法一致
    base = Image.new('RGB', (1000, 750))
    px = base.load()
    for y in range(0, 750, 3):
        for x in range(0, 1000, 2):
            px[x, y] = ((x * 3) % 256, (y * 7) % 256, (x ^ y) % 256)
    seed = os.path.join(src, 'seed.jpg')
    base.save(seed, quality=97, subsampling=0)      # 高画质，保证真的重编码
    for i in range(N):
        shutil.copy2(seed, os.path.join(src, f'p{i:04d}.jpg'))
    os.remove(seed)

    def files_now():
        return sorted(n for n in os.listdir(src)
                      if os.path.isfile(os.path.join(src, n)))

    start_files = files_now()
    print(f'起始 {len(start_files)} 个文件（原地修改 + 前缀编号 + 安全写入）')

    app = ImageProcessorApp()
    app.root.withdraw()
    app._ask_merge_choice = lambda *a, **k: 'merge'
    app.src_folder.set(src)
    app.out_mode.set('original')
    app.name_mode.set('prefix')
    app.compress_only.set(True)
    app.quality_val.set('通用高清 (95%)')
    app.safe_write.set(True)
    app.perf_mode.set('balanced')

    def wait_idle(limit=60):
        """等工作线程真正退出再开下一轮。没等的话 start_processing 会被
        is_running 挡掉，下一轮根本没跑，测试却以为跑过了。"""
        end = time.time() + limit
        while app.is_running and time.time() < end:
            app.root.update()
            time.sleep(0.05)

    def run_round(cancel_at=None, limit=300):
        t0 = time.time()
        app.start_processing()
        state = {'c': False}

        def maybe():
            if cancel_at and not state['c'] and app.processed_count >= cancel_at:
                state['c'] = True
                app.request_cancel()
            elif app.is_running and time.time() - t0 < limit:
                app.root.after(20, maybe)

        def poll():
            if not app.is_running or time.time() - t0 > limit:
                app.root.after(250, app.root.quit)
                return
            app.root.after(50, poll)

        app.root.after(20, maybe)
        app.root.after(50, poll)
        app.root.mainloop()
        return time.time() - t0

    print('\n=== 第 1 轮：跑到一半点「取消」 ===')
    run_round(cancel_at=20)
    after_cancel = files_now()
    check('取消后文件一张不少', len(after_cancel) == len(start_files),
          f'{len(start_files)} -> {len(after_cancel)}')
    check('取消后没有残留临时文件',
          not any('.~' in n for n in after_cancel),
          str([n for n in after_cancel if '.~' in n][:3]))

    print('\n=== 第 2 轮：紧接着再跑一轮，跑满 ===')
    wait_idle()
    elapsed = run_round()
    after_run = files_now()

    # 轮次守卫直接验，比"必须跑够 10 秒"那种靠时间的断言可靠得多：
    # 拿**上一轮**的 run_id 去触发延迟清扫，它必须认出自己过期、什么都不做。
    # 名字必须和真正的临时文件一个格式（带 imgwb 标记）—— 清扫只认这个格式，
    # 正是为了不误删用户自己叫 photo.~1a2b3c4d.jpg 的文件。
    # 用 make_temp_path 造名字：临时文件名里带着本实例的标记，清扫只认自己的
    # （别的实例的新鲜临时文件不碰，见 tests/test_tempname.py 第四节）。
    from imgwb.utils import make_temp_path
    probe = make_temp_path(os.path.join(src, 'IMG_probe.jpg'))
    with open(probe, 'wb') as fh:
        fh.write(b'pretend pending temp')
    app._output_dirs = {src}
    app.sweep_when_idle(deadline=0, run_id=app._run_id - 1)   # 过期的一轮
    check('过期轮次的清扫不碰任何文件', os.path.exists(probe),
          '探针文件被误删了')
    app.sweep_when_idle(deadline=0, run_id=app._run_id)       # 当前轮次才该扫
    check('当前轮次的清扫照常执行', not os.path.exists(probe))
    if os.path.exists(probe):
        os.remove(probe)

    check(f'第 2 轮跑满（{elapsed:.1f} 秒）',
          app.processed_count == app.total_count,
          f'{app.processed_count}/{app.total_count}')
    check('第 2 轮全部成功、零失败', len(app.error_list) == 0,
          f'{len(app.error_list)} 个失败'
          + (f'：{app.error_list[0][1][:60]}' if app.error_list else ''))
    check('**文件总数一张不少**', len(after_run) == len(start_files),
          f'{len(start_files)} -> {len(after_run)}')
    check('都已改名成 IMG_ 前缀',
          all(n.startswith('IMG_') for n in after_run),
          str([n for n in after_run if not n.startswith('IMG_')][:3]))

    print('\n=== 第 3 轮：再取消一次，确认反复来回也不掉文件 ===')
    wait_idle()
    run_round(cancel_at=15)
    final = files_now()
    check('三轮之后文件仍然一张不少', len(final) == len(start_files),
          f'{len(start_files)} -> {len(final)}')

    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing

    multiprocessing.freeze_support()
    main()
