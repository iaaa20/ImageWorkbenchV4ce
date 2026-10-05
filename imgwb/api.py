"""无界面调用接口 —— 给脚本、自动化流程和 AI 代理用。

设计原则：

* 参数全用 ASCII 词元（``cw90``、``number``），不用界面上的中文标签，方便
  程序生成和校验；内部再翻译成流水线认识的值。
* 返回值是纯 JSON 可序列化的 dict，调用方不需要了解 Tk 或 Pillow。
* ``plan()`` 只算不写。任何会改动/删除文件的操作，都建议先 plan 看一眼。

典型用法::

    from imgwb.api import plan, run

    preview = plan(source=r'D:\\照片', output='inplace', name='number')
    # 确认 preview['tasks'] 里的改名符合预期，再真正执行
    result = run(source=r'D:\\照片', output='inplace', name='number')
"""

# ============================================================
# 【导读】api.py —— 不开窗口也能用的接口，给脚本和自动化流程调用。
# plan()：只看不做，列出会怎么处理；run()：真正处理。
# 内部用的是和界面完全相同的处理代码。
# ============================================================
import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

from .config import PROCESS_THRESHOLD, VALID_EXTENSIONS
from . import perf
from .pipeline import process_single_image
from .tasks import (_same_path, build_tasks, clean_leftovers, commit_staged,
                    output_dirs_for,
                    output_escape_error, output_lands_in_source,
                    planned_output_lands_in_source, scan_files)
from .utils import name_segment_error

# 对外词元 -> 流水线内部使用的值
ROTATE = {
    'none': '不旋转',
    'cw90': '顺时针 90°',
    'ccw90': '逆时针 90°',
    '180': '旋转 180°',
}
QUALITY = {
    '100': '极致保真 (100%)',
    '95': '通用高清 (95%)',
    '85': '网络压缩 (85%)',
}
ICO_FORMAT = {
    'auto': '自动 (推荐)',
    'png': '强制 PNG (支持透明)',
    'bmp': '强制 BMP (经典兼容)',
}
NAME_MODES = ('prefix', 'number', 'keep')
OUTPUTS = ('inplace', 'new')


class OptionError(ValueError):
    """参数不合法。消息里会写清楚合法取值，方便调用方自己纠正。"""


# 把简短的参数（如 quality="95"）翻译成界面上的完整选项文字；填错了就报清楚能填哪些。
def _pick(name, value, table):
    if value not in table:
        raise OptionError(
            f'{name} 只能是 {sorted(table)} 之一，收到 {value!r}')
    return table[value] if isinstance(table, dict) else value


# 模拟界面上的设置变量。api 没有窗口，用它包一层，
# 让同一套处理代码既能给界面用、也能给脚本用。
class _Frozen:
    __slots__ = ('v',)

    # 存下值。
    def __init__(self, v):
        self.v = v

    # 跟界面变量一样用 .get() 取值。
    def get(self):
        return self.v


def _settings(**kw):
    """拼一个和界面快照同形状的配置对象，好让 tasks 那边原样复用。"""
    snap = type('Settings', (), {})()
    for key, value in kw.items():
        snap.__dict__[key] = _Frozen(value)
    return snap


def _prepare(source, recursive, output, dest, keep_structure, dir_suffix,
             name, prefix, start, digits, rotate, quality, compress_only,
             ico, ico_size, ico_format, safe_write, copy_others, perf_mode,
             reencode_animated=False, max_edge=0, exclude=None):
    """把外部参数校验、翻译成任务列表。plan 和 run 共用这一段。"""
    if name not in NAME_MODES:
        raise OptionError(f'name 只能是 {list(NAME_MODES)} 之一，收到 {name!r}')
    if output not in OUTPUTS:
        raise OptionError(f'output 只能是 {list(OUTPUTS)} 之一，收到 {output!r}')

    rotate_val = _pick('rotate', rotate, ROTATE)
    quality_val = _pick('quality', quality, QUALITY)
    ico_format_val = _pick('ico_format', ico_format, ICO_FORMAT)

    if not isinstance(start, int) or start < 0:
        raise OptionError(f'start 必须是非负整数，收到 {start!r}')
    if perf_mode not in perf.MODES:
        raise OptionError(
            f'perf 只能是 {sorted(perf.MODES)} 之一，收到 {perf_mode!r}')
    if digits != 'auto' and digits not in (1, 2, 3, 4, 5):
        raise OptionError(f"digits 只能是 1~5 或 'auto'，收到 {digits!r}")
    # **prefix 和 dir_suffix 是名字片段，不是路径。** 它们会被直接拼进
    # os.path.join，而 Windows 下绝对路径会把前面的导出目录整个丢掉 ——
    # 产物落到导出目录之外、覆盖源图，还一路返回 ok=true。
    for value, label, argname in ((prefix, '前缀', 'prefix'),
                                  (dir_suffix, '目录后缀', 'dir_suffix')):
        bad = name_segment_error(value, label)
        if bad:
            raise OptionError(f'{argname}: {bad}（收到 {value!r}）')
    if not isinstance(max_edge, int) or max_edge < 0:
        raise OptionError(f'max_edge 必须是非负整数（0 = 不限），收到 {max_edge!r}')

    dropped = []
    if isinstance(source, (list, tuple)):
        dropped = [os.path.abspath(p) for p in source]
        missing = [p for p in dropped if not os.path.isfile(p)]
        if missing:
            raise OptionError(f'这些文件不存在: {missing[:5]}')
        src_root = os.path.dirname(dropped[0]) if dropped else ''
    else:
        src_root = os.path.abspath(source)
        if not os.path.isdir(src_root):
            raise OptionError(f'source 不是文件夹: {source!r}')

    if output == 'new':
        if not dest:
            raise OptionError('output="new" 时必须给 dest')
        dst_root = os.path.abspath(dest)
    else:
        dst_root = src_root

    cfg = _settings(
        scan_subdirs=recursive,
        start_num=start,
        digit_val='自动 (按数量)' if digits == 'auto' else f'{digits}位',
        prefix_val=prefix,
        name_mode=name,
        out_mode='new' if output == 'new' else 'original',
        keep_structure=keep_structure,
        dir_suffix_val=dir_suffix,
        ico_mode_val=str(ico_size),
        ico_format_val=ico_format_val,
        rotate_val=rotate_val,
        quality_val=quality_val,
        compress_only=compress_only,
        is_ico=ico,
        safe_write=safe_write,
        copy_others=copy_others,
        reencode_anim=reencode_animated,
        max_edge_val=max_edge,
        perf_mode=perf_mode,
    )
    # 导出目录落在来源里会让产物被下一轮重新扫进来，文件每跑一次翻一倍。
    # 命令行/脚本比界面更容易踩到 —— 顺手就把 dest 写成源目录了。
    escape = output_escape_error(src_root, dst_root, output == 'new')
    if escape:
        raise OptionError(escape.replace(chr(10), ' '))
    # 顶层 dest 过了关，不代表**算出来的**输出目录也在来源之外 ——
    # 后缀留空时 `dest\<源名><后缀>` 正好等于来源本身。
    #
    # **`dropped` 是文件列表，不是目录根，不能当 `roots` 传。** 原来传了，
    # 推算出来的是 `dest\a.png` 这种"目录"，而 `build_tasks` 这边没有
    # `dropped_roots`，实际全按 `src_root`（首个文件所在的文件夹）出目录 ——
    # 校验的和真正写的是两套落点。
    # 来源目录也一样：文件列表的"来源"是这些文件各自所在的文件夹。
    source_dirs = list(dict.fromkeys(
        [src_root] + [os.path.dirname(p) for p in dropped]))
    land = output_lands_in_source(
        output_dirs_for(src_root, dst_root, output == 'new', keep_structure,
                        dir_suffix),
        source_dirs)
    if land:
        raise OptionError(land.replace(chr(10), ' '))

    cfg.dropped_paths = dropped
    cfg.excluded_paths = [os.path.abspath(p) for p in (exclude or ()) if p]

    skipped = {}

    # 扫描时有提示（比如跳过了哪些文件）就记下来，放进返回结果里。
    def note(msg):
        skipped['message'] = msg

    files = scan_files(src_root, cfg, note)
    tasks = build_tasks(files, src_root, dst_root, cfg)
    # 上面是按参数推算，这里按排好的任务再核一遍 —— 两者对不上的时候
    # （以前两次都是这么漏的）以实际要写的为准。这时一个字节都还没写。
    if output == 'new':
        land = planned_output_lands_in_source(tasks, source_dirs)
        if land:
            raise OptionError(land.replace(chr(10), ' '))
    return cfg, src_root, dst_root, files, tasks, skipped


def plan(source, *, recursive=True, output='inplace', dest=None,
         keep_structure=True, dir_suffix='_已处理', name='number', prefix='IMG',
         start=1, digits='auto', rotate='none', quality='95', compress_only=False,
         ico=False, ico_size='multi', ico_format='auto', safe_write=True,
         copy_others=True, perf_mode='balanced', reencode_animated=False,
         max_edge=0, exclude=None):
    """只算不写：返回将要扫描到的文件和每个文件的目标路径。

    改名或原地覆盖之前先调它确认一遍，尤其是自动化流程。

    ``exclude`` 是一组**不算输入**的文件路径 —— 调用方自己正在写、又放在
    来源里的文件（比如接收本次输出的报告）。``run`` 同。
    """
    cfg, src_root, dst_root, files, tasks, skipped = _prepare(
        source, recursive, output, dest, keep_structure, dir_suffix, name,
        prefix, start, digits, rotate, quality, compress_only, ico, ico_size,
        ico_format, safe_write, copy_others, perf_mode, reencode_animated,
        max_edge, exclude)

    return {
        'ok': True,
        'dry_run': True,
        'source': src_root,
        'dest': dst_root,
        'scanned': len(files),
        'skipped_note': skipped.get('message'),
        'tasks': [{'src': t.src, 'dst': t.dst, 'staged': t.stage,
                   'copy_only': t.copy_only} for t in tasks],
        'will_delete_originals': [
            # 只改大小写的不算"会删掉原件"：那是同一个文件换个写法
            t.src for t in tasks
            if output == 'inplace' and not _same_path(t.src, t.dst)],
    }


def run(source, *, recursive=True, output='inplace', dest=None,
        keep_structure=True, dir_suffix='_已处理', name='number', prefix='IMG',
        start=1, digits='auto', rotate='none', quality='95', compress_only=False,
        ico=False, ico_size='multi', ico_format='auto', safe_write=True,
        copy_others=True, perf_mode='balanced', reencode_animated=False,
        max_edge=0, workers=None, progress=None, exclude=None):
    """真正执行。返回处理结果，失败的文件逐条列出原因。

    ``progress`` 可传一个 ``callable(done, total)``，每完成一个任务调一次。
    """
    started = time.time()
    cfg, src_root, dst_root, files, tasks, skipped = _prepare(
        source, recursive, output, dest, keep_structure, dir_suffix, name,
        prefix, start, digits, rotate, quality, compress_only, ico, ico_size,
        ico_format, safe_write, copy_others, perf_mode, reencode_animated,
        max_edge, exclude)

    if not tasks:
        return {'ok': True, 'source': src_root, 'dest': dst_root, 'scanned': 0,
                'processed': 0, 'failed': [], 'elapsed_sec': 0.0,
                'skipped_note': skipped.get('message')}

    workers = workers or perf.worker_count(perf_mode)
    staged, failed = [], []
    if len(tasks) > PROCESS_THRESHOLD:
        ex = ProcessPoolExecutor(max_workers=workers,
                                 initializer=perf.init_worker,
                                 initargs=(perf.priority_of(perf_mode),))
    else:
        ex = ThreadPoolExecutor(max_workers=workers)

    with ex:
        futures = {ex.submit(process_single_image, t): t for t in tasks}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                r = fut.result()
            except Exception as exc:
                t = futures[fut]
                failed.append({'path': t.src, 'reason': f'任务异常终止: {exc}',
                               'type': 'unknown'})
                continue
            if r.success:
                staged.append(r)
            else:
                failed.append({'path': r.src_path, 'reason': r.error_msg,
                               'type': r.error_type})
            if progress:
                progress(i, len(tasks))

    # 把本批失败的源文件交给提交器保护起来 —— 它们没有产物，但文件还在，
    # 别的任务的新名字可能正好是它们。
    write_errors = commit_staged(staged, output == 'new',
                                 protect=[f['path'] for f in failed])
    for path, reason, kind in write_errors:
        failed.append({'path': path, 'reason': reason, 'type': kind})

    # 提交时被跳过或写失败的，不能再算成功产物 —— 否则返回值自相矛盾：
    # 同一个路径既出现在 failed 里，又出现在 outputs 里。
    not_committed = {os.path.normcase(os.path.abspath(p))
                     for p, _r, _k in write_errors}
    staged = [r for r in staged
              if os.path.normcase(os.path.abspath(r.src_path))
              not in not_committed]

    return {
        'ok': not failed,
        'source': src_root,
        'dest': dst_root,
        'scanned': len(files),
        'processed': len(staged),
        'failed': failed,
        'outputs': [r.dst_path for r in staged],
        # 这两项 GUI 日志里早就有，API 却检索不到 —— 而 README 把命令行明确
        # 定位成给自动化和 AI 代理用的，信息不能在两个界面之间掉。
        # kept_original: 已经是最优、原样保留的（没重新压过）
        # frames_flattened: 动画被压成单帧的（目标格式存不下多帧）
        'kept_original': [r.dst_path for r in staged
                          if getattr(r, 'kept_original', False)],
        'frames_flattened': [{'path': r.dst_path,
                              'source_frames': r.frames_dropped}
                             for r in staged
                             if getattr(r, 'frames_dropped', 0)],
        # 缩过尺寸的：调用方要能知道产物不是原尺寸了
        'resized': [r.dst_path for r in staged if getattr(r, 'resized', False)],
        'elapsed_sec': round(time.time() - started, 2),
        'skipped_note': skipped.get('message'),
    }


def clean(source, *, recursive=True, include_unknown=False, dry_run=False):
    """清掉一个文件夹里残留的临时文件。**这是用户明确要求的清理。**

    处理过程中的自动清扫只删"能确定是残骸"的；另一台电脑、另一个登录会话、
    或老版本留下的临时文件，程序判断不了主人还在不在，永远不会自动删。
    确认没有别的电脑正在用这个文件夹之后，用 `include_unknown=True` 把那些
    也清掉。主人还活着的临时文件、以及备份文件（那是原文件）无论如何不删。

    `dry_run=True` 只报告会删什么。
    """
    root = os.path.abspath(source)
    if not os.path.isdir(root):
        raise OptionError(f'source 不是文件夹: {source!r}')
    out = clean_leftovers(root, recursive=recursive,
                          include_unknown=include_unknown, dry_run=dry_run)
    out.update(ok=True, source=root, dry_run=bool(dry_run))
    return out


def capabilities():
    """告诉调用方本工具支持什么 —— AI 代理可以先问这个再决定怎么调。"""
    return {
        'extensions': list(VALID_EXTENSIONS),
        'rotate': sorted(ROTATE),
        'quality': sorted(QUALITY),
        'name': list(NAME_MODES),
        'output': list(OUTPUTS),
        'ico_size': ['multi'] + [f'fixed_{s}' for s in (256, 128, 64, 48, 32, 16)]
                    + [f'auto_{s}' for s in (256, 128)],
        'ico_format': sorted(ICO_FORMAT),
        'perf': sorted(perf.MODES),
        'digits': [1, 2, 3, 4, 5, 'auto'],
        'notes': [
            '多帧图像（动态 WebP/GIF、多页 TIFF）在 rotate="none" 时原样复制，'
            '帧与帧延时全保留；要按画质重编码得显式传 reencode_animated=True'
            '（慢得多，且有损源再编一遍往往比原文件还大）',
            'rotate 不是 "none" 时动图一律逐帧处理并保留动画',
            'max_edge=N 时最长边超过 N 的图会等比缩小（只缩不放，小图不动）；'
            '实测 4000x3000 限到 1920 体积只剩 0.14 倍，是省得最多的一招',
            '转 ICO 时尺寸上限 256，这是 ICO 格式本身的限制',
            'output="inplace" 会删除改名前的原文件，执行前建议先调 plan()',
            'safe_write=True 时全部完成才落盘，中途失败可完整撤回',
            "digits='auto' 按这一批的文件总数定宽再多留一位"
            "（1234 个文件 -> 5 位，00001~01234）",
            'copy_others=True 时不支持的格式原样复制并参与编号，输出即源目录的完整镜像',
            "perf='eco' 会把子进程降到后台优先级（含磁盘 I/O），大批量时电脑仍可正常用",
        ],
    }


__all__ = ['plan', 'run', 'capabilities', 'OptionError',
           'ROTATE', 'QUALITY', 'ICO_FORMAT', 'NAME_MODES', 'OUTPUTS']
