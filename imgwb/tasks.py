"""扫描文件、编排输出名，以及暂存文件的提交与撤回。"""

# ============================================================
# 【导读】tasks.py —— 处理前后的「文件编排」。
# 处理前：scan_files 列出文件 → build_tasks 定好每个文件的输出路径和新名字。
# 处理后：commit_staged 把临时文件变成正式文件；discard_staged / sweep_temps 撤回和清理。
# 这里跟「会不会弄丢原图」直接相关，改之前先读 sweep_temps 和 commit_staged 的说明。
# ============================================================
import os
import re
import uuid
from collections import defaultdict
from typing import List

from .config import (IS_WINDOWS, MAX_EDGE_CHOICES, VALID_EXTENSIONS,
                     ProcessConfig, Task)
from .utils import (INSTANCE_TAG, SCOPE_TAG, discard_temp, instance_alive,
                    natural_sort_key, parse_digit_count, path_escapes,
                    path_inside)


def _is_hidden(directory, name):
    """这个文件/目录是不是真的隐藏。

    Windows 看 NTFS 的 FILE_ATTRIBUTE_HIDDEN，不看文件名 —— `.jpg`、`..jpg`
    在从相机或聊天工具导出时并不罕见，它们是合法文件名，也是正经图片。
    其它平台才沿用"点开头"的惯例。
    """
    if not IS_WINDOWS:
        return name.startswith('.')
    try:
        attrs = os.stat(os.path.join(directory, name)).st_file_attributes
        return bool(attrs & 2)          # FILE_ATTRIBUTE_HIDDEN
    except (OSError, AttributeError):
        return False


def output_escape_error(src_root, dst_root, is_export):
    """导出目录落在扫描范围里就返回一句错误说明，否则返回空串。

    把「另存为新位置」指向源文件夹自己（或它的子目录），产物就落在扫描范围
    之内 —— 下一次运行会把上一批产物当成新的源图重新扫进来，**每跑一次
    文件翻一倍**，目录还一层层套自己：

        第 1 次  2 -> 4        src\src_已处理\
        第 2 次  4 -> 8        src\src_已处理\src_已处理\
        第 3 次  8 -> 16       ...
        第 4 次  16 -> 32

    严格指数增长，跑十次就是 1024 倍，而程序每次都报成功。对一个动辄几千张
    照片的批处理工具来说，这是能把磁盘写满的量级。
    命令行和 api.run() 的 dest 更容易踩到 —— 脚本里顺手就把 dest 写成源目录了。
    """
    if not is_export:
        return ''
    # 走 `realpath`，和 `is_protected` / `output_lands_in_source` 一致：
    # junction 和 symlink 能让两条看起来毫不相干的路径指向同一个地方，
    # 只比字符串的话"导出位置是一个指回来源的 junction"就漏过去了。
    # 不存在的尾部 `realpath` 会原样保留，所以还没建出来的导出目录也能比。
    try:
        src, dst = protected_paths([src_root, dst_root])
    except Exception:
        return ''
    if dst == src:
        return ('导出位置不能就是来源文件夹本身 —— 产物会落回扫描范围，'
                '再跑一次就会把它们当成新图重新处理，文件每跑一次翻一倍。')
    if path_inside(dst, src):
        return ('导出位置在来源文件夹**里面** —— 产物会落回扫描范围，'
                '再跑一次就会把它们当成新图重新处理，文件每跑一次翻一倍。'
                f'{chr(10)}来源: {src_root}{chr(10)}导出: {dst_root}')
    return ''


def _file_key(path, dir_cache=None):
    """同一路径的大小写、相对段和链接别名共用一个扫描身份；保留首次输入拼写。

    **真实路径按目录解析，不按文件。** `realpath` 在 Windows 上每次要好几个
    系统调用，逐文件解析的话 3000 个文件要 325 毫秒（只做 `abspath` 是
    5 毫秒），网络盘上更慢 —— 而同一个文件夹里的文件，目录那一段是一样的。
    所以目录解析一次、记进 `dir_cache`，文件名只做大小写归一。

    代价：**单个文件自身是符号链接**（指向扫描范围里另一个文件）时认不出
    是同一个。目录级的别名 —— 大小写、`..`、junction、目录符号链接 ——
    照常认得，父子来源重叠和 API 重复路径靠的都是这一层。
    """
    directory, name = os.path.split(path)
    real_dir = dir_cache.get(directory) if dir_cache is not None else None
    if real_dir is None:
        real_dir = os.path.normcase(os.path.realpath(directory or '.'))
        if dir_cache is not None:
            dir_cache[directory] = real_dir
    return os.path.join(real_dir, os.path.normcase(name))


def _is_own_artifact(name):
    """这个文件名是不是本程序自己写出来的临时文件或备份文件。"""
    return BACKUP_MARK in name or temp_owner(name) is not None


def scan_files(src_root: str, cfg, log=None) -> List[str]:
    """扫描文件，按目录分组后自然排序。cfg 是 UI 线程取好的设置快照。

    开了「不支持的文件也带上」时，非图片文件一并返回，后续按原样复制过去 ——
    这样输出目录才是源目录的完整镜像，编号也不会因为跳过而错位。
    """
    real_dirs = {}                      # 目录 -> 真实路径，见 _file_key
    own = [0]                           # 跳过了几个本程序自己的临时/备份文件
    # **这次运行自己正在写的文件不是输入**（命令行的 `--cli-out` 报告）。
    # 报告在扫描之前就建好了；它要是放在来源文件夹里，开着"带上不支持的
    # 文件"时会被当成附件扫进来 —— 原地编号去改一个自己还开着的文件
    # （WinError 32，照片已经改完名，命令却报失败），导出则多拷出一个
    # 0 字节的报告、计数虚增。按**路径**排除这一个文件，
    # 不按扩展名：用户自己的 JSON 附件照常带走。
    reserved = {_file_key(p, real_dirs)
                for p in (getattr(cfg, 'excluded_paths', None) or ())}
    if cfg.dropped_paths:
        unique = {}
        for path in cfg.dropped_paths:
            if _is_own_artifact(os.path.basename(path)):
                own[0] += 1
                continue
            key = _file_key(path, real_dirs)
            if key in reserved:
                continue
            unique.setdefault(key, path)
        if log and own[0]:
            log(f'ℹ️ 已跳过 {own[0]} 个本程序自己的临时文件或备份文件')
        return sorted(unique.values(), key=natural_sort_key)

    carry_others = getattr(cfg, 'copy_others', None)
    carry_others = carry_others.get() if carry_others is not None else False

    dir_files = defaultdict(list)
    skipped = defaultdict(int)
    carried = defaultdict(int)
    hidden = [0]
    seen = set()

    # 扫到一个文件时决定：收进待处理列表、跟着带走、还是跳过（隐藏文件跳过，但会计数报告）。
    def take(directory, filename):
        full = os.path.join(directory, filename)
        key = _file_key(full, real_dirs)
        if key in seen or key in reserved:
            return
        seen.add(key)
        # **本程序自己的临时文件和备份文件不是输入。** 临时文件的名字以原扩展名
        # 结尾（`001.~imgwb….jpg`），不拦的话会被当成一张普通图片扫进来：
        # 别的窗口正在这个文件夹里原地处理时，它等着提交的产物会被这边
        # 再处理、改名、删掉；上次中断留下的备份（那是原文件）会被当成
        # "不支持的文件"跟着重新编号。两种都不该碰，跳过并计数。
        if _is_own_artifact(filename):
            own[0] += 1
            return
        # 「点开头 = 隐藏文件」是 Unix 惯例，Windows 靠的是 NTFS 隐藏属性，
        # 跟文件名没关系 —— 而本程序明确面向 Windows。原来这一条直接 return，
        # 不计数也不记日志：磁盘上 62 个文件、plan() 只看到 58 个，
        # 少掉的 .jpg / ..jpg / ...jpg 都是能正常打开的真图片，
        # 而函数自己的文档还写着"输出目录是源目录的完整镜像"。
        # 现在只认真正的隐藏属性，而且**一定计入跳过清单**，不再凭空消失。
        if _is_hidden(directory, filename):
            hidden[0] += 1
            return
        ext = os.path.splitext(filename)[1].lower() or '(无扩展名)'
        if filename.lower().endswith(VALID_EXTENSIONS):
            dir_files[directory].append(full)
        elif carry_others:
            dir_files[directory].append(full)
            carried[ext] += 1
        else:
            skipped[ext] += 1

    # 源可以是**多条路径**：地址栏里用分号隔开，或者一次拖进来好几个文件夹。
    # 每条各自成根，输出时各得一个「文件夹名_已处理」（见 build_tasks 的 root_of）。
    roots = [r for r in (getattr(cfg, 'dropped_roots', None) or [])
             if os.path.isdir(r)] or [src_root]
    for one in roots:
        if cfg.scan_subdirs.get():
            for root, dirs, files in os.walk(one):
                dirs[:] = sorted([d for d in dirs if not _is_hidden(root, d)])
                for f in files:
                    take(root, f)
        else:
            for f in os.listdir(one):
                if os.path.isfile(os.path.join(one, f)):
                    take(one, f)

    if log:
        if own[0]:
            log(f'ℹ️ 已跳过 {own[0]} 个本程序自己的临时文件或备份文件')
        if hidden[0]:
            log(f'ℹ️ 已跳过 {hidden[0]} 个隐藏文件')
        if carried:
            detail = '、'.join(f'{ext} × {n}' for ext, n in sorted(carried.items()))
            log(f'📄 {sum(carried.values())} 个不支持的文件将按原样复制并一起编号: {detail}')
        if skipped:
            detail = '、'.join(f'{ext} × {n}' for ext, n in sorted(skipped.items()))
            log(f'ℹ️ 已跳过 {sum(skipped.values())} 个不支持的文件: {detail}')

    all_files = []
    for dir_path in sorted(dir_files.keys(), key=natural_sort_key):
        sorted_files = sorted(dir_files[dir_path], key=natural_sort_key)
        all_files.extend(sorted_files)

    return all_files

# 【文件名编号就在这里定】给每个文件算出输出路径和新名字，
# 生成一张「任务清单」交给后面去处理。
def build_tasks(all_files: List[str], src_root: str, dst_root: str,
                cfg) -> List[tuple]:
    tasks = []
    start = cfg.start_num.get()
    padding = parse_digit_count(cfg.digit_val.get())
    if padding is None:
        # 智能位数：先看这一批的最大编号要几位，再多留一位。
        # 1234 个文件从 1 开始 -> 最大 1234 是 4 位 -> 取 5 位 -> 00001。
        # 多留的那一位是给后续往同一目录追加文件用的，不然一超过 9999
        # 就会出现 9999 和 10000 混排、排序全乱。
        # 但要封顶 6 位：起始编号很大时（start=99999、3 个文件）最大编号本身
        # 就 6 位，再加一位就成了 0099999.jpg —— 两个前导零纯属碍眼。
        padding = min(len(str(start + max(len(all_files), 1) - 1)) + 1, 6)
    prefix = cfg.prefix_val.get() or 'IMG'
    name_mode = cfg.name_mode.get()
    is_export = cfg.out_mode.get() == 'new'
    keep_struct = cfg.keep_structure.get()
    suffix = cfg.dir_suffix_val.get()


    ico_sizes = [cfg.ico_mode_val.get()]
    ico_format = cfg.ico_format_val.get()

    # getattr 兜底：api/cli 造的 cfg 不一定带这些较晚加的开关
    reencode_anim = getattr(cfg, 'reencode_anim', None)
    max_edge_var = getattr(cfg, 'max_edge_val', None)
    max_edge = 0
    if max_edge_var is not None:
        raw = max_edge_var.get()
        max_edge = raw if isinstance(raw, int) else dict(MAX_EDGE_CHOICES).get(raw, 0)

    config = ProcessConfig(
        rotate_mode=cfg.rotate_val.get(),
        quality_opt=cfg.quality_val.get(),
        compress_only=cfg.compress_only.get(),
        is_ico=cfg.is_ico.get(),
        ico_sizes=ico_sizes,
        ico_format=ico_format,
        reencode_animated=bool(reencode_anim.get()) if reencode_anim else False,
        max_edge=max_edge
    )

    used_names = {}
    dir_counters = {}

    safe_write = cfg.safe_write.get()
    source_keys = {os.path.normcase(os.path.abspath(p)) for p in all_files}

    # **"同一个目录"要按目录的真实身份认，不能按字符串。**
    # 已用名字表、编号计数、本批可复用的名字，全是"每个目录一份"。原来拿
    # `final_dir` 这个字符串当键：从 `one\photos` 和 `two\PHOTOS` 两个来源导出，
    # 得到 `out\photos_done` 和 `out\PHOTOS_done` —— 字符串不同，在 Windows 上
    # 却是**同一个文件夹**。于是各建了一份名字表，两边都分到 `a.png`，
    # 提交时后一个把前一个盖掉：界面报"完成 2 张"，实际只有 1 张
    # （安全写入开着也一样；编号模式同样会撞）。
    # 真实路径按目录缓存，一批任务里目录没几个，不会拖慢编排。
    _dir_ids = {}

    def dir_id(directory):
        key = _dir_ids.get(directory)
        if key is None:
            key = _dir_ids[directory] = os.path.normcase(
                os.path.realpath(directory))
        return key

    batch_names = defaultdict(set)
    for p in all_files:
        batch_names[dir_id(os.path.dirname(p))].add(os.path.basename(p).lower())
    claimed = {}                        # 目标的真实身份 -> 分到它的源文件

    # 拖进来的每个文件夹都是一条独立的源路径。按最长前缀把文件归到它自己的
    # 根上，这样两个文件夹各自保留内部结构、各得一个「文件夹名_已处理」，
    # 而不是全都挤进第一个文件夹的名下。
    roots = sorted(
        (os.path.abspath(r) for r in (getattr(cfg, 'dropped_roots', None) or [])),
        key=len, reverse=True)

    # 有多个来源文件夹时，判断这个文件属于哪一个，
    # 好让它输出到对应的「文件夹名_已处理」下面。
    def root_of(path):
        target = os.path.normcase(os.path.abspath(path))
        for r in roots:
            probe = os.path.normcase(r)
            if target == probe or path_inside(target, probe):
                return r
        return src_root

    for src_path in all_files:

        if is_export and keep_struct:
            this_root = root_of(src_path)
            # 归不到任何根的文件（跨盘、或路径对不上）相对路径会算出 ..\..\ 前缀，
            # join 之后能跳出导出目录甚至写回源文件夹；跨盘则直接抛 ValueError。
            # 这两种情况一律退回平铺。
            try:
                rel_dir = os.path.dirname(os.path.relpath(src_path, this_root))
            except ValueError:
                rel_dir = ''
            if rel_dir.startswith('..') or os.path.isabs(rel_dir):
                rel_dir = ''

            src_folder_name = os.path.basename(this_root)
            output_folder = src_folder_name + suffix

            if rel_dir:
                final_dir = os.path.join(dst_root, output_folder, rel_dir)
            else:
                final_dir = os.path.join(dst_root, output_folder)
        elif is_export:
            final_dir = dst_root
        else:
            final_dir = os.path.dirname(src_path)


        space = dir_id(final_dir)       # 这个目录的名字表按真实身份共用
        if space not in used_names:
            try:
                existing = {n.lower() for n in os.listdir(final_dir)}
            except OSError:
                existing = set()
            # 原地修改时，本批文件的旧名字马上就会被替换掉，不算占用 ——
            # 否则重复运行同一流程时编号会一直往后漂。导出时源文件仍然存在，
            # 一律视为占用，免得写坏目标目录里已有的文件。
            reusable = batch_names[space] if not is_export else set()
            used_names[space] = existing - reusable
        if space not in dir_counters:
            dir_counters[space] = start


        src_ext = os.path.splitext(src_path)[1]
        # 不支持的格式只做复制改名：不解码、不重编码，扩展名和文件属性都原样保留
        copy_only = not src_path.lower().endswith(VALID_EXTENSIONS)

        if copy_only:
            ext = src_ext
        elif config.is_ico:
            ext = '.ico'
        elif src_ext.lower() in ('.heic', '.heif'):
            ext = '.jpg'
        else:
            ext = src_ext

        if name_mode == 'keep':
            base_name = os.path.splitext(os.path.basename(src_path))[0]
            new_name = base_name + ext
            counter = 1
            while new_name.lower() in used_names[space]:
                new_name = f'{base_name}_{counter}{ext}'
                counter += 1
            used_names[space].add(new_name.lower())

        else:
            curr = dir_counters[space]
            while True:
                num_str = str(curr).zfill(padding)
                new_name = f'{prefix}_{num_str}{ext}' if name_mode == 'prefix' else f'{num_str}{ext}'

                if new_name.lower() not in used_names[space]:
                    used_names[space].add(new_name.lower())
                    break
                curr += 1
            dir_counters[space] = curr + 1

        dst_path = os.path.join(final_dir, new_name)

        # **一批任务里不许有两个任务写同一个文件。** 上面的名字表本来就该
        # 保证这一点；这里再按最终结果核一遍，防的是"还有别的别名没想到"。
        # 安全写入保证的是每一次替换不出半成品，**不保证批内目标互不相同** ——
        # 两个都成功的替换照样会一个盖掉另一个，而且谁也不报错。
        # 真撞上就整批停下：宁可不跑，也不要悄悄少一张。
        slot = os.path.join(space, new_name.lower())
        if slot in claimed:
            raise ValueError(
                f'两个文件被排到了同一个输出位置，已中止以免互相覆盖：'
                f'{claimed[slot]} 和 {src_path} 都要写到 {dst_path}')
        claimed[slot] = src_path

        # **最后一道兜底：产物必须落在它该落的目录里。**
        # 前缀和目录后缀在入口处已经校验过了（api._prepare、
        # start_processing 的检查 3.5），这里防的是"还有别的路没想到"。
        # 真触发说明有一条路绕过了校验 —— 那就宁可整批停下报错，
        # 也不要往一个不该写的地方写文件。
        if path_escapes(dst_path, final_dir):
            raise ValueError(
                f'产物路径逃出了它该在的目录，已中止以免覆盖别处的文件：'
                f'{dst_path}（应在 {final_dir} 下）')

        # 直写模式下也不能直接写到别的待处理文件头上 —— 并发时那张图可能
        # 还没被读取。只有这种会撞车的才退回暂存，其余仍然直写省磁盘。
        dst_key = os.path.normcase(os.path.abspath(dst_path))
        src_key = os.path.normcase(os.path.abspath(src_path))
        stage = safe_write or (dst_key in source_keys and dst_key != src_key)

        tasks.append(Task(src_path, dst_path, config, stage, copy_only))

    return tasks

# 备份文件的名字。**刻意不符合 TEMP_PATTERN** —— 否则 `sweep_temps` 会把它
# 当残留临时文件删掉，而实测复现过"原文件删了、恢复用的副本又被清扫掉"的
# 双重丢失。改这个名字前先回头看一眼 TEMP_PATTERN。
BACKUP_MARK = '.imgwb-backup-'


def _backup_path(src):
    return f'{src}{BACKUP_MARK}{uuid.uuid4().hex[:8]}'


def _same_path(a, b):
    """两个路径是不是同一条（只差大小写、`.`/`..` 写法的也算）。

    和 `build_tasks` 判断"要不要暂存"用的是同一个口径（规范化路径相等）——
    **编排和提交必须用同一个口径**，原来一边按规范化路径、一边按字符串，
    中间那条缝真的丢过文件。故意不认硬链接之类的别名：那种情况下
    源和目标在目录里是两个条目，照"两个文件"处理才对。
    """
    return (os.path.normcase(os.path.abspath(a))
            == os.path.normcase(os.path.abspath(b)))


def _restore_backup(src, bak):
    """把让位时改名出去的备份放回原位。返回原文件**现在在哪**：
    原位、备份位（放不回去，留着）、或 None（两处都找不到）。

    提交器里有两处要做这件事（改名失败的收尾、晚期拒绝），共用这一份 ——
    原来各写各的，其中一处把失败吞掉了，用户就不知道文件在备份名下。
    """
    if not os.path.exists(src):
        try:
            os.replace(bak, src)
            return src
        except OSError:
            pass
    return bak if os.path.exists(bak) else None


def _reject_colliding(items, guard_srcs, src_of, dst_of):
    """把"目标撞在受保护原文件上"的项挑出来，**反复传播到不再增加为止**。

    为什么必须反复传播：改名是**链式**的。`1→2`、`2→3`、`3→4`，当 3 处理失败
    时我们拒绝 `2→3`（对，否则会盖掉 3）；可这样一来 2 也成了"没有产物的原
    文件"，`1→2` 还是会把一张完好的 2.jpg 盖掉。**只拦一层等于没拦住** ——
    实测就是这么复现的：2.jpg 的哈希变了，而返回值里它既是失败来源、
    又是成功产物。上一轮只验了两个文件的情形，正好测不到这一层。

    所以每拒绝一批，就把它们的源文件也加进保护集合，再筛一遍，直到没有新增。
    每轮 `kept` 必定变小，所以一定会停。

    返回 (还能提交的, 被拒绝的)。
    """
    kept, rejected = list(items), []
    while True:
        guard = protected_paths(list(guard_srcs) + [src_of(r) for r in rejected])
        if not guard:
            break
        hit = [r for r in kept
               if dst_of(r) != src_of(r) and is_protected(dst_of(r), guard)]
        if not hit:
            break
        ids = {id(r) for r in hit}
        rejected.extend(hit)
        kept = [r for r in kept if id(r) not in ids]
    return kept, rejected


def commit_staged(staged, is_export, protect=()):
    """把临时文件转正。返回写入失败的记录 `[(源路径, 原因, 类型)]`。

    **这是整个程序最危险的一段** —— 它要删掉、覆盖用户的原文件。
    这里实测复现过两种真实的数据丢失，所以现在是这么做的：

    1. **不覆盖没提交成功的原文件**。`protect` 装的是本批处理失败、
       没有产物的源文件路径。原来这个函数只拿到成功的结果，不知道哪些源文件
       还活着：`1.jpg → 2.jpg` 成功、`2.jpg → 3.jpg` 失败时，前者会 os.replace
       到 2.jpg 头上，把用户那张（坏，但属于他自己）的文件盖掉 ——
       返回值里同一个路径既算失败来源、又算成功产物。
    2. **让位用的源文件是改名成备份，不是删掉**。原来是"先把所有
       源文件删一遍，再逐个 os.replace"，而 os.replace 是真的会失败的
       （复现里是 WinError 5）—— 一失败，原文件已经没了、产物也没落位。
       现在：全部成功才删备份；有失败就把备份改回原位，改不回去就留着，
       并在错误信息里给出完整路径。

    为什么要"先腾位、再统一改名"：某个文件的新名字可能正好是另一个文件的
    旧名字（整批重新编号时很常见）。两件事交替做会互相踩。

    `is_export` 为真时产物写到别的目录，源文件根本不用动，也就没有备份这一步。
    """
    errors = []

    # ── 第 0 步：临时文件都还在吗 ──
    # 临时文件要是已经不见了（比如被中途的清扫误删），还去动源文件的话，
    # 原图和产物会一起没。宁可这一项不提交。
    missing = [r for r in staged
               if r.tmp_path and not os.path.exists(r.tmp_path)]
    for r in missing:
        errors.append((r.src_path,
                       f'临时文件已不存在，放弃提交以免弄丢原文件: '
                       f'{os.path.basename(r.tmp_path)}', 'io'))
    if missing:
        gone = {id(r) for r in missing}
        staged = [r for r in staged if id(r) not in gone]

    # 这些源文件已经没有产物了 —— 和调用方报来的失败一样要保护起来，
    # 否则上一句"放弃提交以免弄丢原文件"就是空话：别的任务照样能盖上去。
    base_guard = list(protect) + [r.src_path for r in missing]

    # ── 第 1 步：目标撞在受保护的原文件上的，**一路传播着**拒绝掉 ──
    ready, rejected = _reject_colliding(
        staged, base_guard, lambda r: r.src_path, lambda r: r.dst_path)
    for r in rejected:
        errors.append((
            r.src_path,
            f'目标 {os.path.basename(r.dst_path)} 是另一张没能提交的'
            f'原文件，已跳过以免覆盖它', 'io'))
        discard_temp(r.tmp_path)

    # ── 第 2 步：把要让位的源文件改名成备份 ──
    plan = []                           # [(结果, 备份路径或 None)]
    stuck = []                          # 腾不开位、源文件还留在原地的
    if is_export:
        plan = [(r, None) for r in ready]
    else:
        for r in ready:
            if r.src_path == r.dst_path:
                plan.append((r, None))  # 名字没变，原地覆盖，不用腾位
                continue
            if not r.tmp_path and _same_path(r.src_path, r.dst_path):
                # **直写 + 只改大小写：产物已经就在这个文件里了，它是唯一的
                # 一份。** Windows 目录不分大小写，`img_001.jpg` 和
                # `IMG_001.jpg` 是同一个文件。原来这里只比字符串，把它当成
                # "要让位的旧文件"挪成备份；第 3 步又因为没有暂存文件而什么都
                # 不落位；第 4 步按"成功"把备份删掉 —— 目录变空，而结果报的
                # 是成功。这一项不腾位，第 3 步只改个名字。
                plan.append((r, None))
                continue
            bak = _backup_path(r.src_path)
            try:
                os.replace(r.src_path, bak)
            except OSError as exc:
                # 腾不开就别提交这一项 —— 原文件还在原处，没有损失
                errors.append((r.src_path,
                               f'原文件挪不开，已跳过不提交: {exc}', 'io'))
                discard_temp(r.tmp_path)
                stuck.append(r)
                continue
            plan.append((r, bak))

    # **腾不开位的那几项，源文件还在原地、又没有产物** —— 跟第 1 步里被拒绝的
    # 是同一类，同样不能被别人盖掉。所以这里要再传播一轮；被这一轮拒掉的项，
    # 它的备份要改回原位（等于这一项从没动过）。
    if stuck:
        plan, late = _reject_colliding(
            plan,
            base_guard + [r.src_path for r in rejected]
            + [r.src_path for r in stuck],
            lambda rb: rb[0].src_path, lambda rb: rb[0].dst_path)
        for r, bak in late:
            # 原样放回去。**放不回去就必须说出备份在哪** —— 原来这里
            # `except OSError: pass`，原位已经空了、字节躺在备份名下，
            # 错误消息却只有一句"已跳过"。文件没丢，
            # 但用户不知道去哪找，跟丢了差不多。
            where = _restore_backup(r.src_path, bak) if bak else r.src_path
            discard_temp(r.tmp_path)
            msg = (f'目标 {os.path.basename(r.dst_path)} 是另一张没能提交的'
                   f'原文件，已跳过以免覆盖它')
            if where != r.src_path:
                msg += (f'；原文件保留在 {where}' if where
                        else '；原文件没能放回原位')
            errors.append((r.src_path, msg, 'io'))

    # ── 第 3 步：逐个把临时文件改名到位 ──
    broken = []
    for r, bak in plan:
        if not r.tmp_path:
            # 直写模式：产物早就在目标位置了。唯一还差的是"只改大小写"那种
            # —— 文件内容已经是新的，名字还是旧的大小写，这里改过来。
            if (not is_export and r.src_path != r.dst_path
                    and _same_path(r.src_path, r.dst_path)):
                try:
                    os.replace(r.src_path, r.dst_path)
                except OSError as exc:
                    errors.append((
                        r.src_path,
                        f'文件已处理好，但没能改成新名字 '
                        f'{os.path.basename(r.dst_path)}: {exc}', 'io'))
            continue
        try:
            os.replace(r.tmp_path, r.dst_path)
        except OSError as exc:
            broken.append((r, bak, exc))

    # ── 第 4 步：收尾。成功的删备份，失败的尽量还原 ──
    bad = {id(r) for r, _b, _e in broken}
    for r, bak in plan:
        if bak and id(r) not in bad:
            try:
                os.remove(bak)
            except OSError:
                pass                    # 删不掉只是留个备份文件，不影响正确性

    for r, bak, exc in broken:
        where = _restore_backup(r.src_path, bak) if bak else None
        if where == r.src_path:
            errors.append((r.src_path, f'写入失败: {exc}（原文件已还原）', 'io'))
        elif where:
            errors.append((r.src_path,
                           f'写入失败: {exc}；原文件保留在 {where}', 'io'))
        else:
            errors.append((r.src_path, f'写入失败: {exc}', 'io'))
        discard_temp(r.tmp_path)

    return errors


# make_temp_path 造出来的名字长这样：001.~imgwb + 6 位实例标记 + 8 位随机数 + .jpg
# 临时文件的命名规律，清扫残留时按它认。**`imgwb` 这个标记不能去掉**：
# 原来是「.~ + 8 位十六进制」，于是用户自己名为 `photo.~1a2b3c4d.jpg`、
# `backup.~deadbeef.png` 的文件会被当成垃圾删掉 —— 实测一扫就没。
# 文件名是唯一的判据，所以判据必须足够独特。
# 十六进制那一段有三种长度：18 位是现在的（6 位实例标记 + 4 位范围标记 +
# 8 位随机数）；8 位和 14 位是老版本的，照样认得出是临时文件，但看不出主人。
TEMP_PATTERN = re.compile(
    r'\.~imgwb([0-9a-f]{18}|[0-9a-f]{14}|[0-9a-f]{8})(\.[^.]*)?$')


def temp_owner(name):
    """从临时文件名里读出它的主人：`(实例标记, 范围标记)`。

    不是临时文件返回 None；是临时文件但看不出主人（老版本的名字）返回
    `('', '')` —— 调用方必须把它当成"别人的、而且不知道是死是活"。
    """
    found = TEMP_PATTERN.search(name)
    if not found:
        return None
    digits = found.group(1)
    if len(digits) == 18:
        return digits[:6], digits[6:10]
    return '', ''


def sweep_temps(dirs, exclude=None):
    """扫掉目标目录里残留的临时文件，返回清理了几个。

    工作进程被强制终止时（用户直接关窗口），它正在写的那个临时文件不会走
    正常的撤回流程 —— 父进程压根不知道那个随机文件名。只能按命名规律清。
    只扫本次任务实际要写入的目录，且只删符合 `.~` + 8 位十六进制的名字。

    **`exclude` 不是可选的讲究，是必需的。** 安全写入模式下，每一张处理好的
    产物都以临时文件躺在同一个目录里等最后统一提交 —— 它们跟"被强杀的工人
    留下的半截文件"长得一模一样。中途扫一次而不排除它们，等于把整批已完成的
    成果全删掉：用户实测一次 2831 张的批量，因为有 1 张超时触发了中途清扫，
    最后 `commit_staged` 报出 **2560 个失败**。
    原地修改模式下后果更重 —— 那边是先删源文件再改名，临时文件没了就等于
    原图和产物一起没了。

    **别的实例的临时文件，只有主人确定不在了才删。** `exclude` 只管得了本实例
    知道的那些；用户同时开着另一个窗口、导出到同一个文件夹时，那边等着提交
    的临时文件这边根本不知道名字。所以临时文件名里带着主人的标记
    （`temp_owner`）：是自己的就删；是别人的，要 `utils.instance_alive` 明确说
    "那个实例已经不在了"才删 —— 那才是崩溃留下的残骸。

    **绝不能拿文件的时间来猜。** 上一版就是这么错的：别人的临时文件"修改
    时间超过 24 小时"就当垃圾删。可本程序会把照片原来的修改时间保留到产物上
    （`copy2`、`os.utime`）—— 处理一张两天前拍的照片，临时文件**一生成**
    修改时间就是两天前。于是另一个窗口一清扫，这边刚写好、正等着提交的
    产物全被删掉（两个真实进程，老照片 10/10，从启动到出事
    0.16 秒）。文件上的任何时间都不是"它当了多久临时文件"，更证明不了主人
    死没死；能证明的只有主人自己手里那个凭据。看不出主人的（老版本的名字）
    一律不删。
    """
    keep = {os.path.normcase(os.path.abspath(p)) for p in (exclude or ()) if p}
    removed = 0
    alive = {}                          # (实例, 范围) -> 主人还在不在，一次清扫里只查一遍
    for directory in {d for d in dirs if d}:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            owner = temp_owner(name)
            if owner is None:
                continue
            full = os.path.join(directory, name)
            if os.path.normcase(os.path.abspath(full)) in keep:
                continue
            if owner != (INSTANCE_TAG, SCOPE_TAG):
                tag, scope = owner
                if not tag:
                    continue            # 看不出主人是谁，不碰
                if owner not in alive:
                    alive[owner] = instance_alive(scope, tag)
                if alive[owner]:
                    continue            # 主人还在（或者说不准）—— 可能正等着提交
            try:
                os.remove(full)
                removed += 1
            except OSError:
                pass
    return removed


def find_leftovers(dirs):
    """这些目录里有没有**不是本实例的**残留。只看，不删。

    返回 `{'unknown': [...], 'live': [...], 'backups': [...]}`，都是完整路径：

    * `unknown` —— 临时文件，但判断不了主人的死活：另一台电脑或另一个登录
      会话上的实例写的，或者老版本留下的。`sweep_temps` 永远不会自动删它们
      （宁可留垃圾也不误删），所以要有人告诉用户它们在那儿。
    * `live` —— 临时文件，主人此刻还活着（比如同时开着的另一个窗口）。
    * `backups` —— `.imgwb-backup-` 文件。**这是原文件，不是垃圾**：提交到
      一半被打断（断电、被强杀）时，让位的原文件就留在这个名字下。

    主人已经不在的临时文件不在这里 —— 那种 `sweep_temps` 会直接清掉。
    """
    out = {'unknown': [], 'live': [], 'backups': []}
    alive = {}
    for directory in {d for d in dirs if d}:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            full = os.path.join(directory, name)
            if BACKUP_MARK in name:
                out['backups'].append(full)
                continue
            owner = temp_owner(name)
            if owner is None:
                continue
            standing = _temp_standing(owner, alive)
            if standing in ('unknown', 'live'):
                out[standing].append(full)
    for key in out:
        out[key].sort()
    return out


def _temp_standing(owner, alive):
    """一个临时文件的主人是什么情况：`'mine'`（本实例）、`'dead'`（确定已经
    不在了）、`'live'`（还活着）、`'unknown'`（判断不了死活）。

    `owner` 是 `temp_owner` 的返回值；`alive` 是调用方那一轮的查询缓存。
    """
    if owner == (INSTANCE_TAG, SCOPE_TAG):
        return 'mine'
    tag, scope = owner
    if not tag or scope != SCOPE_TAG:
        return 'unknown'
    if owner not in alive:
        alive[owner] = instance_alive(scope, tag)
    return 'live' if alive[owner] else 'dead'


def clean_leftovers(root, recursive=True, include_unknown=False, dry_run=False):
    """用户**明确要求**的清理：把一个文件夹里的残留临时文件清掉。

    和 `sweep_temps` 的区别是有人拍板。自动清扫不敢动"判断不了主人死活"的
    临时文件 —— 它不知道另一台电脑是不是正往这个共享文件夹里写。用户知道。
    所以 `include_unknown=True` 时连那一类也删；**主人还活着的永远不删，
    备份文件永远不删**（那是原文件，要用户自己看过再处理）。

    返回 `{'removed': [...], 'kept_live': [...], 'kept_unknown': [...],
    'backups': [...]}`。`dry_run=True` 时只报告、不动手。

    **能不能删，对着眼前这个文件的主人现查，不查名单。** 原来是先调
    `find_leftovers` 拿一份"活着的 / 来历不明的"路径名单，再重新列一遍目录，
    不在名单上的就删 —— 两次列目录之间，另一个窗口刚写好、正等着提交的产物
    不在那份旧名单上，主人明明活着也被删了（两个真实进程 10/10）。
    `find_leftovers` 的结果现在只用来报备份文件，不当删除的依据。
    """
    dirs = [root]
    if recursive:
        for base, subdirs, _files in os.walk(root):
            dirs.extend(os.path.join(base, d) for d in subdirs)
    found = find_leftovers(dirs)
    removed, kept = [], {'live': [], 'unknown': []}
    alive = {}
    for directory in dirs:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if BACKUP_MARK in name:
                continue
            owner = temp_owner(name)
            if owner is None:
                continue
            full = os.path.join(directory, name)
            standing = _temp_standing(owner, alive)
            # 自己的、和主人已经不在的：sweep_temps 的规则本来就会删
            if standing == 'live' or (standing == 'unknown'
                                      and not include_unknown):
                kept[standing].append(full)
                continue
            if not dry_run:
                try:
                    os.remove(full)
                except OSError:
                    continue
            removed.append(full)
    return {'removed': sorted(removed),
            'kept_live': sorted(kept['live']),
            'kept_unknown': sorted(kept['unknown']),
            'backups': found['backups']}


def discard_staged(staged):
    """取消或出错时丢弃全部临时文件，原目录保持不变。"""
    count = 0
    for r in staged:
        if not r.tmp_path:
            continue
        try:
            os.remove(r.tmp_path)
            count += 1
        except OSError:
            pass
    return count


def protected_paths(sources):
    """把来源路径规范化成一组比较用的键，给"这个能不能删"用。

    一律走 `realpath`：Windows 上的 junction 和 symlink 会让两条看起来毫不
    相干的路径指向同一个地方，只比字符串是比不出来的。
    """
    out = []
    for s in sources:
        if not s:
            continue
        try:
            real = os.path.realpath(s)
        except OSError:
            real = os.path.abspath(s)
        out.append(os.path.normcase(real))
    return out


def is_protected(entry, protected):
    """`entry` 碰不碰得到某个来源。三种情况都要拦：

    1. **它就是来源** —— 删了等于删掉用户指定的那个文件夹；
    2. **它装着来源**（是来源的上层目录）—— `shutil.rmtree` 是递归的，
       删上层会把来源一起带走；
    3. **它在来源里面** —— 这一条是后补的。目录后缀留空时，
       「输出目录」算出来就是来源目录本身，于是清空循环遍历的是来源**里面**
       的东西。那些文件既不等于来源、也不是来源的祖先，前两条都拦不住，
       原图就被一个一个删掉了。

    一律走 `realpath`：Windows 上的 junction 和 symlink 会让两条看起来毫不
    相干的路径指向同一个地方，只比字符串是比不出来的。
    """
    try:
        real = os.path.normcase(os.path.realpath(entry))
    except OSError:
        real = os.path.normcase(os.path.abspath(entry))
    for p in protected:
        if real == p or path_inside(p, real) or path_inside(real, p):
            return True
    return False


def output_dirs_for(src_root, dst_root, is_export, keep_structure,
                    suffix, roots=()):
    """这一批产物会写进哪些**顶层**目录。

    **GUI 和 API 共用这一份算法。** 原来两边各算各的（界面是
    `_output_dir_candidates`，API 在 `build_tasks` 里现算），于是"清空时检查
    哪些目录"和"实际写进哪些目录"可能对不上 —— 以前就这么漏过：
    清空那一侧已经防住了，写入这一侧还在往来源里写。
    """
    if not is_export:
        return []                       # 原地修改，没有"导出目录"这回事
    if not keep_structure:
        return [dst_root]
    out = []
    for r in (list(roots) or [src_root]):
        # 和 `build_tasks` 里拼 `final_dir` 的写法逐字对应：来源是盘符根目录时
        # 文件夹名是空的，那边照样拼 `dst_root\<后缀>`，这边不能跳过它。
        folder = os.path.basename(os.path.abspath(r)) + (suffix or '')
        out.append(os.path.join(dst_root, folder) if folder else dst_root)
    return out or [dst_root]


def output_lands_in_source(final_dirs, sources):
    """产物目录**就是**某个来源、或者在来源**里面**吗？是的话返回一句说明。

    这是 `output_escape_error` 管不到的那一半。那个函数比的是用户填的顶层
    `dest`；而真正要写进去的是 `dest\<源文件夹名><后缀>` —— **后缀留空时，
    它算出来正好是来源目录本身**，顶层检查却因为 `dest` 不在来源里而放行。

    实测：来源 `parent/source`、导出填 `parent`、保留结构、
    后缀留空，连跑四次文件数 **2 → 4 → 8 → 16**，每次都报成功。

    注意**不拦"产物目录包含来源"**：平铺模式下导出到上级目录是正常用法，
    产物和来源目录做邻居，不会被下一轮扫进去。
    """
    srcs = protected_paths(sources)
    for d in final_dirs:
        try:
            real = os.path.normcase(os.path.realpath(d))
        except OSError:
            real = os.path.normcase(os.path.abspath(d))
        for p in srcs:
            if real == p:
                return (f'导出目录算下来正好是来源文件夹本身（{d}）—— '
                        f'产物会落回扫描范围，再跑一次就被当成新图重新处理，'
                        f'文件每跑一次翻一倍。给「导出目录后缀」填个名字，'
                        f'或者换一个导出位置。')
            if path_inside(real, p):
                return (f'导出目录算下来落在来源文件夹里面（{d}）—— '
                        f'产物会落回扫描范围，文件每跑一次翻一倍。'
                        f'换一个导出位置。')
    return ''


def planned_output_lands_in_source(tasks, sources):
    """拿**真正排好的任务**再核一遍：有没有哪个产物要写进来源目录。

    `output_dirs_for` 是在扫描之前、按参数**推算**输出目录；这里看的是
    `build_tasks` 实际定下来的每一个目标路径。两者本该一致，可"本该一致"
    正是两次漏掉的地方：API 收到的是文件列表时，
    入口把文件路径当成目录根去推算（`dest\\a.png`），而任务实际写进
    `dest\\<所在文件夹名><后缀>`，后缀留空时那就是来源目录本身，
    `a_1.png` 落回了 `a.png` 旁边，返回值还是 ok。

    所以推算那一道留着（它能在扫描前就拦下来），这里再按结果兜一道。
    `build_tasks` 只读目录、不写文件，在它之后、开工之前检查是来得及的。
    """
    final_dirs = sorted({os.path.dirname(t.dst) for t in tasks if t.dst})
    return output_lands_in_source(final_dirs, sources)
