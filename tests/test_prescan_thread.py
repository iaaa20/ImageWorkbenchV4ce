"""预扫描工作线程不得依赖 Tk 主循环，关窗后完成也不得访问已销毁的 Tk。"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from imgwb import app as module


def main():
    errors = []
    previous_hook = threading.excepthook
    threading.excepthook = lambda args: errors.append(str(args.exc_value))
    original_scan = module.scan_files
    app = module.ImageProcessorApp()
    app.root.withdraw()
    try:
        with tempfile.TemporaryDirectory() as directory:
            app.src_folder.set(directory)
            job = getattr(app, '_prescan_job', None)
            if job:
                app.root.after_cancel(job)
            workers = []
            gate = threading.Event()
            entered = threading.Event()

            def scan(*args):
                workers.append(threading.current_thread())
                entered.set()
                assert gate.wait(5), '测试没有释放扫描线程'
                return ['one.jpg', 'two.jpg']

            module.scan_files = scan
            app._prescan()
            assert entered.wait(3), '扫描线程未启动'
            # 不进入 Tk mainloop；后台计数必须能独立完成。
            gate.set()
            workers[-1].join(2)
            assert not workers[-1].is_alive(), '预扫描工作线程阻塞在 Tk 调用上'
            assert not errors, errors
            deadline = time.monotonic() + 2
            while app.lbl_progress.cget('text') != '0/2' and time.monotonic() < deadline:
                app.root.update()
                time.sleep(0.01)
            assert app.lbl_progress.cget('text') == '0/2', '扫描结果未回到界面线程'

            # 用户关窗时扫描可能仍在等待文件系统；扫描完成后不应触碰 Tk。
            gate.clear()
            entered.clear()
            app._prescan()
            assert entered.wait(3)
            app.root.destroy()
            gate.set()
            workers[-1].join(2)
            assert not workers[-1].is_alive()
            assert not errors, errors
        print('PASS 预扫描独立完成、主线程显示结果、关窗后扫描结束无 Tk 异常')
    finally:
        module.scan_files = original_scan
        threading.excepthook = previous_hook
        try:
            app.root.destroy()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
