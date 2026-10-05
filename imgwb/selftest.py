"""自测：让程序（尤其是打包出来的 exe）在**当前这台机器上**把自己真跑一遍。

    ImageWorkbenchV4ce.exe --selftest --cli-out report.json
    python 融合_v4ce.py --selftest --cli-out report.json

不需要 Python、不需要任何外部文件：样图是它自己造的，造在临时目录里，
跑完删掉（`--work 文件夹` 可以指定造在哪个文件夹**底下**；那个文件夹里
原有的东西不会动）。做两件事，结果写成一份 JSON 报告，退出码 0 = 全过：

1. **不开窗口跑一批**（200 多张，超过这个数才会用多进程）：导出 + 旋转，
   再原地改名一批。核对每个产物在、能解码、方向对、动图帧数对、
   保留了源照片的修改时间、没留下临时文件。
2. **开真的窗口、按真的按钮跑一批**：走和双击启动完全相同的启动流程，
   在界面上切到高级模式、填好设置、**调用「开始执行」按钮本身**，
   等它跑完，核对产物、状态栏、弹过的对话框，然后走正常的关窗流程。
   顺带记下窗口和屏幕的尺寸、界面缩放、内容有没有被切在窗口外面。

# ============================================================
# 【导读】为什么要有它：`完整自检.py` 只能在装了 Python 的开发机上跑。
# 「换一台干净的电脑能不能用」「英文系统上行不行」「200% 缩放下界面
# 对不对」这几件事，开发机上验不了 —— 只能把验证本身带到那台机器上去。
# 这个入口就是干这个的：拿到 exe 的人双击不了它来自检，但一条命令可以。
# 报告里记着系统版本、界面语言和缩放比例，是在什么环境下通过的一目了然。
# ============================================================

**对话框在自测里不会真的弹出来**（没人去点），而是记进报告。除此之外
界面这条路上没有任何替身：窗口、主循环、按钮、后台线程、工作进程都是真的。
"""
import json
import os
import shutil
import sys
import tempfile
import time

# 多进程的门槛是 200 张（config.PROCESS_THRESHOLD）。少于这个数用的是线程，
# 打包版最容易出问题的那一层 —— 工作子进程 —— 就根本没被跑到。
BATCH = 210
OLD_PHOTO_SECONDS = 2 * 86400       # 源照片的修改时间设成两天前


def parse(argv):
    """`--selftest` 后面的参数。认不出的直接忽略，自测不该因为参数写错而不跑。"""
    opts = {'out': None, 'gui': True, 'lang': None, 'scale': None, 'work': None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--cli-out' and i + 1 < len(argv):
            opts['out'] = argv[i + 1]
            i += 1
        elif a == '--no-gui':
            opts['gui'] = False
        elif a == '--lang' and i + 1 < len(argv):
            opts['lang'] = argv[i + 1]
            i += 1
        elif a == '--scale' and i + 1 < len(argv):
            try:
                opts['scale'] = float(argv[i + 1])
            except ValueError:
                pass
            i += 1
        elif a == '--work' and i + 1 < len(argv):
            opts['work'] = argv[i + 1]
            i += 1
        i += 1
    return opts


def environment():
    """在什么环境下跑的。报告靠它说清楚"这次通过"到底覆盖了什么。"""
    import platform

    from . import i18n
    from .config import HEIC_SUPPORTED
    from .utils import INSTANCE_TAG, SCOPE_TAG

    env = {
        'frozen': bool(getattr(sys, 'frozen', False)),
        'executable': sys.executable,
        'python': platform.python_version(),
        'windows': platform.platform(),
        'heic': bool(HEIC_SUPPORTED),
        'app_language': getattr(i18n, '_lang', '?'),
        'instance': INSTANCE_TAG,
        'scope': SCOPE_TAG,
        'temp': tempfile.gettempdir(),
    }
    try:
        import ctypes

        env['system_ui_language'] = hex(
            ctypes.windll.kernel32.GetUserDefaultUILanguage())
        env['system_dpi'] = int(ctypes.windll.user32.GetDpiForSystem())
        env['system_scale_percent'] = round(env['system_dpi'] / 96 * 100)
    except Exception:
        pass
    return env


def make_samples(folder, count, with_heic):
    """造一批样图：`count` 张 JPEG、一张 3 帧 GIF，支持的话再加一张 HEIC。
    返回文件名列表和设给它们的修改时间。"""
    from PIL import Image

    os.makedirs(folder, exist_ok=True)
    base = Image.new('RGB', (96, 64))
    base.putdata([((x * 3) % 256, (y * 4) % 256, (x ^ y) % 256)
                  for y in range(64) for x in range(96)])
    names = []
    for i in range(count):
        names.append(f'p{i:03d}.jpg')
        base.save(os.path.join(folder, names[-1]), quality=90)
    frames = [Image.new('RGB', (96, 64), c) for c in ('red', 'green', 'blue')]
    frames[0].save(os.path.join(folder, 'anim.gif'), save_all=True,
                   append_images=frames[1:], duration=[80, 90, 100], loop=0)
    names.append('anim.gif')
    if with_heic:
        try:
            base.save(os.path.join(folder, 'phone.heic'), quality=90)
            names.append('phone.heic')
        except Exception:
            pass
    stamp = int(time.time() - OLD_PHOTO_SECONDS)
    for n in names:
        # 只动**源照片**的时间。产物要保留它，而且临时文件不能因此被当垃圾。
        os.utime(os.path.join(folder, n), (stamp, stamp))
    return names, stamp


def check_outputs(final_dir, names, stamp, rotated=True):
    """产物对不对。返回问题列表，空 = 全对。**看的是磁盘，不是返回值。**"""
    from PIL import Image

    from .tasks import BACKUP_MARK, temp_owner

    problems = []
    try:
        produced = sorted(os.listdir(final_dir))
    except OSError as exc:
        return [f'输出目录读不了: {exc}']
    want = sorted(('phone.jpg' if n == 'phone.heic' else n) for n in names)
    junk = [n for n in produced if temp_owner(n) is not None or BACKUP_MARK in n]
    if junk:
        problems.append(f'留下了 {len(junk)} 个临时/备份文件，如 {junk[0]}')
    real = [n for n in produced if n not in junk]
    if real != want:
        extra = [n for n in real if n not in want][:3]
        lost = [n for n in want if n not in real][:3]
        problems.append(f'产物不对：多出 {extra}，缺 {lost}')
        return problems
    size = (64, 96) if rotated else (96, 64)
    try:
        for n in real:
            with Image.open(os.path.join(final_dir, n)) as im:
                im.load()
                if im.size != size:
                    problems.append(f'{n} 尺寸 {im.size}，应为 {size}')
                    break
                if n == 'anim.gif' and getattr(im, 'n_frames', 1) != 3:
                    problems.append(f'动图剩 {getattr(im, "n_frames", 1)} 帧')
    except Exception as exc:
        problems.append(f'产物打不开: {type(exc).__name__}: {exc}')
    drift = [n for n in real
             if abs(int(os.path.getmtime(os.path.join(final_dir, n))) - stamp) > 2]
    if drift:
        problems.append(f'{len(drift)} 个产物没保留源照片的修改时间')
    return problems


def headless(work):
    """不开窗口跑两批。"""
    from . import api
    from .config import HEIC_SUPPORTED

    report = {'problems': []}
    src = os.path.join(work, 'h_src')
    out = os.path.join(work, 'h_out')
    names, stamp = make_samples(src, BATCH, HEIC_SUPPORTED)
    report['files'] = len(names)

    t = time.time()
    r = api.run(src, output='new', dest=out, name='keep', rotate='cw90')
    report['export_seconds'] = round(time.time() - t, 1)
    if r.get('ok') is not True or r.get('processed') != len(names):
        report['problems'].append(
            f'导出批：ok={r.get("ok")} 处理 {r.get("processed")}/{len(names)} '
            f'{str(r.get("failed"))[:200]}')
    report['problems'] += ['导出批：' + p for p in check_outputs(
        os.path.join(out, 'h_src_已处理'), names, stamp)]

    # 原地改名一批：会顶替/删掉原文件的那条路（备份、提交、刷盘都在这条路上）
    inplace = os.path.join(work, 'h_inplace')
    names2, _stamp2 = make_samples(inplace, 30, False)
    r = api.run(inplace, output='inplace', name='prefix', prefix='ST', digits=3)
    left = sorted(os.listdir(inplace))
    if (r.get('ok') is not True or len(left) != len(names2)
            or not all(n.startswith('ST_') for n in left)):
        report['problems'].append(
            f'原地改名批：ok={r.get("ok")}，目录里是 {left[:4]}…共 {len(left)} 个 '
            f'{str(r.get("failed"))[:200]}')
    return report


def layout(app):
    """窗口和内容的尺寸：窗口在不在屏幕里、内容有没有被切在窗口外面够不着。"""
    root = app.root
    root.update_idletasks()
    info = {
        'ui_scale': round(app.ui_scale(), 3),
        'screen': [root.winfo_screenwidth(), root.winfo_screenheight()],
        'window': [root.winfo_width(), root.winfo_height()],
        'window_pos': [root.winfo_rootx(), root.winfo_rooty()],
    }
    try:
        frame, canvas = app._scrollable_frame, app._canvas
        info['content_needs'] = [frame.winfo_reqwidth(), frame.winfo_reqheight()]
        info['canvas'] = [canvas.winfo_width(), canvas.winfo_height()]
        bar = getattr(app, '_hbar', None)
        info['hbar_shown'] = bool(bar is not None and bar.winfo_ismapped())
        region = [float(v) for v in str(canvas.cget('scrollregion')).split()]
        info['scroll_region'] = region
    except Exception as exc:
        info['error'] = f'{type(exc).__name__}: {exc}'
    return info


def layout_problems(info, label):
    out = []
    if 'error' in info:
        return [f'{label}：量不到界面尺寸（{info["error"]}）']
    sw, sh = info['screen']
    w, h = info['window']
    x, y = info['window_pos']
    if w > sw or h > sh:
        out.append(f'{label}：窗口 {w}x{h} 比屏幕 {sw}x{sh} 还大')
    if x < -8 or y < -8 or x + w > sw + 16 or y + h > sh + 16:
        out.append(f'{label}：窗口伸到屏幕外面了（位置 {x},{y} 尺寸 {w}x{h}）')
    need_w = info['content_needs'][0]
    can_w = info['canvas'][0]
    region = info.get('scroll_region') or [0, 0, 0, 0]
    reachable = region[2] - region[0] if len(region) == 4 else 0
    # 内容比可见区宽的话，必须滚得到：有横向滚动条，而且滚动范围盖得住内容
    if need_w > can_w + 2 and not (info.get('hbar_shown')
                                   and reachable >= need_w - 2):
        out.append(f'{label}：内容宽 {need_w} 而可见区只有 {can_w}，'
                   f'又没有横向滚动条够得着（滚动范围 {reachable:.0f}）')
    return out


def drive_gui(app, work, report):
    """在已经建好的真窗口上自动操作一遍。结果写进 `report`，结束时关窗。

    调用方随后进 `app.root.mainloop()`；这里只负责排好一连串定时回调。
    """
    from . import app as app_mod
    from . import i18n
    from .config import HEIC_SUPPORTED

    gui = report.setdefault('gui', {'problems': [], 'dialogs': []})
    src = os.path.join(work, 'g_src')
    out = os.path.join(work, 'g_out')
    names, stamp = make_samples(src, BATCH, HEIC_SUPPORTED)
    gui['files'] = len(names)
    state = {'phase': 'wait_window', 'since': time.time()}

    def note(kind):
        def record(title='', message='', *a, **k):
            gui['dialogs'].append([kind, str(title), str(message)[:300]])
            return True
        return record

    # 对话框是模态的，自测里没人去点 —— 记下来代替弹出。别的都不换。
    for kind in ('showinfo', 'showwarning', 'showerror'):
        setattr(app_mod.messagebox, kind, note(kind))
    app_mod.messagebox.askokcancel = note('askokcancel')
    app._ask_merge_choice = lambda *a, **k: 'merge'

    def fail(msg):
        gui['problems'].append(msg)
        finish()

    def finish():
        if state['phase'] == 'closing':
            return
        state['phase'] = 'closing'
        gui['status_text'] = str(app.status.cget('text'))
        try:
            app.on_close()              # 正常的关窗流程
        except Exception as exc:
            gui['problems'].append(f'关窗出错: {type(exc).__name__}: {exc}')
            app.root.quit()

    def step():
        try:
            _step()
        except Exception as exc:
            import traceback
            gui['problems'].append(
                f'自动操作出错: {type(exc).__name__}: {exc} '
                + traceback.format_exc()[-400:])
            finish()

    def _step():
        phase = state['phase']
        waited = time.time() - state['since']
        if phase == 'wait_window':
            if not app.root.winfo_viewable():
                if waited > 30:
                    return fail('30 秒了窗口还没显示出来')
                return app.root.after(100, step)
            gui['window_title'] = str(app.root.title())
            gui['layout_simple'] = layout(app)
            gui['problems'] += layout_problems(gui['layout_simple'], '简易模式')
            app.btn_mode.invoke()       # 点「高级设置」
            state.update(phase='advanced', since=time.time())
            return app.root.after(400, step)
        if phase == 'advanced':
            gui['layout_advanced'] = layout(app)
            gui['problems'] += layout_problems(gui['layout_advanced'], '高级模式')
            if not app.advanced_mode:
                return fail('点了「高级设置」但没有切过去')
            app.src_folder.set(src)
            app.out_mode.set('new')
            app.new_folder_path.set(out)
            app.keep_structure.set(True)
            app.name_mode.set('keep')
            app.rotate_val.set('顺时针 90°')
            app.compress_only.set(False)
            app.is_ico.set(False)
            app.safe_write.set(True)
            gui['rotate_shown_as'] = str(app.root.getvar(str(app.rotate_val)))
            app.btn_run.invoke()        # 点「开始执行」
            state.update(phase='running', since=time.time(), seen_running=False)
            return app.root.after(200, step)
        if phase == 'running':
            if app.is_running:
                state['seen_running'] = True
            elif state['seen_running'] or waited > 5:
                if not state['seen_running']:
                    return fail('点了「开始执行」但没有开始：'
                                + str(gui['dialogs'][-1:] or ''))
                gui['seconds'] = round(waited, 1)
                gui['processed'] = app.processed_count
                gui['total'] = app.total_count
                gui['errors'] = [str(e[1])[:160] for e in app.error_list[:5]]
                if app.error_list:
                    gui['problems'].append(
                        f'界面批：{len(app.error_list)} 个失败，如 {gui["errors"][0]}')
                if app.processed_count != len(names):
                    gui['problems'].append(
                        f'界面批：处理了 {app.processed_count}/{len(names)}')
                gui['problems'] += ['界面批：' + p for p in check_outputs(
                    os.path.join(out, 'g_src' + i18n.default_dir_suffix()),
                    names, stamp)]
                if any(d[0] == 'showerror' for d in gui['dialogs']):
                    gui['problems'].append(
                        f'界面批：弹了报错框 {gui["dialogs"]}')
                # 让「完成」那一行状态和对话框有机会落下来再关窗
                state.update(phase='settle', since=time.time())
                return app.root.after(600, step)
            if waited > 300:
                return fail('界面批 300 秒还没跑完')
            return app.root.after(100, step)
        if phase == 'settle':
            gui['language'] = getattr(i18n, '_lang', '?')
            return finish()

    app.root.after(200, step)


def run(opts, start_gui):
    """整套自测。`start_gui(lang, scale)` 由入口提供 —— 它走的是和双击启动
    一模一样的启动流程（语言、日志、DPI、建窗口），这里不另起炉灶。"""
    started = time.time()
    report = {'ok': False, 'problems': [], 'options': dict(opts)}
    # **`--work` 给的是"放在哪儿"，不是"这个文件夹归我"。** 原来直接拿它当
    # 工作目录，跑完整个 rmtree —— 用户指了一个已有的文件夹，里面原有的东西
    # 就跟着没了（源码和两种 exe 各 10/10）。所以不管给没给 `--work`，
    # 都在那底下另建一个本次独占的随机子目录，造样图、清理都只在它里面；
    # `work` 这个变量从头到尾只指向自己建的那一个。
    work = None
    app = None
    try:
        base = opts.get('work')
        if base:
            os.makedirs(base, exist_ok=True)
        work = tempfile.mkdtemp(prefix='imgwb-selftest-', dir=base or None)
        report['work'] = work
        if opts.get('lang'):
            from . import i18n
            i18n.set_lang(opts['lang'])
        report['environment'] = environment()
        report['headless'] = headless(work)
        report['problems'] += report['headless']['problems']
        if opts.get('gui'):
            app = start_gui(opts.get('lang'), opts.get('scale'))
            report['environment'] = environment()      # 语言在启动流程里才定
            drive_gui(app, work, report)
            try:
                app.root.mainloop()
            finally:
                app.shutdown_workers()
            report['problems'] += report['gui']['problems']
    except Exception as exc:
        import traceback
        report['problems'].append(
            f'{type(exc).__name__}: {exc} ' + traceback.format_exc()[-600:])
    report['ok'] = not report['problems']
    report['seconds'] = round(time.time() - started, 1)
    if report['ok'] and work:
        shutil.rmtree(work, ignore_errors=True)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if opts.get('out'):
        try:
            with open(opts['out'], 'w', encoding='utf-8') as fh:
                fh.write(text)
        except OSError:
            # 要了报告却写不出来，就不能再说"全过" —— 调用方手里什么凭据都没有
            return 1
    elif sys.stdout is not None:
        try:
            sys.stdout.write(text + '\n')
        except Exception:
            pass
    return 0 if report['ok'] else 1
