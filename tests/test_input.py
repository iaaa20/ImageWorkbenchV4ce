"""输入输出这一头的四件事，都是用户报过的。

**一、GIF 是受支持的输入格式。** 动图写回那套（duration / loop / disposal）
一直是齐的，但 `.gif` 不在 `VALID_EXTENSIONS` 里 —— 于是用户勾了「动图也重编码」
拖进一个 GIF，界面照样说「格式不支持，已忽略」，只把它原样带走。

**二、另存位置要有个像样的默认。** 填源文件夹的**上一级** —— 输出实际落在
`<导出位置>\<源文件夹名>_已处理\`，填上一级正好让产物成为源的邻居。
不能填源文件夹本身：扫描会把自己的产物当输入再扫一遍，`output_escape_error()`
就是为此把这种情况拦下来的。

**三、导出位置也要能拖。** 窗口级的拖拽是设来源，控件上再注册一次会优先命中，
两边分工清楚。拖文件进去就取它所在的目录。

**四、地址栏一次能填多条路径**（分号或换行隔开）。每条各自成根，各得一个
「文件夹名_已处理」，跟一次拖进来几个文件夹一个待遇。

用法:  python tests\test_input.py
需要桌面会话，约十秒。
"""
import os, shutil, sys, tempfile, time
# 用相对路径定位项目，别写死本机路径 —— 否则别人 clone 下来根本跑不了
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from PIL import Image
from imgwb import app as app_mod
from imgwb.app import ImageProcessorApp
app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None
app_mod.messagebox.askokcancel = lambda *a, **k: True

ok = []


def check(name, cond, detail=''):
    print(f'  {"OK  " if cond else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    ok.append(cond)


class Ev:
    def __init__(self, data): self.data = data


def main():
    work = tempfile.mkdtemp()
    # GIF 必须被当成图片，而不是「不支持的格式」
    from imgwb.config import VALID_EXTENSIONS
    from imgwb.pipeline import get_quality_params
    print('零、GIF 是受支持的输入格式')
    check('.gif 在支持列表里', '.gif' in VALID_EXTENSIONS, str(VALID_EXTENSIONS))
    # 这条断言原来是反的（要求「GIF 有保存参数」），一直打印 FAIL —— 而当时
    # 脚本失败也返回 0，所以没人看见。现行约定是**GIF 刻意不压**：
    # 返回空参数，`recompress_pointless` 据此按「没有可调的画质参数」跳过，
    # 文件原样复制。减色试过，收益小风险大，撤掉了（见 get_quality_params）。
    check('GIF 刻意不给画质参数（现行约定：不压，原样带走）',
          get_quality_params('.gif', '通用高清 (95%)') == {},
          str(get_quality_params('.gif', '通用高清 (95%)')))

    a = os.path.join(work, '甲'); b = os.path.join(work, '乙')
    for d in (a, b):
        os.makedirs(d)
        for i in range(3):
            Image.new('RGB', (200, 150), (i*70 % 256, 90, 160)).save(
                os.path.join(d, f'{os.path.basename(d)}_{i}.jpg'), quality=100)

    app = ImageProcessorApp(); app.root.withdraw()
    app._ask_merge_choice = lambda *a, **k: 'merge'

    print('一、另存默认落在源文件夹旁边')
    app.new_folder_path.set('')
    app._default_dest_from(a)
    check('自动填上了源的上一级', app.new_folder_path.get() == os.path.abspath(work),
          app.new_folder_path.get())
    app.new_folder_path.set(r'D:\自己选的')
    app._default_dest_from(a)
    check('用户已选过就不覆盖', app.new_folder_path.get() == r'D:\自己选的',
          app.new_folder_path.get())

    print('\n二、导出位置支持拖拽')
    app._drop_dest(Ev('{' + b + '}'))
    check('拖文件夹进去设成导出位置', app.new_folder_path.get() == b,
          app.new_folder_path.get())
    f = os.path.join(a, '甲_0.jpg')
    app._drop_dest(Ev('{' + f + '}'))
    check('拖文件进去取它所在目录', app.new_folder_path.get() == a,
          app.new_folder_path.get())

    print('\n三、地址栏一次填多条路径')
    out = os.path.join(work, 'out')
    app.new_folder_path.set(out)
    app.src_folder.set(f'{a};{b}')
    app.out_mode.set('new'); app.name_mode.set('keep')
    app.compress_only.set(True); app.quality_val.set('通用高清 (95%)')
    t0 = time.time(); app.start_processing()

    def poll():
        if not app.is_running or time.time() - t0 > 180:
            app.root.after(400, app.root.quit); return
        app.root.after(50, poll)
    app.root.after(50, poll); app.root.mainloop()

    dirs = sorted(os.listdir(out)) if os.path.isdir(out) else []
    made = sum(len(fs) for _, _, fs in os.walk(out)) if os.path.isdir(out) else 0
    check('两条路径各得一个输出文件夹', len(dirs) == 2, str(dirs))
    check('6 张全处理了', made == 6, f'{made} 个')
    check('日志里报了源路径条数',
          '2 条源路径' in app.log_text.get('1.0', 'end'),
          next((l.strip() for l in app.log_text.get('1.0','end').splitlines()
                if '源路径' in l), '没有'))

    app.shutdown_workers(); app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    bad = ok.count(False)
    print('\n' + ('全部通过' if not bad else f'失败 {bad} 项'))
    # **失败必须返回非零。** 原来只打印一句「有失败」就以 0 退出 ——
    # 于是一条过时断言在这里红了很久，而按退出码跑的回归一路报「全过」。
    return 1 if bad else 0


if __name__ == '__main__':
    import multiprocessing; multiprocessing.freeze_support()
    sys.exit(main())
