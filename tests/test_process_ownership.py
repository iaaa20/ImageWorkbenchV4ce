"""完整收尾调用链：失配的历史 PID 不能认领现在的后代。只用本测试创建的进程。"""
import ctypes
from ctypes import wintypes
import os
import importlib.util
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from imgwb import perf
from imgwb.app import ImageProcessorApp


@unittest.skipUnless(sys.platform == 'win32', 'Windows 进程身份检查')
class Ownership(unittest.TestCase):
    def setUp(self):
        # 父进程保留孩子的 Popen 句柄；测试另持查询/等待句柄，不按名字清理。
        code = ("import subprocess,sys; "
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                "print(p.pid,flush=True); sys.stdin.readline(); "
                "p.kill() if p.poll() is None else None; p.wait()")
        self.parent = subprocess.Popen([sys.executable, '-c', code],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
        self.child = int(self.parent.stdout.readline())
        self.births = {p: perf.process_born(p) for p in (self.parent.pid, self.child)}
        self.assertTrue(all(self.births.values()))
        self.k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.k32.OpenProcess(0x100000, False, self.child)
        self.assertTrue(self.handle)
        self.app = object.__new__(ImageProcessorApp)
        self.app._child_pids = {self.parent.pid}
        self.app._child_born = {self.parent.pid: self.births[self.parent.pid]}

    def tearDown(self):
        perf.kill_pid(self.child, born=self.births[self.child])
        if self.parent.poll() is None:
            self.parent.communicate('\n', timeout=5)
        else:
            self.parent.communicate(timeout=5)
        self.k32.CloseHandle(self.handle)

    def child_alive(self):
        return self.k32.WaitForSingleObject(self.handle, 100) == 258  # WAIT_TIMEOUT

    def test_reused_parent_does_not_claim_its_children(self):
        self.app._child_born[self.parent.pid] += 1
        self.assertEqual(self.app._kill_leftover_children(), 0)
        self.assertIsNone(self.parent.poll())
        self.assertTrue(self.child_alive())

    def test_unknown_parent_identity_is_not_authority(self):
        self.app._child_born[self.parent.pid] = None
        self.assertEqual(self.app._kill_leftover_children(), 0)
        self.assertIsNone(self.parent.poll())
        self.assertTrue(self.child_alive())

    def test_owned_tree_is_still_terminated(self):
        self.assertEqual(self.app._kill_leftover_children(), 2)
        self.parent.wait(timeout=5)
        self.assertEqual(self.k32.WaitForSingleObject(self.handle, 5000), 0)

    def test_known_child_is_reaped_even_if_parent_identity_is_stale(self):
        self.app._child_born[self.parent.pid] += 1
        self.app._child_pids.add(self.child)
        self.app._child_born[self.child] = self.births[self.child]
        self.assertEqual(self.app._kill_leftover_children(), 1)
        self.assertIsNone(self.parent.poll())
        self.assertEqual(self.k32.WaitForSingleObject(self.handle, 5000), 0)

    def test_dispatch_loop_asks_birth_once_per_worker(self):
        """派发循环每完成一个任务就调一次 `_remember_children`。每圈都给全部
        工人各查一遍创建时刻的话，实测每秒白花约 8 毫秒 —— 只该在出现新的
        进程对象时查。同一个 PID 换了进程对象（池子换代）则必须重新查。"""
        import time

        class Proc:
            def __init__(self, pid):
                self.pid = pid

        pool = type('Pool', (), {})()
        pool._processes = {1: Proc(self.parent.pid)}
        self.app.executor = pool
        self.app._child_pids, self.app._child_born = set(), {}
        self.app._last_tree_scan = time.time()      # 这里不测全系统快照那半截
        asked, real = [], perf.process_born
        perf.process_born = lambda pid: (asked.append(pid), real(pid))[1]
        try:
            for _ in range(50):
                self.app._remember_children()
            self.assertEqual(asked, [self.parent.pid])
            self.assertEqual(self.app._child_born,
                             {self.parent.pid: self.births[self.parent.pid]})
            pool._processes = {1: Proc(self.parent.pid)}   # 同 PID、新进程对象
            for _ in range(50):
                self.app._remember_children()
            self.assertEqual(asked, [self.parent.pid] * 2)
        finally:
            perf.process_born = real
            self.app._child_pids, self.app._child_born = set(), {}

    def selfcheck(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '完整自检.py')
        spec = importlib.util.spec_from_file_location('selfcheck_test', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.OWN_PIDS.add(self.parent.pid)
        mod.OWN_IMAGES.add(mod._norm(sys.executable))
        # 旧实现缺少这一记录，允许同一测试在回滚快照里证明旧实现会失败。
        mod.OWN_BORN = {self.parent.pid: self.births[self.parent.pid]}
        return mod

    def test_selfcheck_does_not_claim_reused_parent(self):
        mod = self.selfcheck()
        mod.OWN_BORN[self.parent.pid] += 1
        found = mod._tree(self.parent.pid)
        mod.OWN_PIDS.update(found)
        self.assertEqual(found, set())
        self.assertTrue(mod.stop_own())
        self.assertIsNone(self.parent.poll())
        self.assertTrue(self.child_alive())

    def test_selfcheck_still_stops_owned_tree(self):
        mod = self.selfcheck()
        mod.OWN_PIDS.update(mod._tree(self.parent.pid))
        self.assertTrue(mod.stop_own())
        self.parent.wait(timeout=5)
        self.assertEqual(self.k32.WaitForSingleObject(self.handle, 5000), 0)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(Ownership))
    sys.exit(0 if result.wasSuccessful() else 1)
