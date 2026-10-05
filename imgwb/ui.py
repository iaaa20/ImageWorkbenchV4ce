"""界面构建与控件状态。

拆成 mixin 而不是独立类：这些方法要读写 ImageProcessorApp 的 Tk 变量和控件，
合在一起反而绕。app.py 负责状态与逻辑，这里只管长什么样。
"""

# ============================================================
# 【导读】ui.py —— 「界面长什么样」。
# create_ui 从上到下搭窗口（里面按「区块 1~4」标了路标）；
# 其余是界面细节：滚轮、简易 / 高级切换、控件变灰、文件名预览。
# 按钮按下之后做什么，在 app.py。
# ============================================================
import ttkbootstrap as ttk
from ttkbootstrap.constants import BOTH, CENTER, LEFT, RIGHT, W, X, Y, YES
from tkinter import scrolledtext

from .config import HEIC_SUPPORTED, MAX_EDGE_CHOICES, TIMEOUT_CHOICES
from . import perf as PERF
from . import i18n
from .presets import PRESETS
from .tooltip import attach as tip
from .dnd import DND_FILES, HAS_DND
from .utils import parse_digit_count


class UIBuilderMixin:
    """负责建界面和刷新控件状态，由 ImageProcessorApp 继承。"""

    # 滚轮一格滚多少像素。Tk 默认的「一个 unit」是可视高度的 1/10（这个窗口
    # 里是 75px），而整个可滚范围也才 160px 上下 —— 一格就滚掉将近一半行程，
    # 落点只有两三个，手感非常顿。
    WHEEL_PIXELS_PER_NOTCH = 60

    def _on_log_wheel(self, event):
        """日志框上的滚轮：先给日志自己滚，滚到头了就让给页面。

        这个绑定在控件上，早于 Text 的类绑定执行，所以能看到「滚之前」的位置，
        据此判断日志还能不能再滚。小窗口下日志几乎占满可视区，没有这层的话
        滚轮全被它吃掉，页面根本翻不动。
        """
        first, last = self.log_text.yview()
        # **只有日志整个装得下、根本没得滚时，才把滚轮让给页面。**
        # 原来的判据是「滚到头了就让给页面」，但日志每来一行就自动滚到底，
        # 于是它几乎永远处在「到头」状态 —— 用户在日志上往下滚，动的却是
        # 整个页面，感觉就像"日志的滚轮被绑到窗口上了"。
        nothing_to_scroll = first <= 0.001 and last >= 0.999
        if nothing_to_scroll:
            self._scroll_page(event)
            return 'break'          # 别让 Text 再处理一遍
        return None                 # 交给 Text 自己滚，页面不动

    # 整个窗口的滚轮：上下翻页（鼠标在日志框上时交给日志框自己处理）。
    def _on_mousewheel(self, event):
        # 日志框有自己的处理（见 _on_log_wheel），这里放行
        w = getattr(event, 'widget', None)
        log = getattr(self, 'log_text', None)
        while w is not None and log is not None:
            if w is log:
                return
            w = getattr(w, 'master', None)

        self._scroll_page(event)

    # 把滚轮转动换算成页面移动的像素。
    def _scroll_page(self, event):

        # 高分辨率滚轮和精密触控板发的 delta 常常小于 120，原来的
        # int(delta / 120) 会把它们整除成 0 直接丢掉 —— 表现就是滚几下不动、
        # 攒够一整格猛跳一下。这里按比例换算成像素，不足 1px 的余数留到下次。
        self._wheel_accum += -event.delta / 120.0 * self.WHEEL_PIXELS_PER_NOTCH
        pixels = int(self._wheel_accum)
        self._wheel_accum -= pixels
        if pixels:
            self._scroll_pixels(pixels)

    # 这些控件 Tk 自带滚轮类绑定，滚一下就改值。放在可滚动面板里非常危险 ——
    # 用户只是想翻页，结果把「不旋转」滚成了「顺时针 90°」而毫无察觉。
    WHEEL_HIJACKERS = ('TCombobox', 'TSpinbox')

    def _tame_wheel_widgets(self, parent=None):
        """让滚轮经过下拉框/微调框时只翻页，不改它们的值。

        在控件自身上绑一个处理函数并 return 'break'：Tk 的事件传递顺序是
        控件 -> 类 -> 顶层 -> all，'break' 会掐断后面的类绑定（也就是改值的
        那个），同时我们自己先把页面滚了，所以翻页照常。
        """
        widget = parent if parent is not None else self.root
        try:
            if widget.winfo_class() in self.WHEEL_HIJACKERS:
                widget.bind('<MouseWheel>', self._wheel_scroll_only)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._tame_wheel_widgets(child)

    # 装在下拉框、数字框上：滚轮只翻页，不许顺手改掉设置值。
    def _wheel_scroll_only(self, event):
        self._on_mousewheel(event)
        return 'break'

    def _scroll_pixels(self, dy):
        """按像素滚动画布，并把视图夹在内容范围内。"""
        canvas = self._canvas
        region = str(canvas.cget('scrollregion')).split()
        if len(region) != 4:
            return

        total = float(region[3]) - float(region[1])
        visible = canvas.winfo_height()
        if total <= visible:          # 内容没超出，没什么可滚的
            return

        top = canvas.yview()[0] + dy / total
        canvas.yview_moveto(min(max(top, 0.0), 1.0 - visible / total))

    def ui_scale(self):
        """界面缩放倍数。1.0 = 100%，2.0 = 200%。

        入口调了 SetProcessDpiAwareness(1)，Windows 就不再代为拉伸，Tk 会把
        scaling 提上去、按物理像素画控件 —— 而窗口尺寸如果还是写死的 720，
        200% 缩放下就只有实际需要的一半宽。所有硬编码的尺寸都得乘这个系数。

        Tk 的 scaling 是「每点多少像素」，96 DPI 下是 1.3333。
        """
        try:
            return max(1.0, float(self.root.tk.call('tk', 'scaling')) / 1.3333)
        except Exception:
            return 1.0

    def _on_content_resize(self):
        """内容尺寸变了：重算滚动范围，并决定横向滚动条要不要露面。"""
        canvas = self._canvas
        canvas.configure(scrollregion=canvas.bbox('all'))
        self._sync_hbar()

    def _fit_content(self):
        """把画布里那块内容铺到合适大小。

        两个方向的规则一样：**取「画布尺寸」和「内容自然需要的尺寸」里的大值**。
        - 内容比画布大 -> 保持内容的自然尺寸，多出来的部分交给滚动条。
          原来无条件用画布尺寸，等于把内容硬压扁 —— 装不下的是被**切掉**，
          不是变成可以滚。高 DPI 下右边 400 多像素就是这么没的。
        - 画布比内容大 -> 把内容拉到画布那么大，多出来的高度会被日志框吃掉
          （step5 是 expand=YES），不会在下面空一大片灰。

        **量「自然尺寸」之前必须先把拉伸松开。** 不然量到的是上一次拉伸后的
        结果，一次比一次大，几轮下来内容比窗口高出几百像素、凭空多出滚动条。
        """
        if getattr(self, '_fitting', False):
            return
        canvas = self._canvas
        items = canvas.find_withtag('all')
        if not items:
            return
        self._fitting = True
        try:
            item = items[0]
            frame = self._scrollable_frame
            canvas.itemconfig(item, width=1, height=1)   # 先松开
            canvas.update_idletasks()
            need_w, need_h = frame.winfo_reqwidth(), frame.winfo_reqheight()
            canvas.itemconfig(item,
                              width=max(canvas.winfo_width(), need_w),
                              height=max(canvas.winfo_height(), need_h))
            canvas.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox('all'))
        finally:
            self._fitting = False
        self._sync_hbar()

    def _sync_hbar(self):
        """内容比窗口宽才显示横向滚动条，否则收起来别占地方。"""
        bar = getattr(self, '_hbar', None)
        if bar is None:
            return
        try:
            need = self._scrollable_frame.winfo_reqwidth()
            have = self._canvas.winfo_width()
            # _fit_content 会把内容拉到画布宽，那时 reqwidth 反映的就是画布宽，
            # 不该因此判成"装不下"

            if need > have + 2:
                bar.grid()
            else:
                bar.grid_remove()
        except Exception:
            pass

    # 【搭界面】从上到下依次摆：
    #   区块 1 图片来源 → 区块 2 一键处理 / 高级设置 → 区块 3 执行状态（进度条、日志）→ 区块 4 底部按钮
    # 下面每个区块开头都有标记。
    def create_ui(self):
        # 每个下拉框的**中文原文列表**。切换语言时照这份重新生成显示用的译文
        # 列表 —— 不能拿界面上现有的英文反查，重复译文会还原错。
        self._cb_zh = {}
        # 窗口尺寸自适应用到的状态。记的是「所有我们主动设过的尺寸」而不是最后
        # 一个 —— 窗口映射期间 Tk 会补发中间尺寸的 Configure（比如 __init__ 里
        # 设的初始值），只比对最后一个的话这些迟到事件会被误判成用户在拖窗口。
        self._auto_sizes = set()
        self._user_resized = False
        try:
            w, h = self.root.geometry().split('+')[0].split('x')
            self._auto_sizes.add((int(w), int(h)))
        except Exception:
            pass
        self.root.bind('<Configure>', self._on_root_configure, add='+')

        container = ttk.Frame(self.root)
        container.pack(fill=BOTH, expand=YES)

        # ━━ 最外层：一块能上下滚动的画布，所有内容都放在它里面（窗口太矮时可以滚） ━━
        canvas = ttk.Canvas(container)
        scrollbar = ttk.Scrollbar(container, orient='vertical', command=canvas.yview)
        # 横向滚动条是兜底：高 DPI 下窗口宽度未必装得下内容，只有纵向滚动条的话
        # 被切掉的右半边（能力指示器、「⚙ 高级设置」入口）就完全够不着了。
        # 平时隐藏，真的装不下才出现 —— 见 _sync_hbar。
        hbar = ttk.Scrollbar(container, orient='horizontal', command=canvas.xview)

        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind(
            '<Configure>',
            lambda e: self._on_content_resize()
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set, xscrollcommand=hbar.set)

        # 用 grid 而不是 pack：横向滚动条要占住底边一整行，而纵向的只占右侧一列，
        # pack 的 side=RIGHT/BOTTOM 组合会让两条互相抢角落。
        canvas.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        hbar.grid(row=1, column=0, sticky='ew')
        hbar.grid_remove()
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self._hbar = hbar
        self._canvas = canvas
        self._scrollable_frame = scrollable_frame
        self._wheel_accum = 0.0

        # 全局绑定一次就够。原来是在画布的 <Enter>/<Leave> 里 bind_all /
        # unbind_all —— 但指针移进画布内的子控件时 Tk 同样会给画布发 <Leave>，
        # 而内容几乎铺满画布，结果绑定被频繁解除，滚轮时灵时不灵。
        self.root.bind_all('<MouseWheel>', self._on_mousewheel)

        canvas.bind('<Configure>', lambda e: self._fit_content())

        main = ttk.Frame(scrollable_frame, padding=15)
        main.pack(fill=BOTH, expand=YES)

        # ━━ 区块 1：图片来源（浏览、拖拽、是否扫描子文件夹、支持的格式） ━━
        step1 = ttk.Labelframe(main, text=' 图片来源 ', padding=10, bootstyle='primary')
        step1.pack(fill=X, pady=(0, 8))

        row1 = ttk.Frame(step1)
        row1.pack(fill=X)
        ttk.Entry(row1, textvariable=self.src_folder).pack(side=LEFT, fill=X, expand=YES, padx=(0, 10))
        ttk.Button(row1, text='浏览...', command=self.select_source).pack(side=RIGHT)

        row_opts = ttk.Frame(step1)
        row_opts.pack(fill=X, pady=(5, 0))

        self.drop_label = ttk.Label(
            step1, text='💡 也可以直接拖拽文件夹或多张图片到这里',
            bootstyle='secondary', font=('', 9), anchor=CENTER,
            padding=5, cursor='hand2'
        )
        self.drop_label.pack(fill=X, pady=(5, 0))

        if HAS_DND:
            self.drop_label.configure(
                text='📥 拖拽文件夹或图片到此处快速导入'
                     '（地址栏也可用分号隔开填多条路径）', bootstyle='info')
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind('<<Drop>>', self.handle_drop)

        ttk.Checkbutton(
            row_opts, text='递归扫描子文件夹',
            variable=self.scan_subdirs, bootstyle='info-round-toggle'
        ).pack(side=LEFT)

        formats = 'JPG, PNG, WebP, GIF, TIFF, BMP'
        if HEIC_SUPPORTED:
            formats += ', HEIC ✓'
            fmt_style = 'success'
        else:
            formats += ', HEIC ✗'
            fmt_style = 'secondary'
        ttk.Label(row_opts, text=f'支持: {formats}', bootstyle=fmt_style, font=('', 9)).pack(side=RIGHT)


        # ━━ 区块 2：「高级设置」切换按钮 + 两套互换的面板（简易的一键按钮 / 高级的全部选项） ━━
        #
        # 模式切换放在面板正上方。原来塞在窗口底部那一排按钮里，位置太靠下，
        # 一眼扫过去根本注意不到。
        mode_bar = ttk.Frame(main)
        mode_bar.pack(fill=X, pady=(0, 4))
        self.lbl_mode = ttk.Label(mode_bar, text='', font=('', 10, 'bold'))
        self.lbl_mode.pack(side=LEFT)
        self.btn_mode = ttk.Button(mode_bar, text='⚙ 高级设置...',
                                   command=self.toggle_mode,
                                   bootstyle='info', width=18)
        self.btn_mode.pack(side=RIGHT)

        self.btn_lang = ttk.Button(mode_bar, text='\U0001f310 中 / EN',
                                   bootstyle='secondary-link', width=9,
                                   command=self.switch_language)
        self.btn_lang.pack(side=RIGHT, padx=(0, 6))
        tip(self.btn_lang, '切换界面语言（中文 / English）\n'
                           'Switch interface language. The log file follows '
                           'the same setting.')

        # 简易和高级两块占同一个位置，一次只显示一块 —— 同时摆两个「执行」
        # 入口（一键按钮 + 开始执行）只会让人不知道该点哪个。
        self.mode_slot = ttk.Frame(main)
        self.mode_slot.pack(fill=X)

        self.simple_frame = ttk.Frame(self.mode_slot)

        step2 = ttk.Labelframe(self.simple_frame, text=' 一键处理 ', padding=10,
                               bootstyle='success')
        step2.pack(fill=X, pady=(0, 8))

        ttk.Label(step2, text='选好文件夹，点一个就行 —— 结果输出到源文件夹旁边的'
                              '「xxx_已处理」，原文件不动。',
                  foreground='gray', font=('', 9)).pack(anchor=W, pady=(0, 8))

        grid_presets = ttk.Frame(step2)
        grid_presets.pack(fill=X)
        for i in range(3):
            grid_presets.columnconfigure(i, weight=1, uniform='preset')

        self.preset_buttons = {}
        for idx, preset in enumerate(PRESETS):
            cell = ttk.Frame(grid_presets)
            cell.grid(row=idx // 3, column=idx % 3, sticky='nsew', padx=4, pady=4)
            btn = ttk.Button(
                cell, text=preset['label'], width=14,
                bootstyle=preset['style'],
                command=lambda k=preset['key']: self.apply_preset(k))
            btn.pack(fill=X)
            ttk.Label(cell, text=preset['desc'], foreground='gray',
                      font=('', 8), anchor=CENTER, wraplength=150).pack(
                          fill=X, pady=(2, 0))
            warn = preset.get('warn')
            tip(btn, f"{preset['label']} —— {preset['desc']}\n\n"
                     + (f'⚠️ {warn}\n\n' if warn else '')
                     + '输出到源文件夹旁边的「xxx_已处理」，原文件不动。')
            self.preset_buttons[preset['key']] = btn

        # ━━ 区块 3：执行状态（进度条、状态文字、日志框） ━━
        step5 = ttk.Labelframe(main, text=' 执行状态 ', padding=10)
        step5.pack(fill=BOTH, expand=YES, pady=(0, 8))

        prog_frame = ttk.Frame(step5)
        prog_frame.pack(fill=X, pady=(0, 5))
        self.progress = ttk.Progressbar(prog_frame, bootstyle='success-striped', mode='determinate')
        self.progress.pack(side=LEFT, fill=X, expand=YES)
        self.lbl_progress = ttk.Label(prog_frame, text='0/0', width=12)
        self.lbl_progress.pack(side=RIGHT, padx=(10, 0))

        self.status = ttk.Label(step5, text='准备就绪', bootstyle='secondary')
        self.status.pack(anchor=W, pady=(0, 5))

        ttk.Label(step5, text='处理日志:', font=('', 9)).pack(anchor=W)
        # 别太高：小窗口下日志会把整个可视区占满，滚轮全被它吃掉，
        # 页面就没法翻了
        self.log_text = scrolledtext.ScrolledText(
            step5, height=7, font=('Consolas', 9), state='disabled')
        self.log_text.pack(fill=BOTH, expand=YES, pady=(3, 0))
        # 滚动链：日志滚到头之后，滚轮接着翻页面，而不是卡在那儿
        self.log_text.bind('<MouseWheel>', self._on_log_wheel)

        # ━━ 区块 4：底部按钮（暂停、开始、取消、强制停止、查看错误） ━━
        self.btn_frame = ttk.Frame(main)
        self.btn_frame.pack(fill=X, pady=(5, 0))

        # 只在高级模式显示 —— 简易模式下一键按钮本身就是执行入口
        self.btn_run = ttk.Button(self.btn_frame, text='🚀 开始执行',
                                  command=self.start_processing,
                                  bootstyle='success', width=18)

        self.btn_pause = ttk.Button(self.btn_frame, text='⏸ 暂停',
                                    command=self.toggle_pause,
                                    bootstyle='warning-outline', width=10,
                                    state='disabled')
        self.btn_pause.pack(side=LEFT, padx=(0, 6))
        tip(self.btn_pause,
            '停止派发新任务，在跑的几张会先完成。继续时从断点接着来，不重跑。')

        self.btn_cancel = ttk.Button(self.btn_frame, text='⏹ 取消', command=self.request_cancel, bootstyle='danger-outline', width=10, state='disabled')
        self.btn_cancel.pack(side=LEFT)
        tip(self.btn_cancel, '停止本次处理。安全写入开启时会完整撤回已做的改动。')

        self.btn_stop = ttk.Button(self.btn_frame, text='⛔ 强制停止',
                                   command=self.force_stop,
                                   bootstyle='danger', width=12,
                                   state='disabled')
        self.btn_stop.pack(side=LEFT, padx=(6, 0))
        tip(self.btn_stop, '立刻杀掉全部工作进程，不等任何在途任务。')

        self.btn_errors = ttk.Button(self.btn_frame, text='📋 查看错误 (0)', command=self.show_errors, bootstyle='secondary-outline', width=16, state='disabled')
        self.btn_errors.pack(side=RIGHT)

        self.create_advanced()
        # 控件全部建好之后再统一处理，新加的下拉框不用记得单独绑一次
        self._tame_wheel_widgets()
        # 按简易模式（启动时的模式）定一次尺寸，之后切换模式一律不再改窗口
        # 大小。用户要的是「窗口别跳」，不是「窗口一上来就很大」—— 按高级模式
        # 预留高度会让启动窗口高出一大截，简易模式下白占屏幕。
        # 高级模式内容更高，装不下的部分交给滚动条。
        self.set_mode(advanced=False)
        self.fit_window()
        self._size_locked = True
        self.update_ui()
        self.update_preview()

    def create_advanced(self):
        """建高级设置面板。和简易面板占同一个位置，一次只显示一个。

        启动时就建好、只是先不显示 —— update_ui 要操作里面的控件，惰性创建的话
        每处都得先判断存不存在，反而更绕。
        """
        adv = ttk.Frame(self.mode_slot)
        self.advanced_frame = adv

        step2 = ttk.Labelframe(adv, text=' 导出设置 ', padding=10, bootstyle='warning')
        step2.pack(fill=X, pady=(0, 8))

        ttk.Radiobutton(
            step2, text='原地修改 (⚠️ 注意备份)',
            variable=self.out_mode, value='original', command=self.update_ui
        ).pack(anchor=W)

        f_new = ttk.Frame(step2)
        f_new.pack(fill=X, pady=5)
        ttk.Radiobutton(
            f_new, text='另存为新位置:',
            variable=self.out_mode, value='new', command=self.update_ui
        ).pack(side=LEFT)
        self.ent_new = ttk.Entry(f_new, textvariable=self.new_folder_path, width=30)
        if HAS_DND:
            # 导出位置也能直接拖文件夹进来。窗口级的拖拽是设**来源**的，
            # 控件上再注册一次会优先命中这里，两边就分工清楚了。
            try:
                self.ent_new.drop_target_register(DND_FILES)
                self.ent_new.dnd_bind('<<Drop>>', self._drop_dest)
            except Exception:
                pass
        self.ent_new.pack(side=LEFT, fill=X, expand=YES, padx=5)
        self.btn_new = ttk.Button(f_new, text='选择...', command=self.select_dest, bootstyle='warning-outline')
        self.btn_new.pack(side=RIGHT)

        f_struct = ttk.Frame(step2)
        f_struct.pack(fill=X, pady=(10, 0))
        self.chk_struct = ttk.Checkbutton(
            f_struct, text='保持原目录结构',
            variable=self.keep_structure, command=self.update_ui, bootstyle='warning-round-toggle'
        )
        self.chk_struct.pack(side=LEFT)

        self.lbl_suf = ttk.Label(f_struct, text='  ➜  输出文件夹名:')
        self.lbl_suf.pack(side=LEFT, padx=(10, 5))
        self.ent_suf = ttk.Entry(f_struct, textvariable=self.dir_suffix_val, width=15)
        self.ent_suf.pack(side=LEFT)

        step3 = ttk.Labelframe(adv, text=' 命名规则 ', padding=10, bootstyle='info')
        step3.pack(fill=X, pady=(0, 8))

        r_name = ttk.Frame(step3)
        r_name.pack(fill=X, pady=5)
        ttk.Label(r_name, text='命名方式:').pack(side=LEFT)
        ttk.Radiobutton(r_name, text='前缀+编号', variable=self.name_mode, value='prefix', command=self.update_ui).pack(side=LEFT, padx=10)
        ttk.Radiobutton(r_name, text='纯数字编号', variable=self.name_mode, value='number', command=self.update_ui).pack(side=LEFT, padx=10)
        ttk.Radiobutton(r_name, text='保留原名', variable=self.name_mode, value='keep', command=self.update_ui).pack(side=LEFT, padx=10)

        grid = ttk.Frame(step3)
        grid.pack(fill=X, pady=5)
        ttk.Label(grid, text='前缀:').grid(row=0, column=0, padx=(0, 5))
        self.ent_pre = ttk.Entry(grid, textvariable=self.prefix_val, width=10)
        self.ent_pre.grid(row=0, column=1, padx=5)
        ttk.Label(grid, text='起始编号:').grid(row=0, column=2, padx=(15, 5))
        # 要存成属性，否则「保留原名」时没法把它一起置灰
        self.ent_start = ttk.Entry(grid, textvariable=self.start_num, width=8)
        self.ent_start.grid(row=0, column=3, padx=5)
        ttk.Label(grid, text='位数:').grid(row=0, column=4, padx=(15, 5))
        opts_digit = ['自动 (按数量)', '1位', '2位', '3位 (001)', '4位', '5位']
        self._cb_zh['cb_digit'] = opts_digit
        self.cb_digit = ttk.Combobox(
            grid, textvariable=self.digit_val,
            values=i18n.options(opts_digit),
            width=10, state='readonly'
        )
        self.cb_digit.grid(row=0, column=5, padx=5)
        tip(self.cb_digit,
            '「自动」= 先算这一批最大编号需要几位，再多留一位。\n'
            '1234 个文件 → 00001~01234。多留的一位是给以后往同一目录追加文件用的，'
            '否则超过 9999 之后 9999 和 10000 混排会乱。')
        # 预览标签用的是 Consolas，塞中文会挤成一团，规则说明放这一行
        self.lbl_digit_hint = ttk.Label(
            grid, text='自动 = 按文件总数定宽，再多留一位（1234 个 → 00001）',
            foreground='gray', font=('', 8))
        self.lbl_digit_hint.grid(row=1, column=0, columnspan=6, sticky=W, pady=(4, 0))

        preview_frame = ttk.Frame(step3)
        preview_frame.pack(fill=X, pady=(8, 0))
        ttk.Label(preview_frame, text='预览:', font=('', 9)).pack(side=LEFT)
        self.lbl_preview = ttk.Label(preview_frame, text='', bootstyle='info', font=('Consolas', 10))
        self.lbl_preview.pack(side=LEFT, padx=10)

        step4 = ttk.Labelframe(adv, text=' 处理选项 ', padding=10, bootstyle='success')
        step4.pack(fill=X, pady=(0, 8))

        row_opt = ttk.Frame(step4)
        row_opt.pack(fill=X, pady=5)

        ttk.Label(row_opt, text='旋转:').pack(side=LEFT)
        opts_rot = ['不旋转', '顺时针 90°', '逆时针 90°', '旋转 180°']
        self._cb_zh['cb_rotate'] = opts_rot
        self.cb_rotate = ttk.Combobox(row_opt, textvariable=self.rotate_val,
                                      values=i18n.options(opts_rot),
                                      width=12, state='readonly')
        self.cb_rotate.pack(side=LEFT, padx=(5, 20))

        ttk.Label(row_opt, text='画质:').pack(side=LEFT)
        opts_quality = ['极致保真 (100%)', '通用高清 (95%)', '网络压缩 (85%)']
        self._cb_zh['cb_quality'] = opts_quality
        self.cb_quality = ttk.Combobox(row_opt, textvariable=self.quality_val,
                                       values=i18n.options(opts_quality),
                                       width=16, state='readonly')
        self.cb_quality.pack(side=LEFT, padx=5)
        tip(self.cb_quality,
            '极致保真 100% —— 几乎不掉画质，文件最大\n'
            '通用高清 95% —— 肉眼基本看不出差别，日常够用\n'
            '网络压缩 85% —— 体积明显变小，适合发网上\n\n'
            'PNG 在 85% 下会做色彩量化（有损）；BMP 没有画质参数；'
            'TIFF 只在 85% 时换成 LZW 无损压缩。')

        row_compress = ttk.Frame(step4)
        row_compress.pack(fill=X, pady=(8, 0))
        self.chk_compress = ttk.Checkbutton(
            row_compress, text='仅压缩',
            variable=self.compress_only, bootstyle='success-round-toggle'
        )
        self.chk_compress.pack(side=LEFT)
        tip(self.chk_compress,
            '不旋转时也按「画质」重新编码一遍。\n'
            '想单纯压缩体积、或者想把手机照片的方向标记烘焙进像素，就开它。')

        self.chk_ico = ttk.Checkbutton(
            row_compress, text='转成 ICO 图标',
            variable=self.is_ico, bootstyle='success-round-toggle'
        )
        self.chk_ico.pack(side=LEFT, padx=(20, 0))
        tip(self.chk_ico,
            '把图片做成 Windows 图标。\n'
            '尺寸上限 256 —— 这是 ICO 格式本身的限制（目录项用单字节存宽高）。')

        self.ico_detail_frame = ttk.Frame(step4, padding=(20, 10, 0, 0))
        self.ico_detail_frame.pack(fill=X)

        row_ico_sizes = ttk.Frame(self.ico_detail_frame)
        row_ico_sizes.pack(fill=X)

        ttk.Label(row_ico_sizes, text='专业模式:', font=('', 9, 'bold')).pack(side=LEFT)
        ttk.Radiobutton(
            row_ico_sizes, text='标准多尺寸 (256-16)',
            variable=self.ico_mode_val, value='multi',
            bootstyle='toolbutton-success',
            padding=5
        ).pack(side=LEFT, padx=(5, 15))

        ttk.Label(row_ico_sizes, text='固定尺寸:', font=('', 9)).pack(side=LEFT)
        for size in (256, 128, 64, 48, 32, 16):
            ttk.Radiobutton(
                row_ico_sizes, text=f'{size}',
                variable=self.ico_mode_val, value=f'fixed_{size}',
                bootstyle='toolbutton-outline-success',
                padding=2
            ).pack(side=LEFT, padx=2)

        row_ico_auto = ttk.Frame(self.ico_detail_frame)
        row_ico_auto.pack(fill=X, pady=(5, 0))
        ttk.Label(row_ico_auto, text='智能缩放:', font=('', 9)).pack(side=LEFT)
        for size in (256, 128):
            ttk.Radiobutton(
                row_ico_auto, text=f'自动 {size}',
                variable=self.ico_mode_val, value=f'auto_{size}',
                bootstyle='toolbutton-outline-info',
                padding=2
            ).pack(side=LEFT, padx=2)
        ttk.Label(row_ico_auto, text='(原图小时保留，大时压缩)', foreground='gray', font=('', 8)).pack(side=LEFT, padx=5)

        row_ico_fmt = ttk.Frame(self.ico_detail_frame)
        row_ico_fmt.pack(fill=X, pady=(8, 0))
        ttk.Label(row_ico_fmt, text='内部格式:', font=('', 9)).pack(side=LEFT)
        opts_ico_fmt = ['自动 (推荐)', '强制 PNG (支持透明)', '强制 BMP (经典兼容)']
        self._cb_zh['cb_ico_fmt'] = opts_ico_fmt
        self.cb_ico_fmt = ttk.Combobox(
            row_ico_fmt, textvariable=self.ico_format_val,
            values=i18n.options(opts_ico_fmt), width=18, state='readonly'
        )
        self.cb_ico_fmt.pack(side=LEFT, padx=5)
        ttk.Label(row_ico_fmt, text='(Windows 10+ 选自动)', foreground='gray', font=('', 8)).pack(side=LEFT)

        ttk.Label(self.ico_detail_frame, text='💡 提示：多选尺寸会打包成单个 ICO。若转换结果不符合预期，请仅勾选所需的一个尺寸。',
                  foreground='#d9534f', font=('', 8)).pack(anchor=W, pady=(5, 0))

        # 存成属性：update_ui 重新显示 ICO 面板时要用 before= 插回原位，
        # 否则 pack() 会把它追加到 step4 末尾，跑到「性能模式」下面去。
        self.row_perf = ttk.Frame(step4)
        self.row_perf.pack(fill=X, pady=(10, 0))

        ttk.Label(self.row_perf, text='性能模式:').pack(side=LEFT)
        self.perf_buttons = {}
        for mode in (PERF.ECO, PERF.BALANCED, PERF.TURBO, PERF.CUSTOM):
            info = PERF.MODES[mode]
            btn = ttk.Radiobutton(
                self.row_perf, text=info['label'], value=mode,
                variable=self.perf_mode, command=self.update_ui,
                bootstyle='toolbutton-outline-success', padding=4)
            btn.pack(side=LEFT, padx=3)
            tip(btn, f"{info['label']} —— {info['desc']}\n\n{info['tip']}")
            self.perf_buttons[mode] = btn

        self.spin_workers = ttk.Spinbox(self.row_perf, from_=1, to=64,
                                        textvariable=self.max_workers, width=5)
        self.spin_workers.pack(side=LEFT, padx=(12, 4))
        self.lbl_perf = ttk.Label(self.row_perf, text='', foreground='gray',
                                  font=('', 8))
        self.lbl_perf.pack(side=LEFT, padx=4)

        row_mem = ttk.Frame(step4)
        row_mem.pack(fill=X, pady=(8, 0))
        ttk.Label(row_mem, text='内存上限:').pack(side=LEFT)
        opts_mem = [label for label, _mb in PERF.MEMORY_LIMIT_CHOICES]
        self._cb_zh['cb_mem'] = opts_mem
        self.cb_mem = ttk.Combobox(
            row_mem, textvariable=self.mem_limit_val,
            values=i18n.options(opts_mem),
            width=18, state='readonly')
        self.cb_mem.pack(side=LEFT, padx=5)
        tip(self.cb_mem,
            '同时在解码的图片总量上限。超过就排队等，不会硬挤。\n'
            '一张 8000x8000 约 610 MB；176 帧的动图约 1 GB。')
        self.lbl_mem = ttk.Label(row_mem, text='', foreground='gray', font=('', 8))
        self.lbl_mem.pack(side=LEFT, padx=6)

        row_edge = ttk.Frame(step4)
        row_edge.pack(fill=X, pady=(8, 0))
        ttk.Label(row_edge, text='最长边不超过:').pack(side=LEFT)
        opts_edge = [label for label, _px in MAX_EDGE_CHOICES]
        self._cb_zh['cb_edge'] = opts_edge
        self.cb_edge = ttk.Combobox(
            row_edge, textvariable=self.max_edge_val,
            values=i18n.options(opts_edge),
            width=18, state='readonly')
        self.cb_edge.pack(side=LEFT, padx=5)
        tip(self.cb_edge,
            '超过这个尺寸的图会等比缩小，**只缩不放**，小图原样不动。\n\n'
            '这是所有压缩手段里省得最多的一招：4000x3000 存 JPEG q90 实测'
            '1377 KB，限到 1920 只剩 193 KB（0.14 倍）。而且省的是看不到的'
            '像素，不像降画质那样啃细节。\n\n'
            '会真的改变像素尺寸，所以默认不限；一键按钮也一律不缩。')
        self.lbl_edge = ttk.Label(row_edge, text='只缩不放，小图不动',
                                  foreground='gray', font=('', 8))
        self.lbl_edge.pack(side=LEFT, padx=6)

        row_timeout = ttk.Frame(step4)
        row_timeout.pack(fill=X, pady=(8, 0))
        ttk.Label(row_timeout, text='单张超时:').pack(side=LEFT)
        opts_timeout = [label for label, _sec in TIMEOUT_CHOICES]
        self._cb_zh['cb_timeout'] = opts_timeout
        self.cb_timeout = ttk.Combobox(
            row_timeout, textvariable=self.timeout_val,
            values=i18n.options(opts_timeout),
            width=18, state='readonly')
        self.cb_timeout.pack(side=LEFT, padx=5)
        tip(self.cb_timeout,
            '一张图超过这个时间还没处理完，就放弃处理、按命名规则原样复制一份 ——\n'
            '文件在、编号也对，不会因为一两张卡住把整批拖住。\n\n'
            '一张 3650x3650 走完整条流水线实测 0.14 秒，撞到门槛的基本都是卡在'
            '磁盘、杀毒软件或系统换页上，等下去也不会变快。')
        ttk.Label(row_timeout, text='超时就改名直接复制，不处理',
                  foreground='gray', font=('', 8)).pack(side=LEFT, padx=6)

        row_safe = ttk.Frame(step4)
        row_safe.pack(fill=X, pady=(8, 0))
        self.chk_safe = ttk.Checkbutton(
            row_safe, text='安全写入',
            variable=self.safe_write, command=self.update_ui,
            bootstyle='success-round-toggle'
        )
        self.chk_safe.pack(side=LEFT)
        tip(self.chk_safe,
            '开启：所有输出先写临时文件，全部完成后才统一落盘。中途取消能完整'
            '撤回，原目录一点不变；代价是处理期间临时占用约一倍磁盘空间。\n\n'
            '关闭：直接写目标文件，省磁盘，但中途取消只能停在当前进度。')

        row_others = ttk.Frame(step4)
        row_others.pack(fill=X, pady=(8, 0))
        self.chk_others = ttk.Checkbutton(
            row_others, text='带上不支持的文件',
            variable=self.copy_others, bootstyle='info-round-toggle'
        )
        self.chk_others.pack(side=LEFT)
        tip(self.chk_others,
            'mp4、txt、psd 这类不参与图像处理的文件，原样复制到输出目录并一起'
            '参与编号：不解码、不重编码，扩展名和文件属性（修改时间等）全保留。\n\n'
            '这样输出目录就是源目录的完整镜像，编号也不会因为跳过而错位。')

        row_anim = ttk.Frame(step4)
        row_anim.pack(fill=X, pady=(8, 0))
        self.chk_anim = ttk.Checkbutton(
            row_anim, text='动图也重编码',
            variable=self.reencode_anim, bootstyle='warning-round-toggle'
        )
        self.chk_anim.pack(side=LEFT)
        tip(self.chk_anim,
            '默认关闭：动图（多帧 WebP/GIF、多页 TIFF）在不旋转时原样复制，'
            '帧、帧延时和文件属性全保留。\n\n'
            '开启后按画质设置整段重编码 —— 慢得多、内存吃得多，而且有损源'
            '再编一遍往往比原来还大（实测涨到 142%）。选了旋转时无论开关'
            '如何都必须重编码。')
        ttk.Label(row_anim, text='慢且通常更大，除非你确实要换画质',
                  foreground='gray', font=('', 8)).pack(side=LEFT, padx=8)

        self.lbl_safe = ttk.Label(row_safe, text='', foreground='gray', font=('', 8))
        self.lbl_safe.pack(side=LEFT, padx=8)

        info_frame = ttk.Frame(step4)
        info_frame.pack(fill=X, pady=(10, 0))
        ttk.Label(info_frame, text='✓ 保留 ICC 色彩配置  ✓ 保留文件时间  ✓ 自动修正 Exif 旋转',
                  foreground='gray', font=('', 9)).pack(anchor=W)
        ttk.Label(info_frame, text='⚠️ PNG 网络压缩模式会进行色彩量化 (有损)',
                  foreground='orange', font=('', 9)).pack(anchor=W)

    # 点「高级设置 / 返回简易」：在两套面板之间切换。
    def toggle_mode(self):
        self.set_mode(advanced=not self.advanced_mode)

    def set_mode(self, advanced: bool):
        """在简易/高级之间切换。两块面板互斥，执行入口也跟着只留一个。"""
        self.advanced_mode = advanced

        if advanced:
            self.simple_frame.pack_forget()
            self.advanced_frame.pack(fill=X)
            # 高级模式才需要「开始执行」；简易模式下一键按钮自己就会跑
            self.btn_run.pack(side=LEFT, padx=(0, 10), before=self.btn_cancel)
            self.btn_mode.configure(text=i18n.tr('← 返回简易模式'), bootstyle='secondary')
            self.lbl_mode.configure(text=i18n.tr('高级模式 —— 自定义参数后点「开始执行」'))
        else:
            self.advanced_frame.pack_forget()
            self.simple_frame.pack(fill=X)
            self.btn_run.pack_forget()
            self.btn_mode.configure(text=i18n.tr('⚙ 高级设置...'), bootstyle='info')
            self.lbl_mode.configure(text=i18n.tr('简易模式 —— 选好文件夹，点一个按钮就行'))

        self._fit_content()
        self.fit_window()

    def fit_window(self):
        """把窗口调到刚好装下当前模式的内容，**并且保证整个窗口还在屏幕里**。

        三件事：
        1. 宽度也要自适应。原来只算高度、宽度直接沿用 winfo_width()，
           高 DPI 下内容需要 1132px 而窗口一直是 720px，右边整整少一块。
        2. 高宽都夹在屏幕可用范围内，装不下的交给滚动条。
        3. 切到高级模式窗口会长高，如果原来就靠近屏幕下沿，长出来的部分会
           **伸到屏幕外面**（按钮跑到看不见的地方）。所以尺寸定完还要把左上角
           往回挪，保证右下角不越界。

        用户自己拖过窗口之后就不再插手 —— 他既然调成那样，切个模式又被弹回去
        是很烦的。
        """
        # 尺寸只在启动时定一次。切换简易/高级不该让窗口跳一下 —— 内容装不下
        # 交给滚动条，那是滚动区本来就该干的事。
        if getattr(self, '_user_resized', False) or getattr(self, '_size_locked', False):
            self._sync_hbar()
            return

        root = self.root
        root.update_idletasks()

        frame = self._scrollable_frame
        bar_w = 0
        try:
            bar_w = self._canvas.master.winfo_children()[1].winfo_reqwidth()
        except Exception:
            bar_w = int(16 * self.ui_scale())

        need_w = frame.winfo_reqwidth() + bar_w + 4
        need_h = frame.winfo_reqheight() + 8

        # 屏幕可用范围。留一点余量给任务栏和窗口边框。
        margin_w = int(40 * self.ui_scale())
        margin_h = int(80 * self.ui_scale())
        max_w = root.winfo_screenwidth() - margin_w
        max_h = root.winfo_screenheight() - margin_h

        min_w, min_h = root.minsize()
        width = max(min_w, min(need_w, max_w))
        height = max(min_h, min(need_h, max_h))

        # 挪位置：先保证不越过右/下边界，再保证不越过左/上边界。
        x, y = root.winfo_x(), root.winfo_y()
        x = min(x, root.winfo_screenwidth() - width - 8)
        y = min(y, root.winfo_screenheight() - height - 8)
        x, y = max(x, 0), max(y, 0)

        self._auto_sizes.add((width, height))
        root.geometry(f'{width}x{height}+{x}+{y}')
        self._sync_hbar()

    def _on_root_configure(self, event):
        """区分「我们自己调的」和「用户拖的」。

        尺寸出现在 _auto_sizes 里就是自己人；对不上的才是用户手动拖的，
        之后 fit_window 就不再插手。
        """
        if event.widget is not self.root or getattr(self, '_user_resized', False):
            return
        if (event.width, event.height) not in self._auto_sizes:
            self._user_resized = True

    # 设置改变后刷新控件状态，比如没选「导出到新文件夹」时，导出路径框变灰。
    def update_ui(self):
        is_new = self.out_mode.get() == 'new'
        state_new = 'normal' if is_new else 'disabled'
        self.ent_new.configure(state=state_new)
        self.btn_new.configure(state=state_new)
        self.chk_struct.configure(state=state_new)

        is_struct = self.keep_structure.get()
        state_suf = 'normal' if is_new and is_struct else 'disabled'
        self.ent_suf.configure(state=state_suf)
        self.lbl_suf.configure(bootstyle='default' if is_new and is_struct else 'secondary')

        is_keep = self.name_mode.get() == 'keep'
        is_number = self.name_mode.get() == 'number'
        state_pre = 'disabled' if is_number or is_keep else 'normal'
        self.ent_pre.configure(state=state_pre)
        self.cb_digit.configure(state='disabled' if is_keep else 'readonly')
        self.ent_start.configure(state='disabled' if is_keep else 'normal')

        is_ico = self.is_ico.get()
        if is_ico:
            self.ico_detail_frame.pack(fill=X, before=self.row_perf)
        else:
            self.ico_detail_frame.pack_forget()

        limit = dict(PERF.MEMORY_LIMIT_CHOICES).get(self.mem_limit_val.get(), 0)
        if limit:
            self.lbl_mem.configure(text=i18n.tr(f'固定 {limit} MB'))
        else:
            self.lbl_mem.configure(
                text=f'当前约 {PERF.memory_budget_mb(self.perf_mode.get()):.0f} MB')

        if self.safe_write.get():
            self.lbl_safe.configure(text=i18n.tr('(处理期间临时占用约一倍磁盘空间)'))
        else:
            self.lbl_safe.configure(text=i18n.tr('(省磁盘，但中途取消只能停在当前进度)'))

        # 并发数只在「自定义」下可改，其余模式由性能模式算出来
        mode = self.perf_mode.get()
        self.spin_workers.configure(
            state='normal' if mode == PERF.CUSTOM else 'disabled')
        self.lbl_perf.configure(
            text=i18n.tr(PERF.describe(mode, self.max_workers.get())))

        self.update_preview()

    # 刷新文件名预览（例：photo.jpg → IMG_001.jpg）。
    def update_preview(self, *args):
        mode = self.name_mode.get()
        is_ico = self.is_ico.get()
        ext = '.ico' if is_ico else '.jpg'

        if mode == 'keep':
            preview = f'photo.jpg → photo{ext}'
        elif parse_digit_count(self.digit_val.get()) is None:
            # 自动位数要等扫描完才知道宽度，这里给不出确切数字，就把规则写出来
            try:
                start = self.start_num.get()
            except Exception:
                start = 1
            sample = str(start).zfill(4)
            preview = f'photo.jpg → {sample}{ext}'
        else:
            try:
                padding = parse_digit_count(self.digit_val.get())
                start = self.start_num.get()
                num_str = str(start).zfill(padding)

                if mode == 'prefix':
                    prefix = self.prefix_val.get() or 'IMG'
                    new_name = f'{prefix}_{num_str}{ext}'
                else:
                    new_name = f'{num_str}{ext}'

                preview = f'photo.jpg → {new_name}'
            except:
                preview = f'photo.jpg → ???{ext}'

        if HEIC_SUPPORTED:
            preview += '  |  photo.heic → *.jpg'

        if is_ico:
            mode_desc = self.ico_mode_val.get()
            if mode_desc == 'multi':
                preview += ' (标准多尺寸 256-16)'
            elif 'fixed' in mode_desc:
                preview += f' ({mode_desc.split("_")[1]}px 固定)'
            elif 'auto' in mode_desc:
                preview += f' ({mode_desc.split("_")[1]}px 智能)'

        self.lbl_preview.configure(text=i18n.tr(preview))
