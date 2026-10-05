"""开工前的预扫描：认清每个文件，说清范围、时间和问题。

用户的要求原话是「开始工作前扫描一遍所有待处理文件，识别文件，再针对性地调用
相应的模块，明确处理的范围及时间，如果出现问题，告知相关的错误」。

为什么值得单独做一遍：**扩展名不等于真实格式**。真实素材里就有两张 PNG 被
改名成 `.jpg`，后果不止是内存估错 ——

  * 闸门按源格式估，实测低估到 **0.48 倍**（会撑爆内存）
  * 目标按扩展名走，RGBA 的 PNG 被编成 JPEG，**透明通道静默丢掉**
    （实测 17.5 MB RGBA -> 5.9 MB JPEG/RGB），而界面只报「成功」

预扫描必须**并行**跑。3000 张在主线程上一张张认要十几秒，而那段时间工人全闲着
—— 跟 `probe_peak` 在主线程上解 PNG 是同一类毛病，见 `test_probe.py`。

用法:  python tests\test_scan.py
需要桌面会话，约十秒。
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image

from imgwb import app as app_mod
from imgwb.app import ImageProcessorApp

app_mod.messagebox.showinfo = lambda *a, **k: None
app_mod.messagebox.showwarning = lambda *a, **k: None
app_mod.messagebox.showerror = lambda *a, **k: None
app_mod.messagebox.askokcancel = lambda *a, **k: True

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def noisy(im):
    """铺一层噪点，免得纯色图被「再压也省不下」直接跳过。"""
    px = im.load()
    w, h = im.size
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            px[x, y] = ((x * 7) % 256, (y * 13) % 256, (x ^ y) % 256)[:len(im.getbands())]
    return im


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, 'src')
    os.makedirs(src)

    # 正常的 JPEG 和 PNG
    noisy(Image.new('RGB', (1600, 1200))).save(
        os.path.join(src, 'ok.jpg'), quality=100, subsampling=0)
    noisy(Image.new('RGB', (1600, 1200))).save(os.path.join(src, 'ok.png'))
    # **PNG 改名成 .jpg，而且带透明** —— 真实素材里就有这种
    im = Image.new('RGBA', (1600, 1200), (200, 30, 40, 128))
    noisy(im).save(os.path.join(src, 'liar.jpg'), format='PNG')
    # 打不开的
    with open(os.path.join(src, 'broken.jpg'), 'wb') as fh:
        fh.write(b'this is definitely not an image')
    # 不支持的格式，跟着编号带走
    with open(os.path.join(src, 'notes.txt'), 'w', encoding='utf-8') as fh:
        fh.write('hello')

    app = ImageProcessorApp()
    app.root.withdraw()
    app._ask_merge_choice = lambda *a, **k: 'merge'
    app.src_folder.set(src)
    app.out_mode.set('new')
    app.new_folder_path.set(os.path.join(work, 'out'))
    app.name_mode.set('keep')
    app.compress_only.set(True)
    app.quality_val.set('通用高清 (95%)')

    t0 = time.time()
    app.start_processing()

    def poll():
        if not app.is_running or time.time() - t0 > 180:
            app.root.after(400, app.root.quit)
            return
        app.root.after(50, poll)

    app.root.after(50, poll)
    app.root.mainloop()
    log = app.log_text.get('1.0', 'end')

    print('一、说清处理范围和时间')
    check('报了处理范围', '识别完成' in log and '个文件' in log,
          next((l.strip() for l in log.splitlines() if '识别完成' in l), '没有'))
    check('报了预计耗时', '预计耗时约' in log,
          next((l.strip() for l in log.splitlines() if '预计耗时' in l), '没有'))
    check('按真实格式给了分布', '格式分布' in log,
          next((l.strip() for l in log.splitlines() if '格式分布' in l), '没有'))

    print('\n二、出问题要在开工前说，而且说清是哪个文件')
    # 必须点名到具体文件，光说「打不开 1 个」等于没说
    named = [l.strip() for l in log.splitlines()
             if 'broken.jpg' in l and '打不开' in l]
    check('报了打不开的文件、并点了名', bool(named),
          named[0] if named else '只报了数量，没说是哪个')
    check('报了扩展名与真实格式不符', 'liar.jpg' in log and '对不上' in log,
          next((l.strip() for l in log.splitlines() if '对不上' in l), '没有'))
    check('报了透明通道会丢', '透明会丢' in log,
          next((l.strip() for l in log.splitlines() if '透明' in l), '没有'))

    print('\n三、识别的结果真的被后面用上了')
    info = app._file_info
    check('每个文件都认过了', len(info) >= 5, f'{len(info)} 条')
    liar = info.get(os.path.join(src, 'liar.jpg'))
    check('认出 liar.jpg 真实格式是 PNG',
          liar is not None and liar.fmt == 'PNG',
          liar.fmt if liar else '没这条')
    check('liar.jpg 被标记为错名 + 会丢透明',
          liar is not None and liar.mislabeled and liar.alpha_loss)
    # 闸门要用的预估值必须在预扫描阶段就算好，否则主线程还得一张张现算
    ok_jpg = info.get(os.path.join(src, 'ok.jpg'))
    check('预估内存已经算好', ok_jpg is not None and ok_jpg.est_mb > 0,
          f'{ok_jpg.est_mb:.0f} MB' if ok_jpg else '没这条')
    broken = info.get(os.path.join(src, 'broken.jpg'))
    check('打不开的那个归类为 error',
          broken is not None and broken.plan == 'error',
          broken.plan if broken else '没这条')

    print('\n四、预扫描不许把正常处理搞挂')
    out = os.path.join(work, 'out')
    made = sum(len(fs) for _, _, fs in os.walk(out)) if os.path.isdir(out) else 0
    check('正常文件照样处理完了', made >= 4, f'产出 {made} 个')
    check('只有那一个坏文件失败', len(app.error_list) == 1,
          f'{len(app.error_list)} 个失败')

    app.shutdown_workers()
    app.root.destroy()
    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
