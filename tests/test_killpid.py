"""按 pid 收尾时的防误杀：**不确定是不是自己的进程，就不杀。**

收尾时我们手里只有一串记下来的 pid，而 pid 会被系统回收再分配。
用户可能同时开着本程序的另一个窗口在跑批 —— 它的工作进程和我们**同名**。
所以 `perf.kill_pid` 动手前有三道校验（见它的文档），这里各钉一条：

1. 映像名对不上        -> 不杀（原来就有，test_forcestop 也盯着）
2. **映像名查不到**    -> 不杀。原来查询失败时直接跳过校验往下走，
                          等于"不知道它是谁，那就杀吧"
3. **创建时刻对不上**  -> 不杀。同名但不是当初记下的那个进程
                          （pid 被回收给了另一个同名实例）

真的 pid 回收没法在测试里按需制造，所以第 3 条用"给一个错的创建时刻"来模拟
—— 被测的判断是同一句。每一条都先确认**对的参数确实杀得掉**，
免得"不杀"其实是因为函数整个坏了。

用法:  python tests\\test_killpid.py
不建窗口，几秒。只在 Windows 上有意义。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from imgwb import perf

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def sleeper():
    """一个和我们同映像（同一个 python.exe）的无害子进程。"""
    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    time.sleep(0.5)
    return p


def gone(p, wait=5):
    try:
        p.wait(timeout=wait)
        return True
    except subprocess.TimeoutExpired:
        return False


def main():
    if sys.platform != 'win32':
        print('只在 Windows 上有意义，跳过')
        return 0

    procs = []
    try:
        print('一、创建时刻')
        a = sleeper()
        procs.append(a)
        born = perf.process_born(a.pid)
        check('能拿到子进程的创建时刻', isinstance(born, int) and born > 0,
              repr(born))
        check('同一个进程问两次是同一个值', perf.process_born(a.pid) == born)
        b = sleeper()
        procs.append(b)
        check('两个进程的创建时刻不同', perf.process_born(b.pid) != born)

        print('\n二、创建时刻对不上 -> 不杀（模拟 pid 被回收给另一个同名进程）')
        check('错的创建时刻：返回 False',
              perf.kill_pid(a.pid, born=born + 1) is False)
        check('而且进程确实还活着', a.poll() is None)
        check('拿别的进程的创建时刻来杀它：也不杀',
              perf.kill_pid(a.pid, born=perf.process_born(b.pid)) is False
              and a.poll() is None)

        print('\n三、映像名查不到 -> 不杀')
        real = perf._query_image
        perf._query_image = lambda *args: None
        try:
            got = perf.kill_pid(a.pid, born=born)
        finally:
            perf._query_image = real
        check('查不到映像名：返回 False', got is False)
        check('而且进程确实还活着', a.poll() is None)

        print('\n四、对照：参数都对的时候是杀得掉的')
        check('映像名对不上：不杀',
              perf.kill_pid(a.pid, expect_image='__never_matches__.exe',
                            born=born) is False and a.poll() is None)
        check('创建时刻对得上：杀掉', perf.kill_pid(a.pid, born=born) is True
              and gone(a))
        check('不传创建时刻（旧调用方式）照常能杀',
              perf.kill_pid(b.pid) is True and gone(b))
        check('已经退出的进程：返回 False', perf.kill_pid(a.pid, born=born) is False)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
