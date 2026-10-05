"""悬停提示：内容、弹出/收起，以及性能模式在不同核数机器上的并发策略。

用法:  python tests\test_tooltip.py
会真的建出窗口，需要有桌面会话。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from imgwb import perf
from imgwb.app import ImageProcessorApp
from imgwb.tooltip import ToolTip

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


print('=== 并发策略在各种机器上的表现（同一份代码）===')
print(f'  {"核数":>5s} {"节能":>5s} {"均衡":>5s} {"高性能":>6s}')
rows = []
for cpu in (1, 2, 4, 6, 8, 12, 16, 24, 32, 64, 128):
    e = perf.worker_count(perf.ECO, cpu=cpu)
    b = perf.worker_count(perf.BALANCED, cpu=cpu)
    t = perf.worker_count(perf.TURBO, cpu=cpu)
    rows.append((cpu, e, b, t))
    print(f'  {cpu:5d} {e:5d} {b:5d} {t:6d}')

check('任何核数下都至少 1 个并发', all(min(r[1:]) >= 1 for r in rows))
check('节能 <= 均衡 <= 高性能，处处成立',
      all(r[1] <= r[2] <= r[3] for r in rows))
check('单核机器不会开出多个进程', rows[0][1:] == (1, 1, 1), str(rows[0]))
check('双核机器上节能只用 1 个', rows[1][1] == 1)
check('大机器上也有上限（不会开 128 个进程）',
      rows[-1][1] <= perf.CAP_ECO and rows[-1][2] <= perf.CAP_BALANCED
      and rows[-1][3] <= perf.CAP_TURBO,
      f'128 核 -> {rows[-1][1:]}')
check('自定义可以突破上限但仍有天花板',
      perf.worker_count(perf.CUSTOM, 200) == perf.CAP_CUSTOM
      and perf.worker_count(perf.CUSTOM, 20) == 20)
check('自定义给了垃圾值也能兜住',
      perf.worker_count(perf.CUSTOM, 'abc', cpu=8) >= 1
      and perf.worker_count(perf.CUSTOM, 0, cpu=8) >= 1)

print('\n=== 说明文字要简短，别糊一大块上来 ===')
for mode, info in perf.MODES.items():
    n = len(info['tip'])
    check(f'{info["label"]}（{n} 字）', 10 <= n <= 45, info['tip'])

print('\n=== 配色不能是刺眼的纯黑 ===')
from imgwb import tooltip as tt_mod

r, g, b = (int(tt_mod.BG[i:i + 2], 16) for i in (1, 3, 5))
check('提示背景是浅色', min(r, g, b) > 200, f'{tt_mod.BG} = RGB({r},{g},{b})')
fr, fg_, fb = (int(tt_mod.FG[i:i + 2], 16) for i in (1, 3, 5))
check('文字是深色（浅底深字）', max(fr, fg_, fb) < 120, tt_mod.FG)

print('\n=== 界面上的提示 ===')
app = ImageProcessorApp()
app.set_mode(advanced=True)
app.root.update()


def tips_on(widget):
    """这个控件上挂了几条提示。"""
    n = 0
    for seq in ('<Enter>',):
        try:
            n += len([b for b in widget.bind(seq).splitlines() if b.strip()])
        except Exception:
            pass
    return n


targets = [
    ('仅压缩', app.chk_compress),
    ('转成 ICO', app.chk_ico),
    ('安全写入', app.chk_safe),
    ('带上不支持的文件', app.chk_others),
    ('画质下拉', app.cb_quality),
    ('位数下拉', app.cb_digit),
]
for mode, btn in app.perf_buttons.items():
    targets.append((f'性能模式-{perf.MODES[mode]["label"]}', btn))
for key, btn in app.preset_buttons.items():
    targets.append((f'一键-{key}', btn))

missing = [name for name, w in targets if tips_on(w) == 0]
check('该有提示的控件都挂上了', not missing, '缺: ' + '、'.join(missing) if missing else
      f'{len(targets)} 个控件')

print('\n=== 提示能弹出、也能收回 ===')
probe = app.chk_safe
tt = ToolTip(probe, '测试提示内容', delay=1)
probe.event_generate('<Enter>')
app.root.update()
app.root.after(60, app.root.quit)
app.root.mainloop()
check('悬停后弹出', tt._tip is not None)
if tt._tip is not None:
    # 结构是 Toplevel -> 边框 Frame -> Label
    texts = []
    for frame in tt._tip.winfo_children():
        for child in frame.winfo_children():
            try:
                texts.append(child.cget('text'))
            except Exception:
                pass
    check('内容正确', '测试提示内容' in texts, str(texts))
probe.event_generate('<Leave>')
app.root.update()
check('移开后收回', tt._tip is None)

print('\n=== 提示不会顶掉控件原有的绑定 ===')
# 下拉框上既有滚轮处理（防改值）又有提示，两者必须共存
before = app.cb_quality.cget('values')
app.cb_quality.event_generate('<MouseWheel>', delta=-120, x=5, y=5)
app.root.update()
check('滚轮仍不会改下拉框的值',
      app.quality_val.get() == '通用高清 (95%)', app.quality_val.get())
check('下拉框选项没被动过', app.cb_quality.cget('values') == before)

app.root.destroy()
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
