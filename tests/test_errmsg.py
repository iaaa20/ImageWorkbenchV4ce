"""失败时必须说清楚「具体是什么错」，并且把技术细节留进日志。

用户的两句话是这个测试的来由：

  「你要给出具体是什么错误」
  「你这个日志现在根本没用，因为你查 bug 都不看日志，这日志记录的有什么用」

**第一条**：原来空文件、改了扩展名的文本、下载到一半的图，三种完全不同的毛病
回的是同一句「无法识别的图片格式或文件损坏」。用户既不知道发生了什么，也不知道
该怎么办。现在按文件头说清楚是哪一种。

**第二条**：工作子进程里出的错，原来只把异常翻译成一句中文就扔了 —— 异常类型和
调用栈全丢。日志里只剩那句中文，查 bug 的人拿到日志还是得自己去复现，那日志就
白记了。现在技术细节随 `TaskResult.detail` 带回主进程，由主进程写进日志文件
（子进程不自己写日志：多个进程抢同一个文件容易写乱）。

用法:  python tests\\test_errmsg.py
不建窗口，几秒。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from PIL import Image

from imgwb import pipeline as pl
from imgwb.config import ProcessConfig

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def cfg():
    return ProcessConfig(rotate_mode='不旋转', quality_opt='通用高清 (95%)',
                         compress_only=True, is_ico=False)


def main():
    work = tempfile.mkdtemp()
    src = os.path.join(work, 'src')
    out = os.path.join(work, 'out')
    os.makedirs(src)
    os.makedirs(out)

    # 一张真图当对照组
    good = os.path.join(src, 'good.jpg')
    Image.new('RGB', (300, 200), (60, 120, 180)).save(good, quality=92)

    # 几种完全不同的坏法
    real_png = os.path.join(src, '_real.png')
    Image.new('RGB', (240, 180), (200, 60, 60)).save(real_png)
    png_bytes = open(real_png, 'rb').read()
    os.remove(real_png)

    bad = {
        'empty.jpg': b'',                                   # 空文件
        'text.jpg': b'this is a note, not a picture',       # 其实是文本
        'pdf.jpg': b'%PDF-1.7\n1 0 obj\n<<>>',              # 其实是 PDF
        'zip.jpg': b'PK\x03\x04\x14\x00\x00\x00ab',         # 其实是压缩包
        'half.png': png_bytes[:60],                         # 下载到一半
    }
    for name, content in bad.items():
        with open(os.path.join(src, name), 'wb') as fh:
            fh.write(content)

    print('一、对照组：好图要能处理')
    r = pl.process_single_image((good, os.path.join(out, 'good.jpg'),
                                 cfg(), False, False))
    check('正常图片处理成功', r.success, str(r.error_msg))

    print('\n二、不同的坏法要给出不同的说法')
    msgs = {}
    for name in bad:
        r = pl.process_single_image((os.path.join(src, name),
                                     os.path.join(out, name), cfg(), False, False))
        msgs[name] = r.error_msg or ''
        print(f'    {name:<12} -> {r.error_msg}')
        check(f'{name} 确实判为失败', not r.success)
        check(f'{name} 带了技术细节（给日志用）', bool(r.detail),
              str(r.detail)[:60])

    check('**五种坏法给出五种不同的说明**（原来是同一句）',
          len(set(msgs.values())) == len(msgs),
          f'只有 {len(set(msgs.values()))} 种说法')
    check('空文件说得出是空的', '空' in msgs['empty.jpg'], msgs['empty.jpg'])
    check('文本文件说得出不是图片',
          '不是图片' in msgs['text.jpg'], msgs['text.jpg'])
    check('PDF 说得出像 PDF', 'PDF' in msgs['pdf.jpg'], msgs['pdf.jpg'])
    check('压缩包说得出像压缩包',
          'ZIP' in msgs['zip.jpg'] or '压缩' in msgs['zip.jpg'], msgs['zip.jpg'])
    check('截断的图要说"坏了/不完整"，而不是"不是图片"',
          ('损坏' in msgs['half.png'] or '不完整' in msgs['half.png'])
          and '不是图片' not in msgs['half.png'], msgs['half.png'])

    print('\n三、没预料到的异常：必须带回完整调用栈')
    # 没人预料到的错（比如依赖库某天改了行为）最需要调用栈，
    # 而这类错原来只留一句 str(e)，经常还是空的。
    real_fn = pl.get_quality_params

    def boom(*a, **k):
        raise ValueError('假装编码器内部炸了')

    pl.get_quality_params = boom
    try:
        r = pl.process_single_image((good, os.path.join(out, 'x.jpg'),
                                     cfg(), False, False))
    finally:
        pl.get_quality_params = real_fn
    check('失败了', not r.success, str(r.error_msg))
    check('用户看得到异常类型', 'ValueError' in (r.error_msg or ''), str(r.error_msg))
    check('**日志细节里有完整调用栈**',
          'Traceback' in (r.detail or '') and 'boom' in (r.detail or ''),
          (r.detail or '')[:80])

    shutil.rmtree(work, ignore_errors=True)
    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
