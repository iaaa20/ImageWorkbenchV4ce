"""悬停提示。

不用 ttkbootstrap 自带的那个：``ToolTip`` 在 1.x 里叫 ``ttkbootstrap.tooltip``、
2.x 才挪到顶层，而这个项目声明支持 ``ttkbootstrap>=1.0``。自己实现四十来行，
省掉版本分支，延时和换行也好控制。
"""

# ============================================================
# 【导读】tooltip.py —— 鼠标悬停时弹出的说明小窗。
# ============================================================
import tkinter as tk

from . import i18n as _i18n

DELAY_MS = 450          # 停多久才弹，太快会在划过时乱闪
WRAP_PX = 300

# 界面用的是 cosmo 浅色主题，深色提示框糊上来太扎眼。用浅底细边框，
# 跟窗口本身是一路的。
BG = '#fdfdfe'
FG = '#33383f'
BORDER = '#c3cad6'


class ToolTip:
    """给单个控件挂一条悬停说明。"""

    # 给某个控件挂上悬停提示：鼠标停一会儿就弹出说明文字。
    def __init__(self, widget, text, delay=DELAY_MS):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id = None
        self._tip = None

        # 一律用 add='+'，别把控件上已有的绑定顶掉（比如滚轮那套）
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    # 鼠标移进控件：开始计时，停够时间才弹提示。
    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    # 取消还没到时间的弹出。
    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    # 真正把提示小窗画出来。
    def _show(self):
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except Exception:
            return

        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)        # 无边框、无标题栏
        tip.wm_geometry(f'+{x}+{y}')
        try:
            tip.wm_attributes('-topmost', True)
        except Exception:
            pass

        # 外层 Frame 只为画一条 1px 细边框，内层 Label 才是浅底文字
        frame = tk.Frame(tip, background=BORDER, padx=1, pady=1)
        frame.pack()
        tk.Label(
            # 提示文字在**弹出的这一刻**才翻译：窗口是每次悬停重建的，
            # 所以切完语言不用去找已经建好的提示窗更新。
            frame, text=_i18n.tr(self.text), justify='left', wraplength=WRAP_PX,
            background=BG, foreground=FG,
            padx=9, pady=6, font=('', 9),
        ).pack()
        self._tip = tip

    # 鼠标移开：关掉提示小窗。
    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


def attach(widget, text, delay=DELAY_MS):
    """挂一条提示并返回它，方便测试时查。"""
    return ToolTip(widget, text, delay)
