"""入口（`融合_v4ce.py --cli` / `--selftest`）自己带进来的文件和文件夹，
不能伤到用户放在那儿的东西。

两条都是在**真实入口**上复现出来的，回归也就从真实入口跑：
另起一个进程执行 `融合_v4ce.py`，看的是磁盘上的结果，不是返回值。

* **`--cli-out` 报告放在来源文件夹里。** 报告在扫描之前就建好了，于是被
  当成一个"不支持的文件"扫进来：原地编号去改一个自己还开着的文件
  （WinError 32，照片已经改完名、命令却报失败）；导出则多拷出一个 0 字节的
  报告，计数虚增。
* **`--selftest --work 已有的文件夹`。** 自测成功后把整个文件夹删了，
  连同里面原有的、和自测毫无关系的文件。

用法:  python tests\\test_entry.py
不建窗口（自测用 `--no-gui`），约半分钟。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENTRY = os.path.join(ROOT, '融合_v4ce.py')

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def digest(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def entry(args, cwd, **env):
    return subprocess.run(
        [sys.executable, ENTRY] + args, cwd=cwd, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=300,
        env=dict(os.environ, PYTHONIOENCODING='utf-8', **env))


def photos(folder):
    """两张图加一个用户自己的 JSON 附件。返回 {内容哈希: 扩展名}。"""
    os.makedirs(folder)
    Image.new('RGB', (32, 24), 'red').save(os.path.join(folder, 'a.png'))
    Image.new('RGB', (32, 24), 'blue').save(os.path.join(folder, 'b.png'))
    with open(os.path.join(folder, 'notes.json'), 'w', encoding='utf-8') as fh:
        fh.write('{"mine": "用户自己的附件，不是报告"}')
    return {digest(os.path.join(folder, n)): os.path.splitext(n)[1]
            for n in os.listdir(folder)}


def contents(folder, skip=()):
    return {digest(os.path.join(folder, n)): os.path.splitext(n)[1]
            for n in os.listdir(folder) if n not in skip}


def read_report(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        return {'unreadable': f'{type(exc).__name__}: {exc}'}


def report_inside_source(work):
    print('一、--cli-out 报告放在来源文件夹里')

    # 原地编号。和最初复现它的命令一个写法：cwd 就是来源，两个路径都是相对的。
    src = os.path.join(work, 'inplace')
    want = photos(src)
    cmd = ['--cli', 'run', '--source', '.', '--name', 'number', '--json',
           '--cli-out', 'result.json']
    report = os.path.join(src, 'result.json')
    listings = []
    for nth in ('第一次', '第二次（报告已经在那儿了）'):
        p = entry(cmd, src)
        r = read_report(report)
        check(f'原地编号 · {nth}：退出码 0、报告说成功', p.returncode == 0
              and r.get('ok') is True, f'rc={p.returncode} {str(r)[:200]}')
        check(f'原地编号 · {nth}：扫到的是 3 个，报告自己不算',
              r.get('scanned') == 3 and r.get('processed') == 3,
              f'{r.get("scanned")}/{r.get("processed")}')
        listings.append(sorted(os.listdir(src)))
        check(f'原地编号 · {nth}：目录里是 3 个编了号的文件加报告，内容一个没变',
              len(listings[-1]) == 4 and 'result.json' in listings[-1]
              and contents(src, skip=('result.json',)) == want,
              str(listings[-1]))
    check('原地编号：再跑一次，名字不漂', listings[0] == listings[1],
          f'{listings[0]} -> {listings[1]}')
    check('用户自己的 JSON 附件照常跟着编号（排除的只是报告那一个文件）',
          any(n.endswith('.json') and n != 'result.json' for n in listings[0])
          and 'notes.json' not in listings[0], str(listings[0]))

    # 只算不写
    src = os.path.join(work, 'plan')
    want = photos(src)
    report = os.path.join(src, 'plan.json')
    p = entry(['--cli', 'plan', '--source', src, '--name', 'number', '--json',
               '--cli-out', report], work)
    r = read_report(report)
    check('预览：扫到 3 个，任务里没有报告自己',
          p.returncode == 0 and r.get('scanned') == 3
          and not any('plan.json' in t['src'] for t in r.get('tasks', [])),
          f'rc={p.returncode} {str(r)[:200]}')

    # 导出
    src, out = os.path.join(work, 'export'), os.path.join(work, 'export_out')
    want = photos(src)
    report = os.path.join(src, 'result.json')
    p = entry(['--cli', 'run', '--source', src, '--output', 'new', '--dest', out,
               '--no-keep-structure', '--name', 'keep', '--json',
               '--cli-out', report], work)
    r = read_report(report)
    check('导出：退出码 0，扫到 3 个', p.returncode == 0 and r.get('ok') is True
          and r.get('scanned') == 3, f'rc={p.returncode} {str(r)[:200]}')
    check('导出：产物正好 3 个，没有多拷出一份报告',
          sorted(os.listdir(out)) == ['a.png', 'b.png', 'notes.json']
          and contents(out) == want, str(sorted(os.listdir(out))))
    check('导出：来源没动', contents(src, skip=('result.json',)) == want)

    # 对照：报告在来源外面，本来就没事
    src = os.path.join(work, 'outside')
    want = photos(src)
    report = os.path.join(work, 'outside.json')
    p = entry(['--cli', 'run', '--source', src, '--name', 'number', '--json',
               '--cli-out', report], work)
    r = read_report(report)
    check('对照 · 报告在来源外：照常成功', p.returncode == 0
          and r.get('scanned') == 3 and contents(src) == want,
          f'rc={p.returncode} {str(r)[:200]}')


def shell_redirect_inside_source(work):
    """和上面是同一个问题，只是文件不是 `--cli-out` 给的，而是命令行用
    `>` 重定向进来的 —— 路径没人告诉程序。命令真的交给 cmd 去执行，
    重定向是 cmd 做的。"""
    print('\n一点五、用 > 把输出重定向到来源文件夹里')
    exe = f'"{sys.executable}" -m imgwb'
    # 用户的命令行里没有 PYTHONIOENCODING（run_all 给测试自己设了一个，这里
    # 拿掉）：重定向到文件时 Python 默认按系统代码页写，提示里的「📄」GBK
    # 写不出来 —— 这也得在真实条件下过。
    env = {k: v for k, v in os.environ.items()
           if k not in ('PYTHONIOENCODING', 'PYTHONUTF8')}
    env['PYTHONPATH'] = ROOT

    def shell(command, cwd):
        return subprocess.run(command, shell=True, cwd=cwd, timeout=300, env=env)

    src = os.path.join(work, 'redir_inplace')
    want = photos(src)
    report = os.path.join(src, 'result.json')
    listings = []
    for nth in ('第一次', '第二次（文件已经在那儿了）'):
        p = shell(f'{exe} run --source . --name number --json > result.json', src)
        r = read_report(report)
        check(f'原地编号 · {nth}：退出码 0，扫到 3 个',
              p.returncode == 0 and r.get('ok') is True and r.get('scanned') == 3
              and r.get('processed') == 3, f'rc={p.returncode} {str(r)[:200]}')
        listings.append(sorted(os.listdir(src)))
        check(f'原地编号 · {nth}：3 个编了号的文件加报告，内容一个没变',
              len(listings[-1]) == 4 and 'result.json' in listings[-1]
              and contents(src, skip=('result.json',)) == want,
              str(listings[-1]))
    check('原地编号：再跑一次，名字不漂', listings[0] == listings[1],
          f'{listings[0]} -> {listings[1]}')

    # 不加 --json：摘要走 stdout、进度走 stderr，两个都重定向进来源
    src = os.path.join(work, 'redir_both')
    want = photos(src)
    p = shell(f'{exe} run --source . --name number > out.txt 2> err.txt', src)
    names = sorted(os.listdir(src))
    try:
        with open(os.path.join(src, 'out.txt'), encoding='utf-8') as fh:
            said = fh.read()
    except OSError as exc:
        said = str(exc)
    check('stdout 和 stderr 都重定向进来源：退出码 0，说的是 3/3',
          p.returncode == 0 and '3/3' in said, f'rc={p.returncode} {said[:120]}')
    check('两个输出文件都没被编号、也没被改动别的文件',
          len(names) == 5 and {'out.txt', 'err.txt'} <= set(names)
          and contents(src, skip=('out.txt', 'err.txt')) == want, str(names))

    src, out = os.path.join(work, 'redir_export'), os.path.join(work, 'redir_out')
    want = photos(src)
    p = shell(f'{exe} run --source . --output new --dest "{out}" '
              f'--no-keep-structure --name keep --json > result.json', src)
    r = read_report(os.path.join(src, 'result.json'))
    check('导出：退出码 0，扫到 3 个，产物正好 3 个',
          p.returncode == 0 and r.get('scanned') == 3
          and sorted(os.listdir(out)) == ['a.png', 'b.png', 'notes.json']
          and contents(out) == want,
          f'rc={p.returncode} {str(r)[:160]}')


def selftest_work_folder(work):
    print('\n二、--selftest --work 指向一个已有的、放着东西的文件夹')
    # 三趟自测互不相干，一起起、一起等
    mine = os.path.join(work, 'my_folder')
    os.makedirs(os.path.join(mine, 'nested'))
    keep = {os.path.join(mine, 'unrelated-notes.txt'): 'USER NOTES: preserve me',
            os.path.join(mine, 'nested', 'important.txt'): 'USER CONTENT'}
    for path, text in keep.items():
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
    before = {p: digest(p) for p in keep}
    inside = os.path.join(mine, 'report.json')      # 报告也放在那个文件夹里

    private_temp = os.path.join(work, 'private_temp')
    os.makedirs(private_temp)
    default_report = os.path.join(work, 'default.json')

    nowhere = os.path.join(work, 'no_such_dir', 'report.json')
    lost_work = os.path.join(work, 'lost_work')

    def start(args, **env):
        return subprocess.Popen(
            [sys.executable, ENTRY, '--selftest', '--no-gui'] + args, cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=dict(os.environ, PYTHONIOENCODING='utf-8', **env))

    runs = [start(['--work', mine, '--cli-out', inside]),
            start(['--cli-out', default_report],
                  TEMP=private_temp, TMP=private_temp),
            start(['--work', lost_work, '--cli-out', nowhere])]
    codes = [p.wait(timeout=300) for p in runs]

    r = read_report(inside)
    check('指定已有文件夹：自测通过、退出码 0',
          codes[0] == 0 and r.get('ok') is True, f'rc={codes[0]} {str(r)[:300]}')
    check('**文件夹里原有的文件一个没少、一个字节没变**',
          all(os.path.isfile(p) and digest(p) == before[p] for p in keep),
          str([p for p in keep if not os.path.isfile(p)]))
    left = sorted(os.listdir(mine)) if os.path.isdir(mine) else None
    check('自测自己的东西清干净了：文件夹里只剩原有的和报告',
          left == ['nested', 'report.json', 'unrelated-notes.txt']
          and os.listdir(os.path.join(mine, 'nested')) == ['important.txt'],
          str(left))
    used = os.path.normcase(os.path.abspath(str(r.get('work', ''))))
    check('它用的是那个文件夹**底下**自己建的子目录，不是那个文件夹本身',
          os.path.dirname(used) == os.path.normcase(os.path.abspath(mine)),
          str(r.get('work')))

    r = read_report(default_report)
    check('对照 · 不指定：自测通过，临时目录里什么都没留',
          codes[1] == 0 and r.get('ok') is True
          and not [n for n in os.listdir(private_temp)
                   if n.startswith('imgwb-selftest-')],
          f'rc={codes[1]} {os.listdir(private_temp)[:3]} {str(r)[:200]}')

    check('报告写不出来：退出码不是 0（不能一边没报告一边说全过）',
          codes[2] == 1 and not os.path.exists(nowhere), f'rc={codes[2]}')


def main():
    work = tempfile.mkdtemp(dir=os.environ.get('IMGWB_TEST_TMP') or None)
    try:
        report_inside_source(work)
        shell_redirect_inside_source(work)
        selftest_work_folder(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
