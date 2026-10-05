"""进度日志：慢下来的时候也必须每 20 秒说一次话。

原来的进度日志是「每完成 5% 记一条」，几千张的批量里一条要等一百多张。
一旦某几张卡住，日志就是几分钟的空白，用户只能盯着不动的进度条猜 ——
用户实拍的截图里，两条 ⏳ 之间除了文件名什么都没有。

这一套盯三件事：
  1. 不管有没有任务完成，最多隔 PROGRESS_HEARTBEAT_SEC 就有一条带进度的日志
  2. 卡顿报告里每个在跑的文件要带「已跑多少秒」
  3. 慢任务要报出工人里的分段耗时，好分辨「真的在算」还是「卡在别处」

用法:  python tests\test_progress.py
会真的建出窗口，需要有桌面会话。
"""
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image

from imgwb import app as app_mod
from imgwb import config as config_mod
from imgwb import pipeline as pipeline_mod
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


# 门槛压到秒级，否则一套测试要跑 20 秒以上才看得到东西
app_mod.SLOW_TASK_SEC = 1


def slow_worker(args):
    """假装每张图都很慢，但**在工人里其实什么都没干**。

    这正是用户遇到的形态：文件不大、算得很快，可就是几分钟不出结果。
    timings 里的 total 很小，墙上时间很大 —— 日志必须能把这两者区分开。
    """
    time.sleep(2.5)
    src, dst = args[0], args[1]
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return TaskResult(True, src, dst,
                      timings={'open': 0.01, 'decode': 0.03,
                               'encode': 0.05, 'total': 0.09})


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, '素材')
    os.makedirs(src)
    for i in range(4):
        Image.new('RGB', (80, 60), (i * 40, 90, 150)).save(
            os.path.join(src, f'big{i}.jpg'))

    app_mod.process_single_image = slow_worker

    app = ImageProcessorApp()
    app.root.withdraw()
    app.src_folder.set(src)
    app.out_mode.set('new')
    app.new_folder_path.set(os.path.join(work, 'out'))
    app.compress_only.set(True)
    app.safe_write.set(False)
    # 第二轮会写进同一个导出目录，真实使用时这里会弹「目录非空」三选一。
    # 测试里没人点，模态框一弹 mainloop 就卡死 —— 直接选「并入」。
    app._ask_merge_choice = lambda *a, **k: 'merge'

    def run_once():
        app.log_text.configure(state='normal')
        app.log_text.delete('1.0', 'end')
        app.log_text.configure(state='disabled')
        app.start_processing()
        deadline = time.time() + 120

        def poll():
            if not app.is_running or time.time() > deadline:
                app.root.after(400, app.root.quit)
                return
            app.root.after(50, poll)

        app.root.after(50, poll)
        app.root.mainloop()
        text = app.log_text.get('1.0', 'end')
        out = [ln for ln in text.splitlines() if ln.strip()]
        print('\n---- 日志 ----')
        for ln in out:
            print('   ' + ln)
        print('---- 完 ----\n')
        return out

    # 第一轮：心跳门槛 1 秒、卡顿门槛调高 —— 单看心跳有没有按时说话。
    # 每张图 2.5 秒且并发跑，全程约 3 秒，够心跳响两次。
    app_mod.PROGRESS_HEARTBEAT_SEC = 1
    app_mod.STALL_REPORT_SEC = 30
    lines = run_once()

    check('处理跑完了', not app.is_running and app.processed_count == 4,
          f'{app.processed_count}/4')

    beats = [ln for ln in lines if '⏱' in ln and '进度' in ln]
    check('有心跳进度行', len(beats) >= 1, f'{len(beats)} 条')
    check('心跳里带了 x/y 和百分比',
          any(re.search(r'进度 \d+/\d+ \(\d+%\)', ln) for ln in beats),
          beats[0] if beats else '')
    check('心跳里带了「在处理 N 个」',
          any('在处理' in ln for ln in beats), beats[0] if beats else '')

    # 心跳必须是「没有任务完成也照样说话」。这一轮 4 张图要 2.5 秒才有第一个
    # 结果，期间的心跳全部发生在零完成的空窗里 —— 正是用户盯着不动的进度条
    # 想看到的东西。
    check('心跳发生在还没有任何任务完成时',
          any('进度 0/4' in ln for ln in beats),
          next((ln for ln in beats if '进度 0/4' in ln), '没有'))

    slow = [ln for ln in lines if '🐌' in ln]
    check('慢任务被点名', len(slow) >= 1, f'{len(slow)} 条')
    check('慢任务报了「在工人里多少秒」',
          any('在工人里' in ln for ln in slow), slow[0] if slow else '')
    check('慢任务列出了分段耗时',
          any('解码' in ln and '编码写盘' in ln for ln in slow),
          slow[0] if slow else '')
    # 关键：工人里只花了 0.09 秒，墙上却是 1.6 秒 —— 必须明说「不在图像处理上」，
    # 否则用户会以为是图片大、继续去优化根本不慢的解码。
    check('点破「时间不在图像处理上」',
          any('不在图像处理上' in ln for ln in lines),
          next((ln for ln in lines if '不在图像处理上' in ln), '没有这一行'))

    # 第二轮：把卡顿门槛压回 1 秒，验卡顿报告本身。用户截图里的老版本只有
    # 文件名和预估内存，看不出这几个到底卡了多久 —— 现在必须带上「已跑 N 秒」，
    # 并且顺带把整体进度一起报出来。
    print('\n=== 第二轮：卡顿报告 ===')
    app_mod.PROGRESS_HEARTBEAT_SEC = 30
    app_mod.STALL_REPORT_SEC = 1
    lines2 = run_once()

    stalls = [ln for ln in lines2 if '还在处理' in ln]
    check('触发了卡顿报告', len(stalls) >= 1, f'{len(stalls)} 条')
    check('卡顿报告里每个文件带「已跑 N 秒」',
          any(re.search(r'已跑 \d+ 秒', ln) for ln in lines2),
          next((ln for ln in lines2 if '已跑' in ln), '没有'))
    check('卡顿报告后面跟了整体进度',
          any('进度' in ln and '张/秒' in ln for ln in lines2),
          next((ln for ln in lines2 if '张/秒' in ln), '没有'))

    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing

    multiprocessing.freeze_support()
    main()
