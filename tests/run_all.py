"""把 tests\ 下所有测试跑一遍，**并且真的能发现失败**。

用法:
    python tests\run_all.py              跑全部
    python tests\run_all.py ui i18n      只跑名字里含这些字的
    python tests\run_all.py --list       只列出来

需要桌面会话（一半的测试会真的建窗口），全部跑完约三到五分钟。

----------------------------------------------------------------------
**为什么需要这个入口：只看退出码会被骗。**

之前的做法是 `for t in tests/test_*.py: python $t; 看 $?`，报「35/35 全过」。
实际上同时有两种失败被这个做法漏掉：

1. `test_input.py` 打印了 `FAIL` 和「有失败」，**退出码却是 0**（脚本忘了
   `sys.exit(1)`）。一条过时断言就这样红了很久没人看见。
2. `test_ui.py` 的预扫描在 Tk 回调里抛 `TclError`。**Tk 会把回调异常接住、
   打到 stderr、然后让程序继续跑** —— 退出码当然还是 0。

所以这个入口按三条判失败，任一条成立就算红：

* 退出码非零；
* 输出里有以 `FAIL` 开头的行（测试统一用 `check()` 打这个前缀）；
* stderr 里出现 `Exception in Tkinter callback`（Tk 钩子没装上时的兜底）；
* stderr 里出现 `Exception in thread`（后台线程异常的兜底）。

第四条是后补的：**普通后台线程里没人接住的异常，既不改退出码、也不打
`FAIL`**，默认处理器只往 stderr 写一行就完事 —— 前三条一条都碰不到它。
更糟的是本项目装了日志钩子之后，那一行连 stderr 都不去了（进日志文件），
所以钩子在严格模式下会**另外往 stdout 打一行 `FAIL`**
（见 `logfile.install_hooks` 里的 `thread_hook`）。两条路都留着。

跑的时候会设 `IMGWB_TK_STRICT=1`，让 `app.py` 的回调异常钩子把异常也打成
一行 `FAIL`（见 `ImageProcessorApp._on_tk_error`）。
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 以 FAIL 开头的行。测试里的 check() 打的是 '  FAIL 名字'，
# 回调异常钩子打的是 'FAIL 界面回调异常 ...'，都能命中。
FAIL_LINE = re.compile(r'^\s*FAIL\b')
TK_LEAK = 'Exception in Tkinter callback'
# 普通后台线程里没人接住的异常。**这一条是后补的。**
# 它不会改变主进程退出码，默认处理器只往 stderr 打一行 `Exception in thread`，
# 所以前面那三条判据一条都碰不到它 —— 实测 10/10 漏报。
THREAD_LEAK = 'Exception in thread'


def judge(rc, out, err):
    """返回 (是否失败, 原因列表)。"""
    reasons = []
    if rc != 0:
        reasons.append(f'退出码 {rc}')
    fails = [l.strip() for l in (out + '\n' + err).splitlines()
             if FAIL_LINE.match(l)]
    if fails:
        reasons.append(f'{len(fails)} 条 FAIL')
    if TK_LEAK in err:
        reasons.append('界面回调异常漏到 stderr')
    if THREAD_LEAK in err:
        reasons.append('后台线程异常漏到 stderr')
    return bool(reasons), reasons, fails


def main(argv):
    if '--list' in argv:
        for name in sorted(os.listdir(HERE)):
            if name.startswith('test_') and name.endswith('.py'):
                print(' ', name)
        return 0

    picks = [a for a in argv if not a.startswith('-')]
    scripts = sorted(n for n in os.listdir(HERE)
                     if n.startswith('test_') and n.endswith('.py')
                     and (not picks or any(p in n for p in picks)))
    if not scripts:
        print('没有匹配的测试')
        return 2

    env = dict(os.environ)
    # 让界面回调异常也变成一行 FAIL（否则 Tk 接住就不吭声了）
    env['IMGWB_TK_STRICT'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'

    bad = {}
    t0 = time.time()
    print(f'跑 {len(scripts)} 个测试，设了 IMGWB_TK_STRICT=1\n')
    for i, name in enumerate(scripts, 1):
        t1 = time.time()
        p = subprocess.run([sys.executable, os.path.join(HERE, name)],
                           cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        failed, reasons, fails = judge(p.returncode, p.stdout or '',
                                       p.stderr or '')
        secs = time.time() - t1
        print(f'[{i:2}/{len(scripts)}] {"✗" if failed else "✓"} '
              f'{name:<26} {secs:5.1f}s'
              + (f'   {"、".join(reasons)}' if failed else ''))
        if failed:
            bad[name] = (reasons, fails, p.stdout or '', p.stderr or '')

    print(f'\n{"=" * 60}')
    print(f'通过 {len(scripts) - len(bad)} / {len(scripts)}，'
          f'耗时 {time.time() - t0:.0f}s')
    if bad:
        print(f'\n失败 {len(bad)} 个：')
        for name, (reasons, fails, out, err) in bad.items():
            print(f'\n--- {name}（{"、".join(reasons)}）---')
            for f in fails[:8]:
                print('   ', f[:150])
            if TK_LEAK in err or THREAD_LEAK in err:
                tail = [l for l in err.splitlines() if l.strip()][-6:]
                for l in tail:
                    print('    stderr:', l[:150])
        return 1
    print('\n全部通过')
    return 0


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main(sys.argv[1:]))
