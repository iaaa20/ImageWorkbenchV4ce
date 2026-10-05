"""应用主体：状态、事件处理与后台工作线程。"""

# ============================================================
# 【导读】app.py —— 程序的「调度中心」：管点了按钮之后发生什么。
# 一次处理的主线就两个函数：
#   start_processing（检查设置）→ process_worker（后台干活，里面按「第 N 步」标了路标）
# 其他常用：log / flush_logs（日志）、request_cancel（取消）、_identify_all（开工前识别文件）。
# 界面长什么样不在这里，在 ui.py；一张图具体怎么处理，在 pipeline.py。
# 整体流程见项目根目录的《代码导读.md》。
# ============================================================
import os
import shutil
import threading
import time
import inspect
import traceback
from concurrent.futures import (FIRST_COMPLETED, Future, ProcessPoolExecutor,
                                ThreadPoolExecutor)
from concurrent.futures import wait as futures_wait
from itertools import chain
import ttkbootstrap as ttk
from ttkbootstrap.constants import BOTH, X, YES
from tkinter import filedialog, messagebox as _messagebox, scrolledtext


class _TranslatingMessagebox:
    """弹窗代理：把标题和正文翻一道，再交给真正的 messagebox。

    做成代理而不是改 20 处调用点，是因为很多调用藏在
    `root.after(0, lambda: messagebox.showwarning(...))` 里，逐个改又碎又容易漏。
    测试里 `app_mod.messagebox.showinfo = lambda...` 这种替换照样有效 ——
    实例属性会盖住 __getattr__。
    """

    def __getattr__(self, name):
        real = getattr(_messagebox, name)

        def wrapper(*args, **kw):
            args = tuple(i18n.tr(a) if isinstance(a, str) else a for a in args)
            for k in ('title', 'message'):
                if isinstance(kw.get(k), str):
                    kw[k] = i18n.tr(kw[k])
            return real(*args, **kw)

        return wrapper


messagebox = _TranslatingMessagebox()

from .config import (COPY_RATE_MBPS, DEFAULT_MAX_EDGE_LABEL,
                     DEFAULT_RATE_MPXPS, DEFAULT_TIMEOUT_LABEL,
                     LOG_FLUSH_INTERVAL, MAX_EDGE_CHOICES, PROCESS_THRESHOLD,
                     PROGRESS_HEARTBEAT_SEC, SCAN_RATE_MPXPS, SLOW_TASK_SEC,
                     STALL_REPORT_SEC, TIMEOUT_CHOICES, TREE_SCAN_INTERVAL,
                     UI_UPDATE_INTERVAL, VALID_EXTENSIONS, TaskResult)
from .dnd import HAS_DND, TkinterDnD
from . import i18n
from . import logfile
from .pipeline import identify, probe_peak, process_single_image
from . import perf
from .presets import BY_KEY
from .tasks import (BACKUP_MARK, build_tasks, commit_staged, discard_staged,
                    find_leftovers, is_protected,
                    output_dirs_for, output_escape_error,
                    output_lands_in_source, planned_output_lands_in_source,
                    protected_paths, scan_files, sweep_temps)
from .ui import UIBuilderMixin
from .utils import (get_resource_path, name_segment_error, parse_drop_paths,
                    parse_source_paths)


NEWLINE = chr(10)

# 关窗口时如果工作线程正在提交，最多再等这么久。提交只是改名，正常几秒就完；
# 这个上限只为防止卡死在掉线的网络盘上（见 _await_worker_exit）。
COMMIT_GRACE_SECONDS = 120


class OptionVar(ttk.StringVar):
    """下拉框专用变量：**界面显示跟着语言走，用 get() 读出来永远是中文。**

    为什么能这么干：ttk 控件显示的是 Tcl 那一层的变量，由 C 代码直接读写，
    **根本不经过 Python 的 get()**。所以重写 get() 只影响 Python 侧的读取 ——
    也就是所有判断逻辑 —— 界面照样显示英文。

    好处是 `cfg.quality_val.get()` 这类读取点（全项目 16 处，散在
    tasks.py / pipeline 判断里）一处都不用动，自然也改不出 bug。
    """

    def get(self):
        # 逻辑读：把界面上的英文还原成中文原文
        return i18n.canon(super().get())

    def set(self, value):
        # 逻辑写（初始值、一键预设、测试）：传中文，按当前语言显示
        super().set(i18n.display(value))

    def relabel(self):
        """切语言后重贴标签：先还原成中文，再按新语言显示一遍。"""
        super().set(i18n.display(i18n.canon(super().get())))



# 【主程序】整个窗口就是这一个类。
# 界面怎么摆在 ui.py（这里继承了那边的 UIBuilderMixin），这个文件管「点了按钮之后发生什么」。
class ImageProcessorApp(UIBuilderMixin):
    # 只有自测会设（`程序 --selftest --scale 2`）：让 Tk 按这个倍数画界面，
    # 用来在 100% 缩放的机器上**模拟**高缩放下的布局压力。它不等于真的把
    # 系统缩放调到 200% —— 那还牵涉显示器和系统的行为 —— 只是比不验强。
    FORCE_UI_SCALE = None

    # 窗口刚打开时跑一次：建窗口、准备好每个设置项对应的变量、把界面搭出来。
    def __init__(self):
        if HAS_DND:
            self.root = TkinterDnD.Tk()

            self.style = ttk.Style()
            try:
                self.style.theme_use('cosmo')
            except:
                pass

        else:
            self.root = ttk.Window(themename='cosmo')

        if self.FORCE_UI_SCALE:
            try:
                self.root.tk.call('tk', 'scaling',
                                  1.3333 * float(self.FORCE_UI_SCALE))
            except Exception:
                pass

        # **界面回调里逃出来的异常要进日志。** Tk 默认把它打到 stderr 就算了，
        # 而 windowed 打包版根本没有 stderr —— 这类 bug 就彻底消失：用户只看到
        # 某个操作「没反应」，日志里一个字都没有。测试里也一样藏得住，
        # test_ui 漏了预扫描的 TclError 很久，退出码始终是 0。
        self.root.report_callback_exception = self._on_tk_error

        self.root.title('图片处理工作台 V4ce')
        # 尺寸必须跟着界面缩放走。入口调了 SetProcessDpiAwareness(1)，Windows
        # 不再代为拉伸，Tk 按物理像素画控件 —— 200% 缩放下写死的 720 只有实际
        # 需要的一半宽，右边 400 多像素（能力指示器、「⚙ 高级设置」入口、
        # 「绝不动原文件」那句承诺）全被切在窗口外面，而且够不着。
        _s = self.ui_scale()
        self.root.geometry(f'{int(720 * _s)}x{int(560 * _s)}')
        self.root.minsize(int(680 * _s), int(460 * _s))


        try:
            icon_path = get_resource_path('logo.ico')
            if os.path.exists(icon_path):

                self.root.iconbitmap(icon_path)
        except Exception as e:
            print(f'设置图标失败: {e}')


        self.root.attributes('-topmost', True)
        self.root.after(1500, lambda: self.root.attributes('-topmost', False))
        self.root.focus_force()
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)


        self.is_running = False
        self.cancel_requested = False
        self.paused = False
        self.error_list = []
        self.processed_count = 0
        self.total_count = 0
        self.log_queue = []
        self.dropped_paths = []
        self.dropped_roots = []   # 拖进来的每个文件夹各算一条源路径
        self.executor = None      # 关窗时要靠它去终结子进程
        # 「强制停止」跟「取消」不是一回事：取消在直写模式下要等在途任务写完，
        # 免得留下截断的文件；强制停止就是不等。收尾时靠这个标志决定 shutdown
        # 要不要 wait —— 少了它，强制停止会卡在 executor 的收尾里等上几分钟。
        self.force_stopped = False
        # 见过的每一个工作子进程的 pid。不能只靠 executor._processes：
        # max_tasks_per_child 会不停换新进程，而池子一旦进入收尾，那个字典就被
        # 清空了 —— 那时再去杀就一个也找不到，进程却还活着（用户在任务管理器里
        # 看到的就是这些）。所以自己留一份，只增不减。
        self._child_pids = set()
        self._child_born = {}     # pid -> 创建时刻，杀之前核对用（见 _note_children）
        self._last_tree_scan = 0.0
        # 工作线程正在「提交」（把临时文件改名到位）时为 True。这一段是不能被
        # 打断的临界区，关窗口时要等它做完 —— 见 _await_worker_exit。
        self._committing = False
        # 状态栏归谁写。跑完的结局必须压过预扫描 —— 否则一次快速失败之后，
        # 400ms 防抖的预扫描回来一句「找到 N 个文件，可以开始」，
        # 把「出错了」直接冲掉，用户就只剩"点了没反应"这一个印象。
        self._status_owner = 'idle'
        self._file_info = {}       # 预扫描认出来的结果，src -> FileInfo
        self._scan_pool = None     # 预扫描用的线程池，关窗口时要叫停
        self._skipped_ahead = 0    # 派发时「往后挑一张放得下的」的次数
        self._budget_mb = 2048.0   # 本轮的内存闸门预算，预扫描算耗时要用
        # 每开一轮 +1。取消/强制停止会安排一次**延迟**清扫，而清扫必须只对
        # 安排它的那一轮负责 —— 否则用户取消完马上又开一轮，十秒后那次清扫
        # 落在新一轮头上，把它已完成的临时文件全删掉。
        self._run_id = 0
        self._output_dirs = set() # 强制关闭后要去这些目录扫残留临时文件
        self.peak_in_flight_mb = 0.0


        self.src_folder = ttk.StringVar()
        self.scan_subdirs = ttk.BooleanVar(value=True)

        self.out_mode = ttk.StringVar(value='new')
        self.new_folder_path = ttk.StringVar()
        self.keep_structure = ttk.BooleanVar(value=True)
        self.dir_suffix_val = ttk.StringVar(value=i18n.default_dir_suffix())

        self.name_mode = ttk.StringVar(value='prefix')
        self.prefix_val = ttk.StringVar(value='IMG')
        self.start_num = ttk.IntVar(value=1)
        self.digit_val = OptionVar(value='3位 (001)')

        self.rotate_val = OptionVar(value='不旋转')
        self.quality_val = OptionVar(value='通用高清 (95%)')
        self.compress_only = ttk.BooleanVar(value=False)
        self.is_ico = ttk.BooleanVar(value=False)


        self.ico_mode_val = ttk.StringVar(value='multi')

        self.ico_format_val = OptionVar(value='自动 (推荐)')

        self.max_workers = ttk.IntVar(value=min(8, os.cpu_count() or 4))
        self.safe_write = ttk.BooleanVar(value=True)
        self.copy_others = ttk.BooleanVar(value=True)
        self.reencode_anim = ttk.BooleanVar(value=False)
        self.perf_mode = ttk.StringVar(value=perf.BALANCED)
        self.mem_limit_val = OptionVar(value=perf.MEMORY_LIMIT_CHOICES[0][0])
        self.timeout_val = OptionVar(value=DEFAULT_TIMEOUT_LABEL)
        self.max_edge_val = OptionVar(value=DEFAULT_MAX_EDGE_LABEL)


        for var in (self.prefix_val, self.digit_val, self.name_mode, self.is_ico):
            var.trace_add('write', self.update_preview)


        self.is_ico.trace_add('write', lambda *args: self.update_ui())
        self.ico_mode_val.trace_add('write', self.update_preview)

        self.start_num.trace_add('write', self.update_preview)

        self.create_ui()

        # 控件文字的中文原文，切换语言时照这份重贴
        self._snapshot_texts()
        if i18n.get_lang() != 'zh':
            self._retranslate_ui()

        # 选完/拖完文件夹就报一句「找到 N 个」，别让用户先点了才知道对不对
        self.src_folder.trace_add('write', self._schedule_prescan)
        self.scan_subdirs.trace_add('write', self._schedule_prescan)

    # ================================================================
    # 语言切换
    # ================================================================
    def _snapshot_texts(self, parent=None):
        """界面刚建好时，把每个控件的中文原文记下来。

        为什么要存原文快照、而不是切换时拿当前文字反查一张英→中的表：
        反查遇到重复译文就会还原错（两个按钮都译成 Close，还原时分不清谁是
        「关闭」谁是「关掉窗口」）。存原文不可能错。
        """
        if parent is None:
            parent = self.root
            self._text_origin = []
        for w in parent.winfo_children():
            try:
                cur = w.cget('text')
            except Exception:
                cur = None
            if isinstance(cur, str) and cur:
                self._text_origin.append((w, cur))
            self._snapshot_texts(w)

    def _retranslate_ui(self):
        """按当前语言，把所有控件文字照快照重贴一遍。"""
        for w, zh in self._text_origin:
            try:
                w.configure(text=i18n.tr(zh))
            except Exception:
                pass                        # 控件已销毁：跳过，不值得为此报错
        # 下拉框：选项列表和当前选中项都要换成新语言的写法
        for attr in ('cb_digit', 'cb_rotate', 'cb_quality', 'cb_ico_fmt',
                     'cb_mem', 'cb_edge', 'cb_timeout'):
            cb = getattr(self, attr, None)
            if cb is None or attr not in self._cb_zh:
                continue
            try:
                cb.configure(values=i18n.options(self._cb_zh[attr]))
            except Exception:
                pass
        for attr in ('digit_val', 'rotate_val', 'quality_val', 'ico_format_val',
                     'mem_limit_val', 'timeout_val', 'max_edge_val'):
            var = getattr(self, attr, None)
            if isinstance(var, OptionVar):
                var.relabel()

    def switch_language(self, lang=None):
        """切到另一种语言。不传 lang 就在中英之间来回切。"""
        if lang is None:
            lang = 'en' if i18n.get_lang() == 'zh' else 'zh'
        if lang == i18n.get_lang():
            return
        # 导出文件夹后缀还是默认值的话，跟着换成新语言的默认值；
        # 用户自己填过的不动。
        suffix_untouched = self.dir_suffix_val.get() == i18n.default_dir_suffix()
        i18n.set_lang(lang)
        i18n.save_pref(lang)
        if suffix_untouched:
            self.dir_suffix_val.set(i18n.default_dir_suffix())
        self._retranslate_ui()
        # 简易/高级那两行字是 set_mode() 写的，快照重贴会把它们拍回初始值，
        # 所以让 set_mode 按当前模式再写一次
        self.set_mode(getattr(self, 'advanced_mode', False))
        self.update_ui()
        self.update_preview()
        self.log('🌐 界面语言已切换')
        self.flush_logs()      # 跟项目里别处一样：记完立刻刷，否则这行会丢
        logfile.op('切换界面语言', lang)

    def _last_dir(self, *candidates):
        """选择框的起始目录。批量工具经常在同几个目录之间来回切，
        每次都从「文档」开始很折磨人。"""
        for c in candidates:
            if c and os.path.isdir(c):
                return c
        return None

    # 点「浏览…」选来源文件夹时调用：弹出选择框，选好后顺手把导出位置填上默认值。
    def select_source(self):
        d = filedialog.askdirectory(
            title='选择图片来源文件夹', parent=self.root,
            initialdir=self._last_dir(self.src_folder.get(),
                                      self.new_folder_path.get()))
        if d:
            # 必须清掉拖拽列表，否则 scan_files 会继续用旧的拖拽结果，
            # 界面显示的是新文件夹、实际处理（甚至删除）的却是上一批文件。
            if self.dropped_paths:
                self.dropped_paths = []
                self.dropped_roots = []
                self.log('📁 已切换到文件夹模式，之前拖拽的文件不再参与处理')
            self.src_folder.set(d)
            self._default_dest_from(d)
            logfile.op('选择来源文件夹', d)
            self.flush_logs()

    def apply_preset(self, key):
        """一键按钮：套用预设并立刻开跑。

        预设一律导出到源文件夹旁边的「xxx_已处理」，不碰原文件 —— 主界面面向
        不看参数的用户，默认就必须是撤得回来的。想原地修改走高级设置。
        """
        if self.is_running:
            return

        preset = BY_KEY.get(key)
        if preset is None:
            return

        base = self.src_folder.get()
        if self.dropped_paths:
            base = os.path.dirname(self.dropped_paths[0])
        elif not base or not os.path.isdir(base):
            # 还没选来源就直接点按钮，是很自然的用法，帮他把选择框弹出来
            self.select_source()
            base = self.src_folder.get()
            if not base or not os.path.isdir(base):
                return

        base = os.path.abspath(base)
        parent = os.path.dirname(base)
        # 盘符根目录没有上一层，退回写在自己里面，避免生成一个叫「_已处理」的怪目录
        if not parent or not os.path.basename(base):
            parent = base

        self.out_mode.set('new')
        self.new_folder_path.set(parent)
        self.keep_structure.set(True)
        self.dir_suffix_val.set(i18n.default_dir_suffix())
        self.safe_write.set(True)
        self.copy_others.set(True)
        # 一键按钮面向不看参数的用户，动图一律带过 —— 重编码只会又慢又胀
        self.reencode_anim.set(False)
        # 同理，一键按钮绝不悄悄改像素尺寸。要缩尺寸就去高级设置里明确选。
        self.max_edge_val.set(DEFAULT_MAX_EDGE_LABEL)
        self.digit_val.set('自动 (按数量)')

        for name, value in preset['settings'].items():
            getattr(self, name).set(value)

        self.update_ui()
        logfile.op('点击一键按钮', f'{key} | {preset["label"]} | 来源 {base}')
        self.log(f'▶ {preset["label"]} —— {preset["desc"]}')
        self.log(f'   输出到 {os.path.join(parent, os.path.basename(base) + i18n.default_dir_suffix())}')
        self.flush_logs()
        self.start_processing()

    def _drop_dest(self, event):
        """拖到「导出位置」输入框上：设导出目录。

        拖的是文件就取它所在的文件夹 —— 用户多半是从目标目录里随手抓了一个
        文件扔过来。窗口级的拖拽仍然是设**来源**，两边互不干扰。
        """
        paths = parse_drop_paths(getattr(event, 'data', '') or '')
        if not paths:
            return
        d = paths[0]
        if not os.path.isdir(d):
            d = os.path.dirname(d)
        if os.path.isdir(d):
            self.new_folder_path.set(d)
            self.log(f'📁 已通过拖拽设置导出位置: {d}')
            logfile.op('拖拽设置导出位置', d)
            self.flush_logs()

    def _default_dest_from(self, src):
        """没填导出位置时，替用户填一个：源文件夹的**上一级**。

        输出实际会落在 `<导出位置>\<源文件夹名>_已处理\`，所以填上一级
        正好让产物成为源文件夹的邻居 —— 这是最符合直觉的默认。
        **不能直接填源文件夹本身**：那样扫描会把自己的产物当成输入再扫一遍，
        `output_escape_error()` 就是为此把这种情况拦下来的。
        """
        if self.new_folder_path.get():
            return                      # 用户已经选过了，不要覆盖
        try:
            parent = os.path.dirname(os.path.abspath(src))
        except Exception:
            return
        if parent and os.path.isdir(parent):
            self.new_folder_path.set(parent)

    # 点「浏览…」选导出位置时调用。
    def select_dest(self):
        d = filedialog.askdirectory(
            title='选择导出位置', parent=self.root,
            initialdir=self._last_dir(self.new_folder_path.get(),
                                      self.src_folder.get()))
        if d:
            self.new_folder_path.set(d)

    def handle_drop(self, event):
        """处理拖拽事件"""
        paths = parse_drop_paths(event.data)

        if not paths:
            return

        if len(paths) == 1 and os.path.isdir(paths[0]):
            self.src_folder.set(paths[0])
            self.dropped_paths = []
            self.dropped_roots = []
            self._default_dest_from(paths[0])
            self.log(f'📁 已通过拖拽设置源文件夹: {os.path.basename(paths[0])}')
        else:
            take_all = self.copy_others.get()

            # 这个文件要不要收进来：开了「带上不支持的文件」就全要，否则只要图片。
            def wanted(name):
                return take_all or name.lower().endswith(VALID_EXTENSIONS)

            valid_paths = []
            roots = []
            for p in paths:
                if os.path.isfile(p) and wanted(p):
                    valid_paths.append(p)
                    # 散落的单个文件按它所在的目录归一条源路径
                    roots.append(os.path.dirname(os.path.abspath(p)))
                elif os.path.isdir(p):
                    # 每个拖进来的文件夹自成一条源路径，各自保留内部结构，
                    # 导出时也各得一个「文件夹名_已处理」
                    roots.append(os.path.abspath(p))
                    for root, _, files in os.walk(p):
                        for f in files:
                            if wanted(f):
                                valid_paths.append(os.path.join(root, f))

            if valid_paths:
                valid_paths = list(dict.fromkeys(valid_paths))
                self.dropped_paths = valid_paths
                self.dropped_roots = list(dict.fromkeys(roots))
                n_root = len(self.dropped_roots)
                self.src_folder.set(
                    f'已选择 {n_root} 个路径 / {len(valid_paths)} 个文件'
                    if n_root > 1 else f'已选择 {len(valid_paths)} 个文件/项')
                self.log(f'📥 已拖拽导入 {len(valid_paths)} 个文件，'
                         f'来自 {n_root} 条源路径')
                for r in self.dropped_roots[:6]:
                    self.log(f'   • {r}')
                if n_root > 6:
                    self.log(f'   • …… 其余 {n_root - 6} 条')

                # 拖进来但没被接收的，要说清楚，别让用户以为整批都进来了
                ignored = sum(1 for p in paths
                              if os.path.isfile(p)
                              and not p.lower().endswith(VALID_EXTENSIONS))
                if ignored:
                    self.log(f'ℹ️ 其中 {ignored} 个文件格式不支持，已忽略')
            else:
                messagebox.showwarning('格式不支持', '拖拽的文件中没有支持的图片格式', parent=self.root)

        self.flush_logs()

    # 往日志里记一行。先排进队列（后台线程也能安全调用），
    # 再由 flush_logs 统一写到界面上；同时存一份到 logs 文件夹里的 imgwb.log。
    def log(self, message: str):
        # **翻译就发生在这一个出口。** 代码里照样写中文，显示和落盘时才翻 ——
        # 这样 180 多处 self.log(f'...') 一处都不用改，也就不会改出 bug。
        message = i18n.tr(message)
        timestamp = time.strftime('%H:%M:%S')
        self.log_queue.append(f'[{timestamp}] {message}')
        # 界面那个日志框是易失的，关掉窗口就没了。同一行再落一份到磁盘，
        # 用户报问题时直接把 logs\imgwb.log 发过来就行。
        logfile.write(message)

    # 把排队的日志真正写进界面的日志框（只能在界面线程里调用）。
    def flush_logs(self):
        if not self.log_queue:
            return
        # 先整体换出再写。原来是「遍历完再 clear()」，每次 insert 都会释放 GIL，
        # 工作线程在这中间追加的日志会被 clear() 一起抹掉。
        pending, self.log_queue = self.log_queue, []
        self.log_text.configure(state='normal')
        for msg in pending:
            self.log_text.insert('end', msg + '\n')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def shutdown_workers(self):
        """把线程池和它的子进程彻底停掉，返回强制终结了几个子进程。

        只设 cancel_requested 是不够的：工作线程是 daemon，解释器退出时会被
        直接冻住，`with executor:` 的 __exit__ 根本轮不到执行 —— 子进程就滞留
        在后台继续吃内存和 CPU，任务管理器里能看到一堆同名进程慢慢才消失。
        所以这里必须主动把它们结束掉。
        """
        # 预扫描的线程池也要收拾。它排的是几千个 identify，关窗口时如果
        # 还在跑，不叫停就会把整个进程拖着不退。
        scan = getattr(self, '_scan_pool', None)
        if scan is not None:
            try:
                scan.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._scan_pool = None

        ex = self.executor

        # 先把进程列表抄下来再 shutdown —— 之后 _processes 可能已经被线程池的
        # 管理线程清空，那样就一个也杀不到了（但它们其实还活着）。
        procs = []
        if ex is not None:
            procs = list((getattr(ex, '_processes', None) or {}).values())
            self._remember_children()
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

        killed = 0
        for proc in procs:
            try:
                if proc.is_alive():
                    proc.terminate()
                    killed += 1
            except Exception:
                pass

        # 再按 pid 兜一遍。上面那份名单只是「此刻池子还认的进程」，而
        # max_tasks_per_child 换下来的旧进程、以及池子收尾时被清出字典的进程，
        # 都不在里面 —— 它们正是关掉界面后仍留在任务管理器里的那些。
        killed += self._kill_leftover_children()
        return killed

    def _remember_children(self):
        """把当前池子里的子进程 pid 记下来，**连同它们自己的子进程**。

        打包版里每个工作进程其实是两个：PyInstaller 的引导器 + 它解包后起的
        真正的 Python 进程。multiprocessing 只认引导器，杀了它孙子还活着 ——
        那个孙子才是占着一两百 MB 不放的家伙。所以这里要把整棵树记下来。
        """
        ex = self.executor
        direct = {}
        for proc in list((getattr(ex, '_processes', None) or {}).values()):
            pid = getattr(proc, 'pid', None)
            if pid:
                direct[pid] = proc
        if not direct:
            return
        # 池内进程对象是当前可信来源，也允许 PID 正常换代 —— 但**只在换了进程
        # 对象时**才重新查创建时刻。这个函数在派发循环里每完成一个任务就跑
        # 一次；每圈都给全部工人各查一遍的话，实测每秒要白花约 8 毫秒
        # （8 个工人 x 每秒 56 圈 x 18 微秒），正是下面那段注释警告过的热路径。
        # 同一个 pid 对着同一个进程对象，就还是同一个进程，不用再问系统。
        # 手里一直留着这些对象的引用，所以 `is` 比较不会被对象复用骗到。
        seen = getattr(self, '_pool_procs', {})
        fresh = [pid for pid, proc in direct.items()
                 if seen.get(pid) is not proc or pid not in self._child_born]
        self._pool_procs = direct
        if fresh:
            self._note_children(fresh)

        # 抓孙子进程要做一次**全系统**进程快照，实测 6.9 ms。派发循环每完成一个
        # 任务就转一圈，实测每秒转 56 圈 —— 于是 39% 的处理时间花在数进程上
        # （400 张 400x300：1.5 秒里有 0.58 秒）。
        # 而工作进程只在池子建起来和 max_tasks_per_child 换代时才变，隔几秒看
        # 一次完全够用；真正要紧的那次在 _kill_leftover_children 里，收尾前还会
        # 再抓一次，不会漏。
        now = time.time()
        if now - getattr(self, '_last_tree_scan', 0.0) < TREE_SCAN_INTERVAL:
            return
        self._last_tree_scan = now
        try:
            children = perf.owned_descendants(
                {pid: self._child_born.get(pid) for pid in direct})
            self._note_children(children, births=children)
        except Exception:
            pass

    def _note_children(self, pids, births=None):
        """记下这些子进程 pid，**连同各自的创建时刻**。

        pid 会被系统回收：一个工作进程早就退出了（`max_tasks_per_child` 换代），
        它的 pid 后来可能分给别的进程 —— 要是恰好分给了用户另开的那个窗口的
        工作进程（同名），收尾时光比映像名是拦不住的。
        「同一个 pid + 同一个创建时刻」才是同一个进程，所以记的时候把创建时刻
        一起记下，`_kill_leftover_children` 杀之前交给 `perf.kill_pid` 核对。
        后代使用发现时核验过的创建时间，不重新认领此刻同 PID 的其他进程。
        查不到创建时间就不加入按 PID 收尾的名单；池内进程仍由进程对象收尾。
        """
        for pid in pids:
            born = births.get(pid) if births is not None else perf.process_born(pid)
            if born is not None:
                self._child_pids.add(pid)
                self._child_born[pid] = born

    def _kill_leftover_children(self):
        """按 pid 收拾所有还活着的工作子进程，返回杀掉几个。

        只认自己记下来的 pid，绝不按进程名去扫 —— 用户可能同时开着另一个本
        程序的窗口，按名字杀会把人家正在跑的任务一起干掉。

        pid 会被系统回收再分配，所以只在**确定要收尾**时调用（关窗、强制停止、
        取消），不在正常处理途中调用。
        """
        if not self._child_pids:
            return 0

        # 临杀前再抓一次后代：max_tasks_per_child 换上来的新进程可能还没被
        # _remember_children 记到，而它们同样会留在任务管理器里。
        try:
            children = perf.owned_descendants(
                {pid: self._child_born.get(pid) for pid in list(self._child_pids)})
            self._note_children(children, births=children)
        except Exception:
            pass

        killed = 0
        for pid in list(self._child_pids):
            try:
                born = self._child_born.get(pid)
                if born is not None and perf.kill_pid(pid, born=born):
                    killed += 1
            except Exception:
                pass
            self._child_pids.discard(pid)
            self._child_born.pop(pid, None)
        return killed

    def _order_for_throughput(self, tasks, workers, budget_mb):
        """重排派发顺序：**重的先开工**（经典的 LPT 调度）。

        用户实测 4777 张：几张巨图排在队尾，到 89% 之前一直是 ~100 张/秒、
        23 个在飞，之后塌成「在处理 2 个」—— 最后 255 张花掉 3 分钟，
        而全程才 3.9 分钟。

        单张估 692 MB、预算 1639 MB 时同时只放得下 2 张，这是内存决定的，
        加多少核都没用。所以那批巨图本身要花的时间是个**下界**；能选的只有
        「什么时候开工」—— 越早越好，好让它们跟小图的时间重叠掉。

        **「大小兼顾」是靠闸门实现的，不是靠排序。** 上面那个「挑一张放得下
        的」会在巨图占住额度之后，继续挑小图把剩下的额度填满 —— 任何时刻都是
        「一两张巨图 + 一堆小图」，工人满载。排序只管开工先后。

        三种顺序在同一份语料上实测（400 张小图 + 12 张巨图、预算 1 GB，
        巨图本身的下界约 7.2 秒）：

            巨图排在队尾（原顺序）   10.8 秒
            均匀交错                 10.7 秒   <- 每隔 33 张才放一张，开工被推迟
            **重的先开工**            9.5 秒   <- 最接近下界

        交错听起来更「均衡」，但它推迟了巨图开工，尾巴照样甩出来。

        判据是「这张图占掉多少额度」：超过 `预算 / 并发数` 的算重的 ——
        它一进来就挤掉了不止一个工人的份额。输出文件名在 `build_tasks` 阶段
        就定死了，跟派发顺序无关，重排不影响编号。
        """
        if not tasks:
            return tasks

        # 取这张图预估要占多少内存（MB）；没识别到的当 0。
        def peak(t):
            got = self._file_info.get(t.src)
            return got.est_mb if got is not None else 0.0

        share = max(budget_mb / max(workers, 1), 1.0)
        heavy = [t for t in tasks if peak(t) > share]
        if not heavy or len(heavy) == len(tasks):
            return tasks              # 全轻或全重，怎么排都一样
        light = [t for t in tasks if peak(t) <= share]
        heavy.sort(key=peak, reverse=True)
        return heavy + light

    def _identify_all(self, tasks, workers, budget_mb):
        """并行认一遍全部文件，然后把「处理范围 + 预计耗时 + 问题」说清楚。

        **必须并行**。识别虽然只读文件头，但 3000 张在主线程上一张张认也要
        十几秒，而这段时间工人是闲着的 —— 跟之前 `probe_peak` 在主线程上解
        PNG 是同一类毛病。

        **不能用 `with ThreadPoolExecutor(...)`。** 它的 `__exit__` 是
        `shutdown(wait=True)`，而 `map` 已经把几千个任务全排进队列了 ——
        用户点取消、或者直接关窗口，只是跳出这个 for 循环，退出 with 时仍然
        要把剩下几千张全认完才肯走。表现就是「关了界面进程还在」。
        所以手动建、手动 `shutdown(wait=False, cancel_futures=True)`，
        并且挂到 `self._scan_pool` 上让 `shutdown_workers()` 也能收拾它。
        """
        infos = []
        t_scan = time.time()
        # 线程池，不是进程池：识别只读文件头、以 I/O 为主，走线程免掉 pickle
        # 和进程启动。而且它跑在建工作进程池**之前** —— 识别的产物之一就是
        # 「这批图多大」，那决定了子进程多久回收一次。
        pool = ThreadPoolExecutor(max_workers=max(2, workers))
        self._scan_pool = pool
        try:
            for info in pool.map(
                    identify,
                    [(t.src, t.dst, t.config, t.copy_only) for t in tasks]):
                infos.append(info)
                if self.cancel_requested or self.force_stopped:
                    break
        except Exception as exc:
            # 认不出来不该挡住处理本身 —— 退回到边跑边 probe 的老路。
            logfile.exception('预扫描失败，退回边跑边估', exc)
            self.log(f'   ⚠️ 识别阶段出错（{exc}），改成边处理边估算')
            return []
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            self._scan_pool = None

        self._report_scan(infos, workers, time.time() - t_scan, budget_mb)
        return infos

    def _report_scan(self, infos, workers, spent=0.0, budget_mb=0.0):
        """把预扫描的结论说给用户听：范围、时间、问题。"""
        if not infos:
            return
        buckets = {}
        for i in infos:
            buckets.setdefault(i.plan, []).append(i)
        n_proc = len(buckets.get('process', ()))
        n_copy = len(buckets.get('copy', ()))
        n_carry = len(buckets.get('carry', ()))
        bad = buckets.get('error', [])

        total_mb = sum(i.size_bytes for i in infos) / 1048576
        self.log(f'📋 识别完成（{spent:.1f} 秒）——'
                 f' 共 {len(infos)} 个文件、{total_mb:.0f} MB')
        parts = []
        if n_proc:
            mb = sum(i.size_bytes for i in buckets['process']) / 1048576
            # 「最多」是实话：编完发现比原文件还大的，体积护栏会保留原文件，
            # 而那件事只有真编一遍才知道，预扫描预测不了。
            parts.append(f'最多重新编码 {n_proc} 张（{mb:.0f} MB）')
        if n_copy:
            parts.append(f'原样复制 {n_copy} 张（再压也省不下 / 多帧图）')
        if n_carry:
            parts.append(f'跟着编号带走 {n_carry} 个（不支持的格式）')
        if bad:
            parts.append(f'**打不开 {len(bad)} 个**')
        self.log('   ' + i18n.join(parts))

        # 各格式分布 —— 按**真实格式**数，不是看扩展名
        by_fmt = {}
        for i in infos:
            if i.fmt:
                by_fmt[i.fmt] = by_fmt.get(i.fmt, 0) + 1
        if len(by_fmt) > 1:
            self.log('   格式分布：' + i18n.join(
                [f'{k} {v} 张' for k, v in
                 sorted(by_fmt.items(), key=lambda kv: -kv[1])], '、'))

        # 预计耗时。闸门允许的并发常常比工人数还小，这里一并说清楚，
        # 免得用户看到 8 个工人却只有 4 张在跑以为卡住了。
        need = buckets.get('process', ())
        if need:
            avg_mb = sum(i.est_mb for i in need) / len(need)
            budget = budget_mb or self._budget_mb
            gate_n = max(1, int(budget / avg_mb)) if avg_mb else workers
            eff = max(1, min(workers, gate_n))
            # 按像素算，不按字节 —— 编解码的工作量跟像素数走
            secs = sum(i.width * i.height / 1e6
                       / SCAN_RATE_MPXPS.get(i.fmt, DEFAULT_RATE_MPXPS)
                       for i in need) / eff
            secs += sum(i.size_bytes for i in infos
                        if i.plan in ('copy', 'carry')) / 1048576 / COPY_RATE_MBPS
            human = (f'{secs:.0f} 秒' if secs < 90
                     else f'{secs / 60:.1f} 分钟')
            mpx = sum(i.width * i.height for i in need) / 1e6
            self.log(f'   预计耗时约 {human}'
                     f'（{mpx:.0f} 百万像素，并发 {workers}，'
                     f'单张平均预估 {avg_mb:.0f} MB，'
                     f'内存上限同时放得下 {gate_n} 张；'
                     f'开跑后以进度条上的实际速度为准）')

        # ---- 问题清单：开工前就说，别等跑完 -------------------------
        for i in bad[:5]:
            self.log(f'   ❌ {os.path.basename(i.path)} 打不开：{i.error}')
        if len(bad) > 5:
            self.log(f'   ❌ 另有 {len(bad) - 5} 个打不开的文件（详见系统日志）')
        for i in bad:
            logfile.write(f'预扫描打不开: {i.path} -> {i.error}', 'ERROR')

        mis = [i for i in infos if i.mislabeled]
        if mis:
            self.log(f'   ⚠️ {len(mis)} 个文件的扩展名和真实格式对不上，'
                     f'例如 {os.path.basename(mis[0].path)}'
                     f'（实际是 {mis[0].fmt}）')
        lost = [i for i in infos if i.alpha_loss and i.plan == 'process']
        if lost:
            self.log(f'   ⚠️ {len(lost)} 张带透明通道的图会按目标格式编码，'
                     f'**透明会丢**，例如 {os.path.basename(lost[0].path)}'
                     f'（{lost[0].mode}）')
        anim = [i for i in infos if i.frames > 1 and i.plan == 'copy']
        if anim:
            self.log(f'   ⓘ {len(anim)} 个多帧图不重编码，原样带过')
        self.root.after(0, self.flush_logs)

    def sweep_when_idle(self, deadline=None, run_id=None):
        """等处理线程彻底停下来后，再补扫一次残留的临时文件。

        terminate() 是异步的：被杀的子进程可能在工作线程那次清扫之后才把写了
        一半的临时文件落到盘上，所以必须等都停了再补一遍。

        **这次清扫只对安排它的那一轮负责。** 原来只有一个 10 秒 deadline，
        到点就扫、不管还在不在跑 —— 用户取消完马上又开一轮，十秒后这次清扫
        就落在新一轮头上，把它已完成的临时文件全删光。安全写入 + 原地修改的
        组合下，旧版 commit 是"先删源再改名"，于是源图和产物一起没，
        而且一句提示都没有。用户报的「多次处理中普通取消会损失大量图片」
        就是这条路径。
        """
        if deadline is None:
            deadline = time.time() + 10
        if run_id is None:
            run_id = self._run_id
        # 已经换了一轮：那些临时文件属于新的一轮，彻底放手
        if run_id != self._run_id:
            return
        if self.is_running and time.time() < deadline:
            self.root.after(
                200, lambda: self.sweep_when_idle(deadline, run_id))
            return
        # 到点了却还在跑 —— 宁可不扫也不能误删正在用的临时文件
        if self.is_running:
            return

        left = sweep_temps(self._output_dirs)
        if left:
            self.log(f'🧹 清理了 {left} 个残留临时文件')
            self.flush_logs()

    def on_close(self):
        """关窗口。还在处理就先把任务和子进程停干净再退。"""
        logfile.op('关闭窗口',
                   f'处理中 {self.processed_count}/{self.total_count}'
                   if self.is_running else '空闲')
        if not self.is_running:
            self.shutdown_workers()
            self.root.destroy()
            return

        if not messagebox.askokcancel(
                '仍在处理中',
                '还有任务正在处理。\n关闭窗口会取消本次处理，确定吗？', parent=self.root):
            return

        self.cancel_requested = True
        self.status.configure(text=i18n.tr('正在停止...'), bootstyle='warning')
        # 先掐断子进程：在途任务会立刻变成失败，工作线程才能马上跳出等待，
        # 进而执行它自己的收尾（撤回临时文件）。干等 3 秒是等不到的。
        killed = self.shutdown_workers()
        if killed:
            self.log(f'⏹ 已终止 {killed} 个工作进程')
        self._await_worker_exit(time.time() + 5)

    def _await_worker_exit(self, deadline, commit_deadline=None):
        """等工作线程收完尾再销毁窗口；到点就不等了 —— **除非它正在提交**。

        原来是到点就「清扫临时文件 + 销毁窗口」，不管工作线程走到哪了。
        问题出在用户恰好在**提交阶段**关窗口的时候（几千张图在慢盘上，
        提交本身就能超过 5 秒）：那时 `cancel_requested` 已经来不及生效，
        工作线程正在逐个把临时文件改名到位 ——

        * 这里的清扫不带 `exclude`，会把**还没来得及改名的产物**当垃圾删掉；
        * 紧接着销毁窗口、进程退出，工作线程是 daemon，被冻在提交中途：
          原地修改模式下，已经让位的原文件就留在 `.imgwb-backup-` 名下，
          产物又被上一步扫掉了。字节没丢，但目录里是一堆备份名，
          而且一句提示都没有。

        所以：提交期间不设 5 秒那个期限，等它做完再退（提交只是改名，有尽头）；
        另给一个很宽的上限防止卡死在网络盘上 —— 真到了上限，**不清扫**直接退，
        并把情况写进日志：临时文件和备份都留着，至少能手工找回。
        """
        now = time.time()
        if self.is_running and self._committing:
            if commit_deadline is None:
                commit_deadline = now + COMMIT_GRACE_SECONDS
                self.status.configure(
                    text=i18n.tr('正在保存已完成的结果，请稍候…'),
                    bootstyle='warning')
            if now < commit_deadline:
                self.root.after(100, lambda: self._await_worker_exit(
                    deadline, commit_deadline))
                return
            where = '、'.join(sorted(self._output_dirs)[:5])
            logfile.write(
                f'关闭窗口时提交还没做完，等了 {COMMIT_GRACE_SECONDS} 秒后放弃等待。'
                f'没有清扫临时文件：原文件可能留在 {BACKUP_MARK} 开头的备份名下，'
                f'产物可能还是 .~imgwb 临时文件，都在这些目录里: {where}', 'ERROR')
            self.shutdown_workers()
            self.root.destroy()
            return
        if self.is_running and now < deadline:
            self.root.after(100, lambda: self._await_worker_exit(
                deadline, commit_deadline))
            return
        self.shutdown_workers()      # 再兜一次，防止期间又起了新的
        # 清扫必须放在确认子进程都死掉之后：terminate() 是异步的，被杀的进程
        # 可能在工作线程那次清扫之后才把半截临时文件留在盘上。
        # 走到这里要么工作线程已经收完尾，要么它还卡在提交**之前**（等在途
        # 任务）—— 那一轮反正是取消，临时文件本来就要撤回。
        left = sweep_temps(self._output_dirs)
        if left:
            print(f'已清理 {left} 个残留临时文件')
        self.root.destroy()

    def _pump_pause_ui(self):
        """暂停空转时也让状态栏和日志跟上，别看着像卡死。"""
        self.root.after(0, self.flush_logs)

    def toggle_pause(self):
        """暂停 / 继续。

        暂停只是停止**派发**新任务，已经在跑的那几张会跑完 —— 强行掐断它们
        会留下半截文件。继续时从断点接着派发，不会重跑已完成的。
        """
        if not self.is_running:
            return

        self.paused = not self.paused
        logfile.op('暂停' if self.paused else '继续',
                   f'{self.processed_count}/{self.total_count}')
        if self.paused:
            self.log(f'⏸ 已暂停（{self.processed_count}/{self.total_count}），'
                     f'在跑的几张会先完成')
            self.status.configure(text=i18n.tr(f'已暂停 {self.processed_count}/{self.total_count}'),
                                  bootstyle='warning')
            self.btn_pause.configure(text=i18n.tr('▶ 继续'), bootstyle='success')
        else:
            self.log(f'▶ 从 {self.processed_count}/{self.total_count} 继续')
            self.status.configure(text=i18n.tr('处理中...'), bootstyle='info')
            self.btn_pause.configure(text=i18n.tr('⏸ 暂停'), bootstyle='warning-outline')
        self.flush_logs()

    def force_stop(self):
        """强制停止：立刻杀掉所有工作进程，不等任何在途任务。"""
        if not self.is_running:
            return
        if not messagebox.askokcancel(
                '强制停止',
                '立刻终止全部工作进程。\n'
                '安全写入开启时会完整撤回，否则已写出的文件会保留。\n\n确定吗？', parent=self.root):
            return

        logfile.op('强制停止', f'{self.processed_count}/{self.total_count}')
        self.cancel_requested = True
        self.force_stopped = True     # 收尾时别再等在途任务，见 process_worker
        self.paused = False
        self.log('⛔ 强制停止：正在终止全部工作进程...')
        killed = self.shutdown_workers()
        self.log(f'   已终止 {killed} 个工作进程' if killed
                 else '   池子里已经没有活着的工作进程了')
        self.status.configure(text=i18n.tr('正在强制停止...'), bootstyle='danger')
        self.sweep_when_idle()
        self.flush_logs()

    # 点「取消」时调用：通知后台别再派新任务；开了安全写入的话，已写好的临时文件会被撤回。
    def request_cancel(self):
        if not self.is_running:
            return

        logfile.op('取消', f'{self.processed_count}/{self.total_count}')
        self.cancel_requested = True
        self.status.configure(text=i18n.tr('正在取消...'), bootstyle='warning')
        self.log('⚠️ 用户请求取消...')

        # 安全写入时所有输出都还在临时文件里，取消后本来就要整批丢弃 —— 那就
        # 没必要干等在途任务把它们写完。直接终止子进程，取消立刻见效。
        # 直写模式下不能这么干：强杀正在写的文件会在目标目录留下截断的输出。
        if self.safe_write.get():
            killed = self.shutdown_workers()
            if killed:
                self.log(f'   已终止 {killed} 个工作进程，正在撤回临时文件...')
            self.sweep_when_idle()
        else:
            self.log('   直写模式：等在途任务写完，以免留下截断的文件')
        self.flush_logs()

    # 点「查看错误」时调用：弹窗列出这一轮处理失败的文件和原因。
    def show_errors(self):
        if not self.error_list:
            messagebox.showinfo('无错误', '没有处理失败的文件', parent=self.root)
            return

        # 复用已经开着的那个窗口，否则每点一次就叠一个位置完全重合的新窗口，
        # 关掉最上面一个看起来像没反应。
        old = getattr(self, 'err_win', None)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.destroy()
            except Exception:
                pass

        err_win = ttk.Toplevel(self.root)
        self.err_win = err_win
        err_win.title(i18n.tr(f'处理失败的文件 ({len(self.error_list)} 个)'))
        # 尺寸跟着 DPI 走、位置跟着父窗口走。原来是写死的 650x450 且不管父窗口
        # 在哪 —— 4K 屏上会甩出去几百像素，高 DPI 下还偏小。
        _s = self.ui_scale()
        ew, eh = int(650 * _s), int(450 * _s)
        self.root.update_idletasks()
        ex = self.root.winfo_x() + (self.root.winfo_width() - ew) // 2
        ey = self.root.winfo_y() + (self.root.winfo_height() - eh) // 3
        ex = max(0, min(ex, self.root.winfo_screenwidth() - ew))
        ey = max(0, min(ey, self.root.winfo_screenheight() - eh))
        err_win.geometry(f'{ew}x{eh}+{ex}+{ey}')
        err_win.transient(self.root)

        io_errors = sum(1 for _, _, t in self.error_list if t == 'io')
        img_errors = sum(1 for _, _, t in self.error_list if t == 'image')
        other_errors = len(self.error_list) - io_errors - img_errors

        summary = ttk.Label(err_win, text=i18n.tr(f'IO错误: {io_errors}  |  图片损坏: {img_errors}  |  其他: {other_errors}'), font=('', 10, 'bold'))
        summary.pack(pady=(10, 5))

        text = scrolledtext.ScrolledText(err_win, font=('Consolas', 10))
        text.pack(fill=BOTH, expand=YES, padx=10, pady=10)

        for i, (path, error, err_type) in enumerate(self.error_list, 1):
            icon = {'io': '💾', 'image': '🖼️', 'unknown': '❓'}.get(err_type, '❓')
            text.insert('end', f'{i}. {icon} {os.path.basename(path)}\n')
            text.insert('end', f'   路径: {path}\n')
            text.insert('end', f'   错误: {error}\n\n')

        text.configure(state='disabled')

    # 【一次处理的起点】点「开始执行」时调用。
    # 这里只做各种检查（路径对不对、会不会覆盖等），全部通过后开一个后台线程去跑
    # process_worker —— 真正的活在那边干，所以处理期间界面不会卡住。
    def start_processing(self):
        if self.is_running:
            return

        src_root = self.src_folder.get()

        is_dropped_mode = bool(self.dropped_paths)

        # ── 检查 1：来源路径有没有效（地址栏可以用分号隔开填多条）──
        if not is_dropped_mode:
            # 地址栏支持一次填多条（分号或换行隔开）。每条各自成根，
            # 各得一个「文件夹名_已处理」—— 跟一次拖进来几个文件夹一个待遇。
            typed = parse_source_paths(src_root)
            if len(typed) > 1:
                self.dropped_roots = typed
                src_root = typed[0]
                self.log(f'📁 {len(typed)} 条源路径：'
                         + '、'.join(os.path.basename(r) for r in typed[:4])
                         + ('…' if len(typed) > 4 else ''))
                # 必须立刻刷。`process_worker` 开头会 `log_queue.clear()`，
                # 在这之前排进队列、又没落到控件上的日志会被整个抹掉。
                self.flush_logs()
            elif typed:
                self.dropped_roots = []
                src_root = typed[0]
            if not src_root or not os.path.exists(src_root):
                messagebox.showerror(
                    '错误',
                    '请选择有效的源文件夹或拖拽图片到窗口。'
                    '要一次处理多个文件夹，可以用分号隔开多条路径。',
                    parent=self.root)
                return
        else:
            if not os.path.exists(src_root):
                src_root = os.path.dirname(self.dropped_paths[0])

        # ── 检查 2：导出位置 ──
        is_export = self.out_mode.get() == 'new'
        if is_export:
            dst_root = self.new_folder_path.get()
            if not dst_root:
                messagebox.showerror('错误', '请选择导出位置', parent=self.root)
                return
        else:
            dst_root = src_root

        # 起始编号绑的是自由输入框，清空后 IntVar.get() 会抛 TclError。原来这个
        # 异常要等到工作线程里的 build_tasks 才炸，弹出一句生硬的 Tcl 报错。
        if self.name_mode.get() != 'keep':
            try:
                self.start_num.get()
            except Exception:
                messagebox.showerror('错误', '起始编号必须是数字', parent=self.root)
                return

        # ── 检查 3：导出目录不能放在来源文件夹里面（否则会把自己的产物当成新图片再扫一遍）──
        #
        # 导出目录落在来源里的话，产物会被下一轮重新扫进来，文件每跑一次
        # 翻一倍、目录一层层套自己。这是能把磁盘写满的量级，直接拦下。
        escape = output_escape_error(src_root, dst_root, is_export)
        if escape:
            logfile.op('拒绝执行', f'导出目录逃逸 | 来源 {src_root} | 导出 {dst_root}')
            messagebox.showerror('导出位置不对', escape, parent=self.root)
            return

        # ── 检查 3.1：算出来的输出目录不能落回来源 ──
        #
        # 检查 3 比的是用户填的顶层「导出位置」；而真正写进去的是
        # `<导出位置>\<源文件夹名><后缀>` —— **后缀留空时它正好等于来源目录
        # 本身**，顶层检查因为导出位置不在来源里而放行。
        # 实测：这样连跑四次，文件数 2 → 4 → 8 → 16，每次都报成功。
        if is_export:
            land = output_lands_in_source(
                self._output_dir_candidates(src_root, dst_root),
                [src_root] + list(self.dropped_roots or []))
            if land:
                logfile.op('拒绝执行：输出目录落回来源', dst_root)
                messagebox.showerror('导出位置不对', land, parent=self.root)
                return

        # ── 检查 3.5：前缀和目录后缀必须是「名字」，不能是路径 ──
        #
        # 这两个值会被直接拼进 os.path.join，而 Windows 下绝对路径会把前面的
        # 导出目录整个丢掉 —— 一个 `D:\别人的目录\victim` 这样的前缀能让产物
        # 落到导出目录之外、把那边的文件覆盖掉，程序还一路报成功。
        # （已复现。）
        for var, label in ((self.prefix_val, '前缀'),
                           (self.dir_suffix_val, '导出目录后缀')):
            bad = name_segment_error(var.get(), label)
            if bad:
                logfile.op('拒绝执行', f'{label} | {var.get()!r}')
                messagebox.showerror('名字填得不对', bad, parent=self.root)
                return

        # ── 检查 4：导出目录里已经有东西时，问一句「合并」还是「另建新目录」──
        #
        # 导出目录里已经躺着上一批的产物时先问一句。原来是直接并进去，于是
        # 连点「一键编号」和「一键压缩」之后，同一个 xxx_已处理 里同时躺着
        # 001.jpg… 和 IMG_0001.jpg…，用户分不清哪个是哪次的。
        if is_export and not self._confirm_output_dirs(src_root, dst_root):
            self.is_running = False
            return

        # 所有设置在 UI 线程里一次性取好再交给工作线程：既避免跨线程读 Tk 变量，
        # 也保证跑的过程中改动界面不会让这一批任务读到半新半旧的配置。
        snapshot = self.snapshot_settings()

        # ── 检查全部通过：把这次的所有设置记进系统日志，方便以后排查 ──
        #
        # 把这一批的完整参数记进日志。用户报"结果不对"时，第一件事就是确认
        # 当时到底是什么设置 —— 截图里往往看不全。
        logfile.op('开始执行', ' | '.join([
            f'来源 {src_root}',
            f'导出 {dst_root}' if is_export else '原地修改',
            f'命名 {self.name_mode.get()}',
            f'旋转 {self.rotate_val.get()}',
            f'画质 {self.quality_val.get()}',
            f'仅压缩 {self.compress_only.get()}',
            f'转ICO {self.is_ico.get()}',
            f'安全写入 {self.safe_write.get()}',
            f'带上不支持 {self.copy_others.get()}',
            f'动图重编码 {self.reencode_anim.get()}',
            f'性能 {self.perf_mode.get()}',
            f'内存上限 {self.mem_limit_val.get()}',
            f'单张超时 {self.timeout_val.get()}',
            f'最长边 {self.max_edge_val.get()}',
        ]))

        # 置位必须在这里（UI 线程），不能等工作线程启动后再做 —— 否则两次快速
        # 点击都能通过上面的 is_running 检查，起两个 worker 同时改同一批文件。
        self.is_running = True
        self.btn_run.configure(state='disabled', text='处理中...')
        self.btn_cancel.configure(state='normal')
        self.btn_pause.configure(state='normal', text='⏸ 暂停',
                                 bootstyle='warning-outline')
        self.btn_stop.configure(state='normal')

        # ── 开后台线程去跑 process_worker，界面继续响应 ──
        threading.Thread(target=self.process_worker,
                         args=(src_root, dst_root, is_export, snapshot),
                         daemon=True).start()

    def _output_dir_candidates(self, src_root, dst_root):
        """这一批会写进哪些「xxx_已处理」目录。只看顶层，够用来做非空检查。"""
        # **和 API 共用同一份算法**（tasks.output_dirs_for）。
        # 原来这里自己算一遍，结果"清空时检查哪些目录"和"实际写进哪些目录"
        # 可能对不上，以前就这么漏过。
        return output_dirs_for(src_root, dst_root, True,
                               bool(self.keep_structure.get()),
                               self.dir_suffix_val.get(),
                               self.dropped_roots) or [dst_root]

    def _confirm_output_dirs(self, src_root, dst_root):
        """导出目录非空就问一句。返回 False 表示用户取消，别开跑。"""
        busy = []
        for d in self._output_dir_candidates(src_root, dst_root):
            try:
                n = len(os.listdir(d))
            except OSError:
                n = 0
            if n:
                busy.append((d, n))
        if not busy:
            return True

        total = sum(n for _, n in busy)
        where = NEWLINE.join(f'  {d}（已有 {n} 个文件）' for d, n in busy[:3])
        choice = self._ask_merge_choice(total, where)
        if choice is None:
            return False

        if choice == 'new':
            # 另建：往后找一个还空着的后缀，_已处理_2、_3……
            base = self.dir_suffix_val.get()
            for i in range(2, 100):
                self.dir_suffix_val.set(f'{base}_{i}')
                if not any(os.path.isdir(d) and os.listdir(d)
                           for d in self._output_dir_candidates(src_root, dst_root)):
                    break
            self.log(f'📁 本批改用后缀「{self.dir_suffix_val.get()}」，与上一批分开')
        elif choice == 'clear':
            # **绝不能删到来源。** 导出位置的默认值就是源文件夹的上一级，
            # 所以平铺模式下「清空导出目录」会直接遍历到源目录本身 ——
            # 这不是边角情况，是默认配置就能踩到：实测复现出源目录
            # 连图片一起被 rmtree 掉，然后弹一句「未找到图片文件」。
            # 清理发生在扫描之前，安全写入和取消撤回都来不及保护输入。
            guarded = protected_paths(
                [src_root] + list(self.dropped_roots or [])
                + list(self.dropped_paths or []))
            removed, kept = 0, []
            for d, _n in busy:
                for name in os.listdir(d):
                    full = os.path.join(d, name)
                    if is_protected(full, guarded):
                        kept.append(full)
                        continue
                    try:
                        if os.path.isdir(full):
                            shutil.rmtree(full, ignore_errors=True)
                        else:
                            os.remove(full)
                        removed += 1
                    except OSError:
                        pass
            self.log(f'🧹 已清空导出目录，删除 {removed} 项')
            if kept:
                # 说出来。悄悄跳过会让用户以为清空了，下一批又撞上同名文件。
                self.log(f'   ⚠️ 跳过 {len(kept)} 项：它们就是来源、'
                         f'或者装着来源，删了等于删掉你的原图')
                for p in kept[:3]:
                    self.log(f'      保留 {p}')
                logfile.op('清空导出目录时保护了来源',
                           ' | '.join(kept[:5]))
        else:
            self.log('📁 本批并入已有的导出目录')
        self.flush_logs()
        return True

    def _ask_merge_choice(self, total, where):
        """三选一：另建 / 并入 / 清空。返回 new|merge|clear|None(取消)。"""
        win = ttk.Toplevel(self.root)
        win.title(i18n.tr('导出目录不是空的'))
        win.transient(self.root)
        win.resizable(False, False)
        result = {'v': None}

        body = ttk.Frame(win, padding=16)
        body.pack(fill=BOTH, expand=YES)
        ttk.Label(body, text=i18n.tr(f'导出目录里已经有 {total} 个文件：'),
                  font=('', 10, 'bold')).pack(anchor='w')
        ttk.Label(body, text=where, foreground='gray', font=('', 9),
                  justify='left').pack(anchor='w', pady=(4, 10))
        ttk.Label(body, text=i18n.tr('两批产物混在一起，之后分不清哪个是哪次的结果。'),
                  foreground='gray', font=('', 9)).pack(anchor='w')

        row = ttk.Frame(body)
        row.pack(fill=X, pady=(14, 0))

        # 用户点了弹窗里的某个按钮：记下选的是哪个，然后关掉弹窗。
        def pick(v):
            result['v'] = v
            win.destroy()

        ttk.Button(row, text=i18n.tr('另建新目录'), bootstyle='success',
                   command=lambda: pick('new')).pack(side='left', padx=(0, 8))
        ttk.Button(row, text=i18n.tr('并入'), bootstyle='secondary',
                   command=lambda: pick('merge')).pack(side='left', padx=(0, 8))
        ttk.Button(row, text=i18n.tr(f'清空后写入（删 {total} 项）'),
                   bootstyle='danger-outline',
                   command=lambda: pick('clear')).pack(side='left')

        win.protocol('WM_DELETE_WINDOW', lambda: pick(None))
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 3
        win.geometry(f'+{max(0, x)}+{max(0, y)}')
        win.grab_set()
        self.root.wait_window(win)
        return result['v']

    def _schedule_prescan(self, *_args):
        """来源变了就预扫一次。防抖 400ms —— 手动输路径时每敲一个字符都会触发。"""
        # 用户换了来源，上一次的结局就过期了，状态栏交还给预扫描
        self._status_owner = 'prescan'
        job = getattr(self, '_prescan_job', None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._prescan_job = self.root.after(400, self._prescan)

    def _prescan(self):
        """扫一下来源，告诉用户「找到 N 个」。

        原来路径填进去之后进度还是 0/0、日志空白、状态「准备就绪」，用户必须
        先点一个真的会写盘的按钮，才知道路径对不对、有几张图。对一个「点了就
        直接干活」的简易模式来说，这一步反馈不能少。
        """
        self._prescan_job = None
        if self.is_running:
            return
        if self.dropped_paths:
            n = len(self.dropped_paths)
            self.status.configure(text=i18n.tr(f'已选 {n} 个文件，可以开始'), bootstyle='info')
            self.lbl_progress.configure(text=i18n.tr(f'0/{n}'))
            return

        root_dir = self.src_folder.get()
        if not root_dir or not os.path.isdir(root_dir):
            return

        # 扫描放后台：网络盘或几千个文件的目录会卡住 UI 线程。
        # **只取扫描真正需要的字段**，别拿整份快照 —— 整份会读起始编号那个
        # IntVar，用户把输入框清空准备重输时 `IntVar.get()` 抛 TclError，
        # 预扫描整个崩在 Tk 回调里。而 Tk 回调异常默认只打到 stderr，
        # windowed 打包版连 stderr 都没有，所以这个 bug 在界面上只表现为
        # 「预扫描偶尔不出数」，日志里一个字都没有。
        snap = self._scan_only_settings()

        # 工作线程只计数，不调用 Tk：after() 也需要主事件循环，用户关闭窗口
        # 或调用者仅用 update() 驱动界面时，后台 after() 会阻塞并抛 RuntimeError。
        # 由界面线程轮询结果；窗口销毁后，后台扫描仍可自然结束而不访问 Tk。
        result = Future()

        def work():
            try:
                files = scan_files(root_dir, snap, None)
            except Exception:
                result.set_result(None)
                return
            result.set_result(len(files))

        def show_result():
            if not result.done():
                self.root.after(50, show_result)
                return
            count = result.result()
            if count is not None:
                self._prescan_done(root_dir, count)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(50, show_result)

    # 选好文件夹后，后台数完文件，回到界面线程更新状态栏「找到 N 个文件」。
    def _prescan_done(self, path, count):
        # 扫的过程中用户可能又改了路径，过期结果直接丢掉
        if self.is_running or self.src_folder.get() != path:
            return
        # 跑完的结局优先。预扫描是 400ms 防抖 + 后台线程，很容易在一次
        # **快速失败**之后才回来，一句「找到 N 个文件，可以开始」就把
        # 「出错了，详见日志」冲掉了 —— 用户只剩"点了没反应"这个印象。
        if self._status_owner == 'run':
            return
        if count:
            self.status.configure(text=i18n.tr(f'找到 {count} 个文件，可以开始'),
                                  bootstyle='info')
        else:
            self.status.configure(text=i18n.tr('这个文件夹里没有可处理的文件'),
                                  bootstyle='warning')
        self.lbl_progress.configure(text=i18n.tr(f'0/{count}'))

    # 界面回调里逃出来的异常，按发生顺序记在这里。
    # 测试拿它断言「这一轮界面操作没有把异常吞掉」——
    # 光看退出码是看不出来的，Tk 把异常接住之后程序照常继续跑。
    TK_ERRORS = []

    def _on_tk_error(self, exc_type, exc_value, tb):
        """Tk 回调里没人接住的异常：记进日志，并在界面上说一句。

        装进 `IMGWB_TK_STRICT=1` 的环境里跑时，还会往 stdout 打一行 `FAIL`，
        让测试跑批能把它当失败看见（`tests/run_all.py` 会设这个变量）。
        生产环境不打 —— 用户要的是日志，不是控制台。
        """
        text = ''.join(traceback.format_exception(exc_type, exc_value, tb))
        ImageProcessorApp.TK_ERRORS.append(text)
        logfile.write(f'[界面回调异常] {exc_type.__name__}: {exc_value}',
                      level='ERROR')
        logfile.write(text, level='ERROR')
        try:
            self.log(f'⚠️ 界面出错：{exc_type.__name__}: {exc_value}'
                     f'（完整调用栈已写进日志）')
            self.flush_logs()
        except Exception:
            pass                       # 日志框可能还没建好或已销毁
        if os.environ.get('IMGWB_TK_STRICT') == '1':
            print(f'FAIL 界面回调异常 {exc_type.__name__}: {exc_value}',
                  flush=True)

    def _scan_only_settings(self):
        """预扫描用的最小快照。

        `scan_files` 要用的是这四样：`dropped_paths`、`dropped_roots`、
        `scan_subdirs`、`copy_others`。预扫描没必要去读起始编号、画质那些 ——
        读了反而会被一个空输入框掀翻（见 `_prescan` 里的说明）。

        **四样一个都不能少。** 第一版只给了前两样，于是：
        * 漏 `copy_others` → `scan_files` 里 `getattr` 取不到按 False 算，
          「不支持的文件也带走」勾着也不计数，预扫描报 0/1 而实际要处理 2 个；
        * 漏 `dropped_roots` → 一次拖进来几个文件夹时只数第一个。
        这是一次修复时引入的回归。**加字段时回头看一眼
        `scan_files` 到底读了什么**，别照着记忆写。

        后台线程不能直接碰 Tk 变量，所以这里也要先冻成普通值。
        """
        class _Frozen:
            __slots__ = ('v',)

            def __init__(self, v):
                self.v = v

            def get(self):
                return self.v

        snap = type('ScanSettings', (), {})()
        snap.dropped_paths = list(self.dropped_paths)
        snap.dropped_roots = list(self.dropped_roots)
        snap.scan_subdirs = _Frozen(bool(self.scan_subdirs.get()))
        snap.copy_others = _Frozen(bool(self.copy_others.get()))
        return snap

    def snapshot_settings(self):
        """把界面上的设置冻结成一份快照，接口和 Tk 变量一致（都有 .get()）。"""
        # 把某个设置值「冻住」。后台线程不能直接读界面控件（Tk 只允许界面线程碰它），
        # 所以开跑前把每个设置抄一份放进来，后台读的是这份抄件。
        class _Frozen:
            __slots__ = ('v',)

            # 存下当时的值。
            def __init__(self, v):
                self.v = v

            # 跟界面变量一样用 .get() 取值，拿到的是冻住的那份。
            def get(self):
                return self.v

        names = ('scan_subdirs', 'start_num', 'digit_val', 'prefix_val', 'name_mode',
                 'out_mode', 'keep_structure', 'dir_suffix_val', 'ico_mode_val',
                 'ico_format_val', 'rotate_val', 'quality_val', 'compress_only',
                 'is_ico', 'safe_write', 'copy_others', 'reencode_anim',
                 'max_workers', 'perf_mode', 'mem_limit_val', 'timeout_val',
                 'max_edge_val')

        snap = type('Settings', (), {})()
        for n in names:
            snap.__dict__[n] = _Frozen(getattr(self, n).get())
        snap.dropped_paths = list(self.dropped_paths)
        snap.dropped_roots = list(self.dropped_roots)
        return snap

    # 【真正干活的地方】在后台线程里跑完整一轮。
    # 大致顺序：扫描 → 定文件名 → 识别每张图 → 开工作进程 → 派发/等待/收结果 → 收尾落盘 → 写总结。
    # 函数很长，下面每一段都用「第 N 步」标了路标，按顺序往下读即可。
    def process_worker(self, src_root: str, dst_root: str, is_export: bool, cfg=None):
        cfg = cfg if cfg is not None else self
        crashed = False
        self._run_id += 1
        self.is_running = True
        self.cancel_requested = False
        self.force_stopped = False
        self.paused = False
        self.error_list = []
        self.processed_count = 0
        self.total_count = 0      # 不清零的话，下一次跑空文件夹会显示成 0/上次的总数
        self.log_queue.clear()
        staged = []
        tasks = []           # 先占位：出错时 except 分支要用它算清扫目录
        started_at = time.time()

        # 运行按钮已经在 start_processing 里（UI 线程）禁用了，这里只需重置错误计数
        self.root.after(0, lambda: self.btn_errors.configure(state='disabled', text='📋 查看错误 (0)'))

        self.log('========================================')
        self.log('开始扫描文件...')
        self.root.after(0, self.flush_logs)

        try:
            # ━━━━ 第 1 步：扫描来源，列出这一轮要处理的全部文件 ━━━━
            all_files = scan_files(src_root, cfg, self.log)

            if not all_files:
                self.root.after(0, lambda: messagebox.showwarning('空', '未找到图片文件', parent=self.root))
                return

            # PNG 在 85% 档会被量化成 256 色。高级模式底部有那行警告，简易模式
            # 看不到，而「一键压缩」走的正是这一档 —— 至少在日志里说一声。
            png_count = sum(1 for f in all_files if f.lower().endswith('.png'))
            if png_count and '85%' in cfg.quality_val.get() and cfg.compress_only.get():
                self.log(f'⚠️ 本批有 {png_count} 个 PNG，网络压缩档会把它们量化成 '
                         f'256 色（有损，可能出现色带）')

            heic_count = sum(1 for f in all_files if f.lower().endswith(('.heic', '.heif')))
            if heic_count > 0:
                self.log(f'找到 {len(all_files)} 个图片 (含 {heic_count} 个 HEIC)')
            else:
                self.log(f'找到 {len(all_files)} 个图片文件')

            scan_secs = time.time() - started_at
            # ━━━━ 第 2 步：给每个文件定好「输出到哪、改成什么名字」 ━━━━
            # 编号在这一步就定死了，后面怎么调度顺序都不影响文件名。
            tasks = build_tasks(all_files, src_root, dst_root, cfg)
            plan_secs = time.time() - started_at - scan_secs

            # 开工前按**排好的任务**再核一遍落点。start_processing 的检查 3.1
            # 是按参数推算的；推算和实际对不上正是两次漏掉的地方，
            # 所以这里以真正要写的路径为准再兜一道。这时一个字节都还没写。
            if is_export:
                land = planned_output_lands_in_source(
                    tasks, [src_root] + list(
                        getattr(cfg, 'dropped_roots', None) or []))
                if land:
                    logfile.op('拒绝执行：输出目录落回来源', dst_root)
                    self.log(f'❌ 错误: {land}')
                    self.root.after(0, lambda: messagebox.showerror(
                        '导出位置不对', land, parent=self.root))
                    tasks = []
                    return

            self._output_dirs = {os.path.dirname(t.dst) for t in tasks}

            # 开工前先看一眼这些目录里有没有以前留下的东西。
            # 能确定是残骸的（本实例的、主人已经不在的）顺手清掉 —— 以前只在
            # 取消/超时/出错时才清扫，崩溃留下的要等下一次"碰巧"才会被收走。
            # 判断不了的不删，但**要说出来**：它们不会被自动清理，没人提的话
            # 就永远躺在用户的照片文件夹里。备份文件更要说 —— 那是原文件。
            try:
                swept = sweep_temps(self._output_dirs)
                left = find_leftovers(self._output_dirs)
            except Exception:
                swept, left = 0, {}
            if swept:
                self.log(f'🧹 开工前清理了 {swept} 个以前留下的临时文件')
            if left.get('backups'):
                self.log(f'⚠️ 发现 {len(left["backups"])} 个备份文件（名字里带 '
                         f'.imgwb-backup-）。它们是**原文件**：上次处理在最后一步'
                         f'被打断时留下的，请确认后手工改回原名。例如 '
                         f'{left["backups"][0]}')
            if left.get('unknown'):
                self.log(f'ℹ️ 发现 {len(left["unknown"])} 个来历不明的临时文件'
                         f'（名字里带 .~imgwb）：可能是另一台电脑正在处理，'
                         f'也可能是很久以前留下的。程序不会自动删除它们。例如 '
                         f'{left["unknown"][0]}')
            if swept or left.get('backups') or left.get('unknown'):
                logfile.op('开工前检查残留',
                           f'清理 {swept} 个，备份 {len(left.get("backups", []))} 个，'
                           f'来历不明 {len(left.get("unknown", []))} 个')
                self.root.after(0, self.flush_logs)

            self.total_count = len(tasks)
            self.root.after(0, lambda: self.progress.configure(maximum=self.total_count, value=0))
            self.root.after(0, lambda: self.status.configure(text=i18n.tr(f'处理中: 0/{self.total_count}'), bootstyle='info'))

            perf_mode = cfg.perf_mode.get()
            workers = perf.worker_count(perf_mode, cfg.max_workers.get())
            priority = perf.priority_of(perf_mode)
            use_multiprocess = len(tasks) > PROCESS_THRESHOLD

            self.log(f'性能模式 — {perf.describe(perf_mode, cfg.max_workers.get())}')
            self.log(f'使用{"多进程" if use_multiprocess else "多线程"}模式')

            # ━━━━ 第 3 步：算内存上限：同时正在解码的图，加起来最多占多少 MB ━━━━
            #
            # 内存预算要在预扫描之前算好 —— 预扫描给的「预计耗时」取决于
            # 闸门同时放得下几张，而那正是由预算决定的。
            explicit_mb = dict(perf.MEMORY_LIMIT_CHOICES).get(
                cfg.mem_limit_val.get(), 0)
            budget_mb = perf.memory_budget_mb(perf_mode, explicit_mb)
            self._budget_mb = budget_mb

            # ━━━━ 第 4 步：识别每个文件 ━━━━
            # 认出真实格式、尺寸、会不会真的重新编码、大概要占多少内存；
            # 并在日志里报出「处理范围 / 预计耗时 / 有问题的文件」。
            #
            # ---- 建池子之前，先把整批文件认一遍 ----------------------
            # 只读文件头，几毫秒一张，**用线程池**：识别是 I/O 为主，
            # 走线程免掉 pickle 和进程启动，也不占子进程的回收额度。
            # 更要紧的是它必须在建池子之前 —— 「多久回收一次子进程」要按
            # 这批图的平均大小定，而那正是识别的产物。
            #
            # 三个用处：明确范围（真要编码 / 原样复制 / 带过去各多少）、
            # 给出预计耗时、以及**提前把问题说出来**（打不开的、扩展名和
            # 真实格式对不上的）。顺带把 est_mb 一次算好，闸门直接取用 ——
            # 主线程不用再一张张 probe。
            scan_t0 = time.time()
            self.log('🔍 正在识别文件…')
            self.root.after(0, self.flush_logs)
            self._file_info = {}
            infos = self._identify_all(tasks, workers, budget_mb)
            self._file_info = {i.path: i for i in infos}
            avg_peak = (sum(i.est_mb for i in infos) / len(infos)) if infos else 0.0
            if use_multiprocess and priority == 'background':
                self.log('   子进程已降到后台优先级，磁盘 I/O 让给前台程序')
            self.root.after(0, self.flush_logs)

            # ━━━━ 第 5 步：开工作进程池 ━━━━
            # 真正处理图片的是这些子进程，个数 = 并发数；文件少时改用线程。
            if use_multiprocess:
                # initializer 在每个子进程起来时跑一次，把自己的优先级降下来。
                # max_tasks_per_child 是内存的关键：子进程默认一直复用，Pillow
                # 解过一张 6000x4000 之后 RSS 就再也降不回来，跑几千张下来每个
                # 进程都撑到几百 MB。定期换新进程才能把内存还给系统。
                kwargs = dict(max_workers=workers,
                              initializer=perf.init_worker, initargs=(priority,))
                if 'max_tasks_per_child' in inspect.signature(
                        ProcessPoolExecutor.__init__).parameters:
                    # 按这批图的平均大小定回收频率。图小的时候回收纯属白烧，
                    # 而且 8 个工人会同时撞线、一起重建 —— 见 perf 里的说明。
                    per_child = perf.tasks_per_child(workers, avg_peak,
                                                     budget_mb)
                    kwargs['max_tasks_per_child'] = per_child
                    if per_child != perf.TASKS_PER_CHILD:
                        self.log(f'   子进程每处理 {per_child} 张才回收'
                                 f'（这批图平均预估 {avg_peak:.0f} MB，'
                                 f'回收得太勤反而拖慢）')
                else:
                    self.log('   （当前 Python 不支持子进程回收，内存占用会偏高，'
                             '建议升级到 3.11+）')
                executor = ProcessPoolExecutor(**kwargs)
            else:
                executor = ThreadPoolExecutor(max_workers=workers)
            self.executor = executor

            handled = set()
            last_ui_update = time.time()
            last_log_flush = time.time()
            last_log_mark = 0
            last_mem_check = 0
            mem_warned = False
            log_every = max(50, len(tasks) // 20)     # 大约每 5% 记一条

            # ━━━━ 第 6 步：准备调度：一次最多派出去多少个、按什么顺序派（大图先开工） ━━━━
            #
            # 在飞任务数上限。原来是把全部 5731 个任务一次性 submit 出去，
            # 队列和 future 全堆在内存里；限流之后内存只跟并发数走，跟总量无关。
            # 顺带让「暂停」有了着力点 —— 停止派发即可。
            window = max(workers * 3, workers + 4)

            # 重排派发顺序，见 `_order_for_throughput`。
            if self._file_info:
                tasks = self._order_for_throughput(tasks, workers,
                                                   budget_mb)

            pending = iter(tasks)
            in_flight = {}
            exhausted = False

            # 内存闸门。把内存打爆的不是任务总数，而是几个工作进程**同时**抓到
            # 巨图：8000x8000 一张解出来就是几百 MB，八个一起就是四五个 GB，
            # 而且是一两秒内冲上去的。所以按「正在解码的图有多大」限流，
            # 而不是只按任务条数。
            weights = {}
            submitted_at = {}          # 每个任务是什么时候派出去的
            began_at = {}              # 每个任务是什么时候真正开工的
            in_flight_mb = 0.0
            self.peak_in_flight_mb = 0.0   # 给测试和诊断看的
            next_task = None
            next_mb = 0.0
            # 候选缓冲：预扫描把每张图的预估内存都算好了，所以派发时可以
            # 「挑一张放得下的」而不是死等队首。前瞻长度取窗口的 8 倍 ——
            # 只是存着任务元组，内存可以忽略，但足够跨过一长串连续的巨图。
            LOOKAHEAD = max(window * 8, 64)
            ready = []
            skipped_ahead = 0      # 有多少次是靠「往后挑」才没停摆的
            self._skipped_ahead = 0    # 给测试和诊断看
            last_done_at = time.time()
            last_progress_log = time.time()
            slow_logged = 0
            big_logged = 0
            kept_count = 0        # 压缩反而更大、保留了原文件的张数
            resized_count = 0     # 因为「最长边不超过」被缩过的张数
            # 把每张图在工人里的分段耗时累加起来。跑完拿它跟「墙上时间 × 并发数」
            # 一比，就能一句话回答"这五分钟到底花在哪" —— 是真在解码编码，
            # 还是卡在排队、进程回收、磁盘或杀毒软件上。
            spent_in_workers = {}
            bytes_done = 0        # 实际读写过的字节，用来换算真实吞吐
            flattened = 0         # 动画被压成单帧的张数（目标格式存不下多帧）

            # ━━━━ 第 7 步：准备进度播报和「剩余约 X」的算法 ━━━━
            # （这里只是先定义好，主循环里每隔一阵才会调用）
            def progress_line():
                """一行进度。进度条停住的时候，用户要的是「到哪了、还剩多久」，
                光看条不动只能猜。

                **剩余时间按剩余的「工作量」算，不按剩余张数。** 一批图里
                大小能差两个数量级，按张数算等于假设每张一样贵 —— 用户那次
                4777 张，89% 时还报「剩余约 0.1 分钟」，实际又跑了 3 分钟，
                因为剩下的全是巨图。预扫描已经把每张的像素数认出来了，
                拿它当工作量，剩余时间才有意义。

                复制/跳过的那些便宜得多，按经验打到两成 —— 它们不解码，
                只是读写一遍。
                """
                c, t = self.processed_count, self.total_count
                pct = c * 100 // max(t, 1)
                spent = time.time() - started_at
                rate = c / spent if spent > 0 else 0
                left = 0.0
                if cost_left > 0 and spent > 0:
                    # **实测校准**：先验只是标定值，机器快慢、杀软、盘速都会
                    # 让它整体偏一个系数。拿「已经花掉的工人秒 ÷ 这些图的先验
                    # 成本」把系数量出来，再乘到剩下的成本上。
                    real = spent_in_workers.get('total', 0.0)
                    calib = (real / cost_done) if cost_done > 0.5 else 1.0
                    calib = min(max(calib, 0.3), 3.0)

                    # **分相取上界**。剩下的重活只能按闸门允许的并发跑，轻活
                    # 能跑满并发，两者是重叠进行的，所以取较大的那个：
                    #   重活相：重活成本 / 闸门允许的并发
                    #   整体相：全部剩余成本 / 并发数
                    # 少了这一条就会在「小图跑完、只剩巨图」时严重低估 ——
                    # 用户那次 89% 报「剩余 0.1 分钟」、实际又跑了 3 分钟。
                    conc_heavy = float(workers)
                    if heavy_n > 0:
                        avg_heavy = heavy_peak_sum / heavy_n
                        if avg_heavy > 0:
                            conc_heavy = max(1.0, min(float(workers),
                                                      budget_mb / avg_heavy))
                    left = max(heavy_cost_left / conc_heavy,
                               cost_left / max(workers, 1)) * calib
                elif rate > 0:
                    left = (t - c) / rate
                # 不到 90 秒就用秒。原来一律按分钟显示到小数点后一位，
                # 十几秒和两秒都是「0.0 分钟」，等于没说。
                human = f'{left:.0f} 秒' if left < 90 else f'{left / 60:.1f} 分钟'
                msg = (f'进度 {c}/{t} ({pct}%)  {rate:.1f} 张/秒  '
                       f'剩余约 {human}  在处理 {len(in_flight)} 个')
                if self.error_list:
                    msg += f'  失败 {len(self.error_list)}'
                return msg
            anim_carried = 0
            # 估这张图要占一个工人多少秒（按像素数和格式算），用来算「剩余约 X」。
            #
            # 每张图的**先验成本**，单位是「工人秒」：预扫描已经知道尺寸和
            # 格式，按标定过的像素吞吐就能算出它大概要占一个工人多久。
            # 复制/跳过的不解码，按字节走磁盘算。
            def _cost_of(info):
                if info.plan == 'process':
                    mpx = info.width * info.height / 1e6
                    return mpx / SCAN_RATE_MPXPS.get(info.fmt,
                                                     DEFAULT_RATE_MPXPS)
                return info.size_bytes / 1048576 / COPY_RATE_MBPS

            # 「重活」的判据跟派发那边一致：占掉的额度超过一个工人应得的一份。
            _share = max(budget_mb / max(workers, 1), 1.0)
            cost_left = 0.0          # 还没跑的先验成本合计（工人秒）
            cost_done = 0.0
            heavy_cost_left = 0.0    # 其中属于重活的那部分
            heavy_peak_sum = 0.0
            heavy_n = 0
            for _i in self._file_info.values():
                _c = _cost_of(_i)
                cost_left += _c
                if _i.est_mb > _share:
                    heavy_cost_left += _c
                    heavy_peak_sum += _i.est_mb
                    heavy_n += 1
            self.log(f'内存上限 {budget_mb:.0f} MB'
                     + ('（手动设定）' if explicit_mb else '（自动）'))

            # ━━━━ 第 8 步：单张超时的设置 ━━━━
            #
            # 单张超时：超过就放弃处理、原样复制。实测一张 3650x3650 走完整条
            # 流水线只要 0.14 秒，所以撞到这个门槛的一定不是「图太大」，而是卡在
            # 别处（磁盘、杀软、换页）。与其整批陪着它耗，不如把它带走。
            timeout_sec = dict(TIMEOUT_CHOICES).get(cfg.timeout_val.get(), 0)
            # 线程模式下杀不掉一个跑着的线程，超时只能干等 —— 说清楚，别假装有。
            if timeout_sec and not use_multiprocess:
                self.log(f'ℹ️ 单张超时只在多进程模式下有效'
                         f'（超过 {PROCESS_THRESHOLD} 个文件才启用），本次不生效')
                timeout_sec = 0
            elif timeout_sec:
                self.log(f'单张超时 {timeout_sec} 秒，超过就改名直接复制')
            demoted = set()        # 已经因超时降级过的源文件，避免来回打转
            timeout_hits = 0

            # ━━━━ 第 9 步：主循环 —— 一直转到全部做完、或被取消/强停 ━━━━
            # 每一圈做三件事：
            #   ① 派发：只要还有空名额，就挑一张内存放得下的图交给工人
            #   ② 检查超时：真在跑、却超过门槛的图，改成原样复制
            #   ③ 等结果：最多等 0.2 秒，把做完的收回来、更新进度
            #
            # 这里不能用 `with executor:`。它的 __exit__ 是 shutdown(wait=True)，
            # 会一直等在途任务跑完 —— 用户点了「强制停止」，界面却卡在
            # 「正在强制停止...」几分钟不动，正是卡在这一句里。
            # 现在按情况决定等不等：
            #   正常跑完      -> 等，让在途任务写完
            #   取消（直写）  -> 等，否则会留下截断的文件
            #   强制停止      -> 不等，立刻放手
            try:
                while True:
                    # ── 主循环 ①：派发 ──
                    #
                    # 补足在飞任务；暂停或已取消时不再派发
                    # `not exhausted or ready` —— **两个都要**。
                    # 只看 exhausted 的话，pending 一取空就不再派发，而候选
                    # 缓冲里可能还压着几十张，那些图会被静默丢掉。
                    while ((not exhausted or ready) and not self.paused
                           and not self.cancel_requested
                           and len(in_flight) < window):
                        # 候选缓冲填满，才好「挑一张放得下的」
                        while len(ready) < LOOKAHEAD and not exhausted:
                            try:
                                t = next(pending)
                            except StopIteration:
                                exhausted = True
                                break
                            # 不旋转的多帧图只会被复制，峰值内存约等于 0，
                            # 不能让它白占闸门额度 —— 否则两个「预估 737 MB」
                            # 的动图就能把 2 GB 的闸门占满，而它们其实只是
                            # 在做文件复制。
                            if t.copy_only:
                                mb, frames = 0.0, 1
                            else:
                                # 预扫描已经把每张图认过一遍了，直接取；
                                # 万一没有（预扫描被取消打断）才现算。
                                got = self._file_info.get(t.src)
                                if got is not None:
                                    mb, frames = got.est_mb, got.frames
                                else:
                                    # 目标扩展名要一起传：扩展名和真实格式
                                    # 不符的文件峰值由目标那一侧决定。
                                    mb, frames = probe_peak(t.src, t.config,
                                                            t.dst)
                            if frames > 1 and mb == 0.0:
                                anim_carried += 1
                                if anim_carried <= 3:
                                    self.log(
                                        f'   ⓘ 动图 {os.path.basename(t.src)}'
                                        f'（{frames} 帧）不旋转，原样带过、'
                                        f'不重编码')
                            ready.append((t, mb))

                        if not ready:
                            break

                        # **挑一张现在放得下的，而不是死等队首那张。**
                        # 严格按顺序派发的话，队首是张巨图、额度又不够时，
                        # 整批就停摆了 —— 后面明明还有一堆小图能立刻开工，
                        # 工人却全闲着。预扫描已经把每张的预估内存算好了，
                        # 挑一张是 O(前瞻长度) 的事，几乎不要钱。
                        # 输出文件名在 build_tasks 阶段就定死了，跟派发顺序
                        # 无关，所以重排不会影响编号。
                        pick = -1
                        for idx, (_t, mb) in enumerate(ready):
                            if not in_flight or in_flight_mb + mb <= budget_mb:
                                pick = idx
                                break
                        if pick < 0:
                            # 缓冲里没有一张放得下 —— 那才是真该等了
                            break

                        next_task, next_mb = ready.pop(pick)
                        if pick > 0:
                            skipped_ahead += 1
                            self._skipped_ahead = skipped_ahead

                        if next_mb > 400 and big_logged < 8:
                            big_logged += 1
                            self.log(f'   ⓘ 大图 {os.path.basename(next_task.src)} '
                                     f'预计占用 {next_mb:.0f} MB，已按闸门限流')

                        fut = executor.submit(process_single_image, next_task)
                        in_flight[fut] = next_task
                        weights[fut] = next_mb
                        submitted_at[fut] = time.time()
                        in_flight_mb += next_mb
                        self.peak_in_flight_mb = max(self.peak_in_flight_mb,
                                                     in_flight_mb)
                        next_task = None

                    # 记下当前这批工作进程的 pid。子进程会被 max_tasks_per_child
                    # 不停换新，收尾时池子那份名单已经被清空 —— 只有自己攒的这份
                    # 还认得它们，关窗口时才不会留下一堆孤儿进程。
                    self._remember_children()

                    # ── 主循环 ②：检查超时 ──
                    #
                    # 超时的任务：放弃处理，改成原样复制重新排队。
                    # 跑着的任务没法单独取消，只能把整个池子换掉 —— 被牵连的
                    # 在途任务原样重排，不算失败、也不会重复计数。
                    if timeout_sec and in_flight:
                        now = time.time()
                        # 超时只能算「真正在跑」的时间。窗口是并发数的 3 倍，
                        # 多出来的那些只是躺在进程池的内部队列里排队 —— 把排队
                        # 时间也算进去的话，排队约 2× 单张耗时、自己再跑 1×，
                        # **任何真实耗时超过门槛 1/3 的图都会被误判**（默认门槛
                        # 120 秒，也就是超过约 35 秒的图必中）。用户报的
                        # 「太大的图直接罢工、等 120 秒过了再处理」就是这个。
                        #
                        # 进程池是先进先出派发的，所以此刻真正在跑的，就是
                        # 「最早提交、还没完成」的那 workers 个。它们进入这个
                        # 集合的时刻就是开工时刻，误差不超过一次轮询（0.2 秒）。
                        running = sorted(
                            in_flight, key=lambda f: submitted_at.get(f, 0)
                        )[:workers]
                        for f in running:
                            began_at.setdefault(f, now)
                        late = [f for f in running
                                if now - began_at.get(f, now) > timeout_sec]
                        if late:
                            timeout_hits += len(late)
                            requeue = []
                            for f in late:
                                t = in_flight[f]
                                secs = now - began_at.get(f, now)
                                if t.copy_only or t.src in demoted:
                                    # 连复制都超时，那就不是图片的问题了
                                    self.error_list.append(
                                        (t.src, f'超过 {secs:.0f} 秒仍未完成，'
                                                f'复制也没成功', 'timeout'))
                                    self.processed_count += 1
                                    continue
                                demoted.add(t.src)
                                self.log(f'   ⏱ {os.path.basename(t.src)} '
                                         f'超过 {timeout_sec} 秒还没好，'
                                         f'放弃处理、改名直接复制')
                                requeue.append(t._replace(copy_only=True))
                            # 同一个池子里其它在途任务的工人马上要被杀，原样重排
                            for f in in_flight:
                                if f not in late:
                                    requeue.append(in_flight[f])

                            self.shutdown_workers()
                            in_flight.clear()
                            weights.clear()
                            submitted_at.clear()
                            began_at.clear()
                            in_flight_mb = 0.0
                            # 被杀的工人可能留下半截临时文件，按命名规律扫掉。
                            # **必须排除已完成任务的临时文件** —— 安全写入下
                            # 它们就躺在同一个目录里等提交，长得跟半截文件一样。
                            sweep_temps(self._output_dirs,
                                        exclude=[r.tmp_path for r in staged])

                            executor = ProcessPoolExecutor(**kwargs)
                            self.executor = executor
                            pending = chain(requeue, pending)
                            exhausted = False
                            next_task = None
                            self.log(f'   已重建工作进程池，{len(requeue)} 个任务重排')
                            self.root.after(0, self.flush_logs)
                            continue

                    # 心跳进度：不管有没有任务完成，最多隔 PROGRESS_HEARTBEAT_SEC
                    # 就报一次。原来只有「每完成 5% 记一条」，几千张的批量里一条
                    # 要等一百多张 —— 一旦慢下来日志就是几分钟的空白。
                    if time.time() - last_progress_log >= PROGRESS_HEARTBEAT_SEC:
                        last_progress_log = time.time()
                        last_log_mark = self.processed_count
                        self.log('⏱ ' + progress_line())
                        self.root.after(0, self.flush_logs)

                    # ── 用户点了取消：立刻跳出主循环 ──
                    #
                    # 取消后必须立刻跳出，不能等 in_flight 排空：那些任务的
                    # 子进程可能已经被强杀，future 永远不会有结果，等下去就是
                    # 空转到天荒地老。已完成的结果由 with 之后的收尾循环捡回来。
                    if self.cancel_requested:
                        break

                    if not in_flight:
                        if exhausted and not ready:
                            break
                        time.sleep(0.05)      # 暂停中，空转等着
                        self._pump_pause_ui()
                        continue

                    # ── 主循环 ③：等结果（顺带：太久没动静就在日志里报「卡在哪」）──
                    done, _ = futures_wait(in_flight, timeout=0.2,
                                           return_when=FIRST_COMPLETED)
                    if not done:
                        # 卡住时要说得出卡在哪 —— 光看进度条停住毫无信息。
                        # 隔一段就把还没跑完的文件连同它们的预估内存列出来。
                        if time.time() - last_done_at > STALL_REPORT_SEC:
                            last_done_at = time.time()
                            now = time.time()
                            busy = sorted(
                                ((weights.get(f, 0.0),
                                  now - began_at.get(f, submitted_at.get(f, now)), t)
                                 for f, t in in_flight.items()),
                                key=lambda x: x[1], reverse=True)[:5]
                            self.log(f'⏳ 已 {STALL_REPORT_SEC} 秒没有任务完成，'
                                     f'{len(in_flight)} 个还在处理：')
                            for mb, secs, t in busy:
                                self.log(f'     {os.path.basename(t.src)}  '
                                         f'预估 {mb:.0f} MB  已跑 {secs:.0f} 秒')
                            # 卡住时更要报进度，不然日志里只有一串「没有完成」
                            self.log('     ' + progress_line())
                            last_progress_log = time.time()
                            self.root.after(0, self.flush_logs)
                        self._pump_pause_ui()
                        continue

                    last_done_at = time.time()

                    # ── 主循环 ③（续）：逐个收回做完的结果：记成功 / 失败 / 保留原文件，累计进度和耗时 ──
                    for future in done:
                        task = in_flight.pop(future)
                        in_flight_mb -= weights.pop(future, 0.0)
                        _fi = self._file_info.get(task.src)
                        if _fi is not None:
                            _c = _cost_of(_fi)
                            cost_left -= _c
                            cost_done += _c
                            if _fi.est_mb > _share:
                                heavy_cost_left -= _c
                                heavy_peak_sum -= _fi.est_mb
                                heavy_n = max(heavy_n - 1, 0)
                        sent_at = submitted_at.pop(future, None)
                        began_at.pop(future, None)
                        handled.add(future)
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = TaskResult(False, task[0], task[1],
                                                f'任务异常终止: {exc}', 'unknown')
                        self.processed_count += 1

                        # 慢任务要说清楚慢在哪一段。一张 3650x3650 走完整条流水线
                        # 实测 0.14 秒，所以几十秒的任务肯定不是在算图 —— 把工人
                        # 里的分段耗时和墙上时间一起打出来，就能分辨是解码慢、
                        # 写盘慢，还是压根没在这个函数里（排队、进程回收、传输）。
                        wall = time.time() - sent_at if sent_at else 0.0
                        if wall >= SLOW_TASK_SEC and slow_logged < 12:
                            slow_logged += 1
                            tm = result.timings or {}
                            inside = tm.get('total', 0.0)
                            parts = i18n.join([
                                f'{i18n.tr(label)} {tm[key]:.1f}s'
                                for key, label in (('open', '打开'),
                                                   ('decode', '解码'),
                                                   ('encode', '编码写盘'),
                                                   ('copy', '复制'))
                                if key in tm], '、')
                            self.log(
                                f'   🐌 {os.path.basename(result.src_path)} '
                                f'用了 {wall:.0f} 秒，其中在工人里 {inside:.1f} 秒'
                                + (f'（{parts}）' if parts else ''))
                            if wall - inside > SLOW_TASK_SEC:
                                self.log(
                                    f'      有 {wall - inside:.0f} 秒不在图像处理上 —— '
                                    f'排队、子进程回收或结果传输，不是图片本身慢')

                        try:
                            bytes_done += os.path.getsize(result.tmp_path
                                                          or result.dst_path)
                        except OSError:
                            pass
                        for _k, _v in (result.timings or {}).items():
                            spent_in_workers[_k] = spent_in_workers.get(_k, 0.0) + _v

                        if result.success:
                            staged.append(result)
                            if getattr(result, 'kept_original', False):
                                kept_count += 1
                            if getattr(result, 'frames_dropped', 0):
                                flattened += 1
                            if getattr(result, 'resized', False):
                                resized_count += 1
                        else:
                            self.error_list.append(
                                (result.src_path, result.error_msg, result.error_type))
                            # 界面上只列前 20 条，但**文件日志一条不落** ——
                            # 出问题时最需要的往往正是被折叠掉的那些。
                            logfile.write(
                                f'[失败] {result.src_path} | {result.error_type} | '
                                f'{result.error_msg}', 'WARN')
                            # 技术细节（异常类型、调用栈、文件头）只进日志文件。
                            # 少了这一条，日志就只剩一句翻译过的中文 —— 查 bug 的
                            # 人拿到日志还是得自己去复现，那日志就白记了。
                            if getattr(result, 'detail', None):
                                logfile.write(
                                    f'    详情 {os.path.basename(result.src_path)}: '
                                    f'{result.detail}', 'WARN')
                            # 失败的前几条直接写进日志，不用等跑完再点「查看错误」
                            if len(self.error_list) <= 20:
                                self.log(f'   ✗ {os.path.basename(result.src_path)} — '
                                         f'{result.error_msg}')
                            elif len(self.error_list) == 21:
                                self.log('   ✗ 失败较多，后续只计数，'
                                         '跑完点「查看错误」看全部')

                        # 大批量时日志不能只有开头那几行，隔一段就记一次进度
                        if self.processed_count - last_log_mark >= log_every:
                            last_log_mark = self.processed_count
                            last_progress_log = time.time()
                            self.log(progress_line())

                        # 内存告警：几千张大图跑下来很容易把系统吃满，吃满之后
                        # 系统开始换页，表现就是「卡在最后一点」。
                        if self.processed_count - last_mem_check >= 100:
                            last_mem_check = self.processed_count
                            pressure = perf.memory_pressure()
                            # critical 必须当场降并发。原来写成
                            # `if warn or critical ... elif critical`，第一次撞到
                            # critical 只会告警、不降并发 —— 因为 critical 也落在
                            # 前一个分支里，elif 根本不求值。真正降并发要等下一次
                            # 检查，也就是 100 张之后；而此时系统已经在换页，
                            # 那 100 张可能就是压垮它的那一下。
                            if pressure == 'critical' and window > 1:
                                window = 1
                                budget_mb = max(256, budget_mb * 0.5)
                                mem_warned = True
                                self.log(f'⚠️ 内存告急，已把并发压到 1、'
                                         f'内存上限降到 {budget_mb:.0f} MB')
                            elif pressure in ('warn', 'critical') and not mem_warned:
                                mem_warned = True
                                _used, avail, total_mem = perf.memory_status()
                                self.log(
                                    f'⚠️ 系统内存吃紧（剩余 '
                                    f'{(avail or 0) / 1073741824:.1f} GB / '
                                    f'{(total_mem or 0) / 1073741824:.1f} GB）。'
                                    f'可以点「暂停」缓一缓，或换到「节能」模式降低并发。')

                        now = time.time()
                        if now - last_ui_update > UI_UPDATE_INTERVAL:
                            last_ui_update = now
                            c, t, e = (self.processed_count, self.total_count,
                                       len(self.error_list))
                            self.root.after(0, lambda c=c: self.progress.configure(value=c))
                            self.root.after(0, lambda c=c, t=t: self.lbl_progress.configure(text=i18n.tr(f'{c}/{t}')))
                            self.root.after(0, lambda c=c, t=t: self.status.configure(text=i18n.tr(f'处理中: {c}/{t}')))
                            self.root.after(0, lambda e=e: self.btn_errors.configure(text=i18n.tr(f'📋 查看错误 ({e})')))

                        if now - last_log_flush > LOG_FLUSH_INTERVAL:
                            last_log_flush = now
                            self.root.after(0, self.flush_logs)
            # ━━━━ 第 10 步：主循环结束后收尾：该等在途任务就等，然后关掉工作进程 ━━━━
            finally:
                # 强制停止时 wait=False：在途任务的工人已经被杀，等它们没有意义，
                # 而且 shutdown(wait=True) 会一直卡着不返回。
                # 其余情况照旧等在途任务写完。
                try:
                    executor.shutdown(wait=not self.force_stopped,
                                      cancel_futures=True)
                except Exception:
                    pass

            # 收尾时线程池已经等过在途任务了，它们的临时文件也要收拢起来处理。
            for fut in list(in_flight):
                if fut in handled or fut.cancelled() or not fut.done():
                    continue
                try:
                    r = fut.result()
                except Exception:
                    continue
                if r.success:
                    staged.append(r)

            # ━━━━ 第 11 步：落盘 ━━━━
            # 取消了：撤回所有临时文件，原目录保持不变；
            # 正常跑完：把临时文件统一改名成最终文件（原地修改时才会替换原图）。
            if self.cancel_requested:
                dropped = discard_staged(staged)
                # 被强制终止的工作进程正写到一半的临时文件，父进程不知道名字，
                # 只能按命名规律扫一遍目标目录
                dropped += sweep_temps(os.path.dirname(t.dst) for t in tasks)
                written = sum(1 for r in staged if not r.tmp_path)
                if written:
                    self.log(f'↩️ 已撤回 {dropped} 个改动；直写模式下另有 {written} '
                             f'个文件已经落盘，无法撤回')
                else:
                    self.log(f'↩️ 已撤回全部 {dropped} 个改动，原文件未受影响')
            else:
                # 把本批失败的源文件交给提交器保护 —— 它们没有产物但文件还在，
                # 别的任务的新名字可能正好是它们（已复现）。
                #
                # **提交是临界区。** 原地修改时它是「原文件改名成备份 → 产物
                # 改名到位 → 删备份」三段，中途被打断的话原文件会留在
                # `.imgwb-backup-` 名下、产物还是临时文件。所以用 `_committing`
                # 告诉关窗口那一侧：等我做完（见 _await_worker_exit）。
                self._committing = True
                try:
                    self.error_list.extend(commit_staged(
                        staged, is_export,
                        protect=[p for p, _r, _t in self.error_list]))
                finally:
                    self._committing = False

            if anim_carried > 3:
                self.log(f'ⓘ 共 {anim_carried} 个动图原样带过（不旋转时不重编码，'
                         f'帧、帧延时和循环次数原样保留）')

            # ━━━━ 第 12 步：写总结日志：完成多少张、用了多久、时间都花在哪 ━━━━
            if self.cancel_requested:
                self.log(f'⚠️ 已取消！完成 {self.processed_count}/{self.total_count}')
                self.root.after(0, lambda: messagebox.showwarning('已取消', f'已取消\n完成: {self.processed_count}/{self.total_count}', parent=self.root))
            else:
                spent = time.time() - started_at
                self.log(f'✅ 完成！共 {self.total_count} 张，'
                         f'耗时 {spent / 60:.1f} 分钟'
                         + (f'（平均 {self.total_count / spent:.1f} 张/秒）'
                            if spent > 0 else ''))
                # 分段耗时 + 真实吞吐 + 子进程重建次数。
                # 「墙上很久但工人里没花时间」有三种可能，这三个数字正好各指一个：
                #   吞吐低得离谱      -> 磁盘/网络盘/云同步/杀软在拦
                #   子进程重建次数多  -> 池子在反复重启（max_tasks_per_child）
                #   扫描/提交占大头   -> 卡在目录遍历或最后的改名
                commit_secs = time.time() - started_at - spent
                mbps = bytes_done / 1048576 / spent if spent > 0 else 0
                self.log(f'⏱ 分段：扫描 {scan_secs:.1f}s、编排 {plan_secs:.1f}s、'
                         f'处理 {spent:.1f}s、收尾 {max(commit_secs, 0):.1f}s')
                self.log(f'⏱ 实际吞吐 {mbps:.1f} MB/秒'
                         f'（共搬运 {bytes_done / 1048576:.0f} MB）'
                         f'  子进程用过 {len(self._child_pids)} 个')
                # 吞吐低**不一定**是磁盘问题：如果时间都花在编码上，那是 CPU 忙，
                # 磁盘本来就没什么可干的。所以这条提示要等下面算出"在图像处理上
                # 占多少"之后再决定发不发 —— 曾经在 95% 时间都在编码的那次
                # 误报过一句"可能是网络盘/杀软"，把人往错方向带。
                low_throughput = bool(mbps and mbps < 20)

                # 耗时分解。用户报"太慢"时，这几行就是答案 —— 不用再来回问。
                inside = spent_in_workers.get('total', 0.0)
                budget = spent * max(workers, 1)
                if spent > 0 and inside > 0:
                    parts = i18n.join([
                        f'{i18n.tr(label)} {spent_in_workers[key]:.1f}s'
                        for key, label in (('open', '打开'), ('decode', '解码'),
                                           ('encode', '编码写盘'), ('copy', '复制'))
                        if spent_in_workers.get(key)], '、')
                    self.log(f'⏱ 耗时分解：墙上 {spent:.1f} 秒 × 并发 {workers} '
                             f'= {budget:.0f} 工人秒，实际在图像处理上 '
                             f'{inside:.1f} 秒（{inside / budget * 100:.0f}%）')
                    if parts:
                        self.log(f'     其中 {parts}')
                    if inside / budget < 0.25:
                        self.log('     ⚠️ 绝大部分时间**不在图像处理上** —— '
                                 '通常是磁盘慢、杀毒软件实时扫描、或子进程反复重建。'
                                 '可以试试关掉「安全写入」、把源和导出放在同一块盘、'
                                 '或把杀软对这两个目录设为排除。')
                        if low_throughput:
                            self.log('     ⚠️ 同时吞吐低于 20 MB/秒，'
                                     '优先怀疑网络盘/云同步目录或杀软实时扫描。')
                    elif kept_count > self.total_count * 0.5:
                        self.log(f'     ℹ️ 时间几乎都花在编码上，而其中 {kept_count} 张'
                                 f'编完又被丢弃了（压不小）。换个更低的画质档，'
                                 f'或者这批本来就不需要压缩。')

                if resized_count:
                    self.log(f'🔻 {resized_count} 张已缩到最长边 ≤ '
                             f'{cfg.max_edge_val.get()}')
                if kept_count:
                    self.log(f'ℹ️ {kept_count} 张已经是最优（重新压缩反而更大或'
                             f'该格式没有画质参数），已保留原文件')
                if flattened:
                    # 动画被保住时日志会说「原样带过」，被销毁时原来却一声不吭 ——
                    # 用户拿到一批看着正常、实际不会动的文件，统计里还都算成功。
                    self.log(f'⚠️ {flattened} 张动图因目标格式存不下多帧，'
                             f'只保留了第一帧（动画没了）')
                if timeout_hits:
                    self.log(f'⏱ 有 {timeout_hits} 张超过 {timeout_sec} 秒，'
                             f'已放弃处理、原样复制带走（文件在、编号也对）')
                if self.error_list:
                    # 只报个数等于没说 —— 用户看到「33 个失败」，还得自己想到去点
                    # 「查看错误」才知道出了什么事。这里按原因归类，把最主要的
                    # 三类直接写进日志和结束弹窗。
                    kinds = {}
                    for item in self.error_list:
                        # 归类要按「哪一类毛病」，不能带上各自的细节 ——
                        # 现在的原因里写了字节数、文件头这些，每条都不一样，
                        # 直接拿整句当 key 就谁也归不到一起（实测会变成
                        # 「1 个…；1 个…；另有 1 类」，等于没归类）。
                        key = str(item[1])
                        for sep in ('：', ':', '（', '(', '，'):
                            key = key.split(sep)[0]
                        key = key.strip() or '未知原因'
                        kinds[key] = kinds.get(key, 0) + 1
                    top = sorted(kinds.items(), key=lambda kv: -kv[1])[:3]
                    brief = '；'.join(f'{n} 个「{k}」' for k, n in top)
                    if len(kinds) > 3:
                        brief += f'；另有 {len(kinds) - 3} 类'
                    self.log(f'⚠️ {len(self.error_list)} 个失败：{brief}')
                    ok_n = self.total_count - len(self.error_list)
                    detail = '\n'.join(f'  · {n} 个 {k}' for k, n in top)
                    if len(kinds) > 3:
                        detail += f'\n  · 另有 {len(kinds) - 3} 类，详见清单'
                    msg = (f'完成！成功: {ok_n}，失败: {len(self.error_list)}\n\n'
                           f'失败原因：\n{detail}\n\n'
                           f'点界面下方的「查看错误」可以看完整清单。')
                    self.root.after(0, lambda t=msg: messagebox.showwarning(
                        '完成', t, parent=self.root))
                else:
                    self.root.after(0, lambda: messagebox.showinfo('完成', f'全部完成！共 {self.total_count} 张', parent=self.root))

            self.root.after(0, self.flush_logs)

        # ━━━━ 出错兜底：这一轮意外崩溃时走这里 —— 撤回临时文件、把错误写进日志 ━━━━
        except Exception as exc:
            # 消息要立刻取出来：except 块结束时 exc 会被解绑，
            # 而 after() 的回调是稍后才在 UI 线程里执行的。
            msg = str(exc)
            crashed = True
            # 界面上只能显示一句话，但日志里要有完整调用栈，否则报问题的人
            # 给不出栈，我们只能靠猜。
            logfile.exception('process_worker', exc)
            discard_staged(staged)
            sweep_temps(os.path.dirname(t.dst) for t in tasks)
            self.log(f'❌ 错误: {msg}')
            # 日志路径要给出来。界面那个日志框只有七行，用户再点一次执行就把
            # 报错挤没了 —— 而磁盘上那份有完整调用栈。
            where = logfile.path()
            if where:
                self.log(f'   完整调用栈见日志: {where}')
            self.root.after(0, self.flush_logs)
            self.root.after(0, lambda msg=msg, w=where: messagebox.showerror(
                '处理出错',
                # 逐段翻：整段拼完再去套模板是套不上的（中间夹着换行和路径），
                # 而且会让覆盖率体检脚本永久误报一条。
                i18n.tr(f'{msg}')
                + (NEWLINE + NEWLINE + i18n.tr('完整调用栈已写入日志:')
                   + NEWLINE + w if w else ''),
                parent=self.root))

        # ━━━━ 无论成功、取消还是出错，最后都会跑这里：恢复按钮、刷新状态栏 ━━━━
        finally:
            self.executor = None
            self.is_running = False
            e = len(self.error_list)
            self.root.after(0, lambda: self.btn_run.configure(state='normal', text='🚀 开始执行'))
            self.root.after(0, lambda: self.btn_cancel.configure(state='disabled'))
            self.root.after(0, lambda: self.btn_pause.configure(
                state='disabled', text='⏸ 暂停', bootstyle='warning-outline'))
            self.root.after(0, lambda: self.btn_stop.configure(state='disabled'))
            # 计数原来只在 0.15 秒的节流块里刷新，跑得快的小批次一次都轮不到，
            # 按钮会亮着却写 (0)，点开却列着好几条。这里补最终值。
            self.root.after(0, lambda e=e: self.btn_errors.configure(
                state='normal' if e > 0 else 'disabled', text=f'📋 查看错误 ({e})'))
            # 原来无条件写「准备就绪」，于是成功、失败、取消三种结局在状态栏上
            # 长得一模一样 —— 跑完 12 张挂了 1 张，状态栏照样一片祥和。
            done, total = self.processed_count, self.total_count
            # **崩溃必须排在最前面。** 少了这一条，跑到一半炸掉、还没有任何
            # 文件失败时（error_list 是空的），会掉进下面的「完成」分支报出
            # 「完成 0/975」—— 用户看到的是"一点开始直接结束"，而真正的报错
            # 被日志框的七行滚动挤没了，完全无从查起。
            if crashed:
                final_text = f'出错了，停在 {done}/{total}（详见日志）'
                final_style = 'danger'
            elif self.cancel_requested:
                final_text = f'已取消，停在 {done}/{total}'
                final_style = 'secondary'
            elif e > 0:
                final_text = f'完成，{e} 个失败'
                final_style = 'warning'
            elif total and done < total:
                # 没崩、没取消、没有失败记录，却没跑完 —— 这本身就不正常，
                # 别报成「完成」。
                final_text = f'异常结束，只处理了 {done}/{total}（详见日志）'
                final_style = 'danger'
            elif total:
                final_text = f'完成 {done}/{total}'
                final_style = 'success'
            else:
                final_text, final_style = '准备就绪', 'secondary'
            self._status_owner = 'run'
            self.root.after(0, lambda t=final_text, st=final_style:
                            self.status.configure(text=i18n.tr(t), bootstyle=st))
            self.root.after(0, lambda: self.progress.configure(value=self.processed_count))
            self.root.after(0, lambda: self.lbl_progress.configure(text=i18n.tr(f'{self.processed_count}/{self.total_count}')))
