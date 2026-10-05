"""命令行入口 —— 让脚本和 AI 代理能直接调用，无需打开界面。

    python -m imgwb --help
    python -m imgwb capabilities
    python -m imgwb plan --source D:\\照片 --name number
    python -m imgwb run  --source D:\\照片 --name number --rotate cw90

加 ``--json`` 时只往 stdout 打印一个 JSON 对象，便于程序解析；
其余提示一律走 stderr，不会污染 stdout。
"""

# ============================================================
# 【导读】cli.py —— 命令行版本（python -m imgwb ...），本质是把参数转交给 api.py。
# ============================================================
import argparse
import json
import os
import stat
import sys

from . import perf
from .api import (ICO_FORMAT, NAME_MODES, OUTPUTS, QUALITY, ROTATE,
                  OptionError, capabilities, clean, plan, run)


# 定义命令行能接受哪些参数（例如 python -m imgwb run --source 某文件夹 ...）。
def build_parser():
    p = argparse.ArgumentParser(
        prog='python -m imgwb',
        description='图片批量处理：旋转、压缩、重命名编号、转 ICO。')
    sub = p.add_subparsers(dest='command', required=True)

    sub.add_parser('capabilities', help='输出支持的格式与所有可选值（JSON）')

    c = sub.add_parser(
        'clean', help='清掉一个文件夹里残留的临时文件（名字里带 .~imgwb 的）')
    c.add_argument('--source', required=True, help='要清理的文件夹')
    c.add_argument('--no-recursive', action='store_true', help='不进子文件夹')
    c.add_argument('--include-unknown', action='store_true',
                   help='连"判断不了是谁留下的"也删。先确认没有别的电脑正在'
                        '处理这个文件夹')
    c.add_argument('--dry-run', action='store_true', help='只列出会删什么，不动手')
    c.add_argument('--json', action='store_true', help='只输出 JSON')

    for cmd, helptext in (('plan', '只计算不写盘，预览每个文件的目标路径'),
                          ('run', '真正执行')):
        s = sub.add_parser(cmd, help=helptext)
        s.add_argument('--source', required=True,
                       help='源文件夹；也可以给多个文件路径')
        s.add_argument('--files', nargs='*', default=None,
                       help='直接指定要处理的文件（给了这个就忽略 --source 的扫描）')
        s.add_argument('--no-recursive', action='store_true', help='不扫描子文件夹')

        s.add_argument('--output', choices=OUTPUTS, default='inplace',
                       help='inplace=原地修改（会删除改名前的原文件），new=导出到新位置')
        s.add_argument('--dest', help='output=new 时的导出目录')
        s.add_argument('--no-keep-structure', action='store_true',
                       help='导出时不保留子目录结构，全部平铺')
        s.add_argument('--dir-suffix', default='_已处理', help='导出时的输出文件夹后缀')

        s.add_argument('--name', choices=NAME_MODES, default='number')
        s.add_argument('--prefix', default='IMG', help='name=prefix 时的前缀')
        s.add_argument('--start', type=int, default=1, help='起始编号')
        s.add_argument('--digits', default='auto',
                       help="1~5，或 auto（按文件总数定宽，默认）")

        s.add_argument('--rotate', choices=sorted(ROTATE), default='none')
        s.add_argument('--quality', choices=sorted(QUALITY), default='95')
        s.add_argument('--compress-only', action='store_true',
                       help='不旋转时也应用画质设置')

        s.add_argument('--ico', action='store_true', help='转成 ICO 图标')
        s.add_argument('--ico-size', default='multi',
                       help='multi | fixed_256 | auto_256 等（上限 256）')
        s.add_argument('--ico-format', choices=sorted(ICO_FORMAT), default='auto')

        s.add_argument('--no-copy-others', action='store_true',
                       help='不支持的格式直接跳过，不复制过去')
        s.add_argument('--max-edge', type=int, default=0,
                       help='最长边上限（像素），0=不限。只缩不放，小图不动。'
                            '实测 4000x3000 限到 1920 体积只剩 0.14 倍')
        s.add_argument('--reencode-animated', action='store_true',
                       help='不旋转时也把动图整段重编码（默认原样复制：'
                            '重编码慢得多，且有损源再编一遍往往更大）')
        s.add_argument('--no-safe-write', action='store_true',
                       help='直接写盘、省磁盘，但中途中断无法完整撤回')
        s.add_argument('--perf', choices=sorted(perf.MODES), default='balanced',
                       help='eco=节能(后台优先级，不卡电脑) / balanced / '
                            'turbo=全核 / custom=配合 --workers')
        s.add_argument('--workers', type=int, help='并发数，仅 --perf custom 时需要')
        s.add_argument('--json', action='store_true', help='只输出 JSON')

    return p


# 把命令行参数整理成 api.run() / api.plan() 需要的格式。
def _options(a):
    return dict(
        source=a.files if a.files else a.source,
        recursive=not a.no_recursive,
        output=a.output,
        dest=a.dest,
        keep_structure=not a.no_keep_structure,
        dir_suffix=a.dir_suffix,
        name=a.name,
        prefix=a.prefix,
        start=a.start,
        digits=a.digits if a.digits == 'auto' else int(a.digits),
        rotate=a.rotate,
        quality=a.quality,
        compress_only=a.compress_only,
        ico=a.ico,
        ico_size=a.ico_size,
        ico_format=a.ico_format,
        safe_write=not a.no_safe_write,
        copy_others=not a.no_copy_others,
        reencode_animated=a.reencode_animated,
        max_edge=a.max_edge,
        perf_mode=a.perf,
    )


# 打印结果：加了 --json 就输出机器可读的 JSON，否则打印给人看的摘要。
def _report(result, as_json):
    if as_json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write('\n')
        return

    if result.get('dry_run'):
        print(f'来源 {result["source"]}  ->  {result["dest"]}')
        print(f'扫描到 {result["scanned"]} 个文件')
        for t in result['tasks'][:20]:
            print(f'  {t["src"]}\n    -> {t["dst"]}')
        if len(result['tasks']) > 20:
            print(f'  …… 其余 {len(result["tasks"]) - 20} 个略')
        if result['will_delete_originals']:
            print(f'⚠ 执行后会删除 {len(result["will_delete_originals"])} 个改名前的原文件')
        return

    print(f'完成 {result["processed"]}/{result["scanned"]}，'
          f'耗时 {result["elapsed_sec"]}s')
    if result['failed']:
        print(f'失败 {len(result["failed"])} 个:')
        for f in result['failed'][:20]:
            print(f'  {f["path"]}\n    {f["reason"]}')
        if len(result['failed']) > 20:
            print(f'  …… 其余 {len(result["failed"]) - 20} 个略')


def _path_of_fd(fd):
    """一个已经打开的文件描述符指向磁盘上的哪个文件。查不出来返回 None。"""
    if sys.platform != 'win32':
        try:
            return os.readlink(f'/proc/self/fd/{fd}')
        except OSError:
            return None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    buf = ctypes.create_unicode_buffer(32768)
    got = k32.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(fd), buf,
                                        len(buf), 0)
    if not got or got >= len(buf):
        return None
    path = buf.value
    if path.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path[8:]
    return path[4:] if path.startswith('\\\\?\\') else path


def _redirected_outputs():
    """stdout / stderr 要是被重定向到了磁盘上的文件，返回那些文件的路径。

    `python -m imgwb run --source . --json > result.json`：那个文件是命令行
    替我们建的，在程序启动之前就已经躺在来源文件夹里了，而且一直开着。
    和 `--cli-out` 的报告是同一个问题，只是路径没人告诉我们，
    得自己从句柄上问出来。问不出来就算了 —— 那样只是回到没排除的老样子。

    顺手把写进**文件**的输出定成 UTF-8（和 `--cli-out` 一致）。重定向到文件时
    Python 默认用系统代码页，中文 Windows 上是 GBK，而提示里有 GBK 没有的
    字符（「📄 N 个不支持的文件…」）—— 来源里只要有一个非图片文件，
    结果还没写出去就先死在编码上。控制台和管道不动。
    """
    found = []
    for stream in (sys.stdout, sys.stderr):
        try:
            fd = stream.fileno()
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                continue                # 控制台、管道、空设备
            stream.reconfigure(encoding='utf-8')
            path = _path_of_fd(fd)
        except Exception:
            continue
        if path:
            found.append(path)
    return found


# 命令行入口：解析参数 → 调用对应功能（查看能力 / 预览计划 / 真正处理）→ 打印结果。
#
# `reserved` 是入口替我们打开的输出文件（exe 的 `--cli-out`）。它在扫描之前
# 就已经建好了，放在来源文件夹里的话不能被当成输入，原样交给 api 排除掉。
# 用 `> 文件` / `2> 文件` 重定向进来的也是同一回事，一并算上。
def main(argv=None, reserved=None):
    args = build_parser().parse_args(argv)
    reserved = list(reserved or ()) + _redirected_outputs()

    if args.command == 'capabilities':
        json.dump(capabilities(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write('\n')
        return 0

    if args.command == 'clean':
        try:
            result = clean(args.source, recursive=not args.no_recursive,
                           include_unknown=args.include_unknown,
                           dry_run=args.dry_run)
        except OptionError as exc:
            if args.json:
                json.dump({'ok': False, 'error': str(exc), 'error_type': 'option'},
                          sys.stdout, ensure_ascii=False, indent=2)
                sys.stdout.write('\n')
            else:
                print(f'参数错误: {exc}', file=sys.stderr)
            return 2
        if args.json:
            json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write('\n')
        else:
            verb = '会删除' if args.dry_run else '已删除'
            print(f'{verb} {len(result["removed"])} 个临时文件')
            if result['kept_live']:
                print(f'保留 {len(result["kept_live"])} 个：另一个正在运行的'
                      f'实例还要用')
            if result['kept_unknown']:
                print(f'保留 {len(result["kept_unknown"])} 个来历不明的'
                      f'（加 --include-unknown 才删）')
            if result['backups']:
                print(f'另有 {len(result["backups"])} 个备份文件没有动 —— '
                      f'它们是原文件，请确认后手工改回原名:')
                for p in result['backups'][:10]:
                    print(f'  {p}')
        return 0

    try:
        if args.command == 'plan':
            result = plan(**_options(args), exclude=reserved)
        else:
            # 处理进度回调：每做完一张就刷新一下「处理中 x/y」。
            def tick(done, total):
                if not args.json:
                    print(f'\r处理中 {done}/{total}', end='', file=sys.stderr)

            result = run(**_options(args), workers=args.workers, progress=tick,
                         exclude=reserved)
            if not args.json:
                print(file=sys.stderr)
    except OptionError as exc:
        # 参数错单独归一类，方便调用方（尤其是自动化）区分「用法错了」和「跑挂了」
        if args.json:
            json.dump({'ok': False, 'error': str(exc), 'error_type': 'option'},
                      sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write('\n')
        else:
            print(f'参数错误: {exc}', file=sys.stderr)
        return 2

    _report(result, args.json)
    return 0 if result.get('ok') else 1
