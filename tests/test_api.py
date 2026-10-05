"""无界面接口回归：api.plan / api.run / 命令行 / 参数校验。

用法:  python tests\test_api.py
不需要显示器。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from imgwb.api import OptionError, capabilities, plan, run

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def folder(n=4, with_anim=True):
    d = tempfile.mkdtemp()
    for i in range(1, n + 1):
        Image.new('RGB', (60, 40), (i * 50 % 256, 90, 150)).save(
            os.path.join(d, f'DSC_{i:04d}.jpg'))
    if with_anim:
        f = [Image.new('RGB', (40, 30), (i * 80 % 256, 60, 180)) for i in range(4)]
        f[0].save(os.path.join(d, 'anim.webp'), format='WEBP', save_all=True,
                  append_images=f[1:], duration=70, loop=0)
    return d


print('== capabilities ==')
caps = capabilities()
check('列出扩展名', '.jpg' in caps['extensions'])
check('列出旋转选项', set(caps['rotate']) == {'none', 'cw90', 'ccw90', '180'})
check('ICO 尺寸不含 512', not any('512' in s for s in caps['ico_size']),
      str(caps['ico_size']))
check('可 JSON 序列化', isinstance(json.dumps(caps, ensure_ascii=False), str))

print('\n== plan 只算不写 ==')
d = folder()
before = sorted(os.listdir(d))
p = plan(source=d, output='inplace', name='number', rotate='cw90')
after = sorted(os.listdir(d))
check('目录未被改动', before == after)
check('列出全部任务', len(p['tasks']) == 5, f'{len(p["tasks"])} 个')
check('标出会删除的原文件', len(p['will_delete_originals']) == 5,
      f'{len(p["will_delete_originals"])} 个')
check('结果可 JSON 序列化', isinstance(json.dumps(p, ensure_ascii=False), str))
shutil.rmtree(d, ignore_errors=True)

print('\n== run 原地编号重命名 ==')
d = folder()
r = run(source=d, output='inplace', name='number', rotate='cw90')
names = sorted(os.listdir(d))
check('全部成功', r['ok'] and r['processed'] == 5, f'{r["processed"]}/5 {r["failed"]}')
width = len(str(len(names))) + 1          # 智能位数：够用再多留一位
first = str(1).zfill(width)
check(f'编号从 {first} 开始', names[0].startswith(first), str(names))
with Image.open(os.path.join(d, first + '.webp')) as im:
    check('动图帧数保留', im.n_frames == 4, f'{im.n_frames} 帧')
    check('动图已旋转', im.size == (30, 40), str(im.size))

print('\n== 重复执行编号稳定（不会跑到 1001）==')
r2 = run(source=d, output='inplace', name='number', rotate='cw90')
check('第二次仍从 001 开始', sorted(os.listdir(d)) == names,
      str(sorted(os.listdir(d))))
shutil.rmtree(d, ignore_errors=True)

print('\n== run 导出到新位置 ==')
d = folder(3, with_anim=False)
out = tempfile.mkdtemp()
r = run(source=d, output='new', dest=out, name='prefix', prefix='PIC',
        rotate='180', quality='85')
produced = []
for root, _, fs in os.walk(out):
    produced += [os.path.join(root, f) for f in fs]
check('导出成功', r['ok'] and len(produced) == 3, f'{len(produced)} 个')
check('用了前缀', all(os.path.basename(p).startswith('PIC_') for p in produced),
      str([os.path.basename(p) for p in produced]))
check('源目录未动', len(os.listdir(d)) == 3)
shutil.rmtree(d, ignore_errors=True)
shutil.rmtree(out, ignore_errors=True)

print('\n== 参数校验 ==')
for kw, why in (
        (dict(source='.', rotate='left'), '非法 rotate'),
        (dict(source='.', name='auto'), '非法 name'),
        (dict(source='.', start=-1), 'start 为负'),
        (dict(source='.', digits=9), 'digits 越界'),
        (dict(source='.', output='new'), 'output=new 却没给 dest'),
        (dict(source=os.path.join(HERE, '不存在的目录')), 'source 不存在'),
):
    try:
        plan(**kw)
        check(why, False, '没有报错')
    except OptionError as exc:
        check(why, True, str(exc)[:46])
    except Exception as exc:
        check(why, False, f'抛的不是 OptionError: {type(exc).__name__}')

print('\n== 命令行 ==')
d = folder(3, with_anim=False)
env = dict(os.environ, PYTHONIOENCODING='utf-8')


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'imgwb', *args],
                          cwd=ROOT, capture_output=True, text=True,
                          encoding='utf-8', env=env)

r = cli('capabilities')
check('capabilities 输出合法 JSON', r.returncode == 0 and json.loads(r.stdout)['rotate'])

r = cli('plan', '--source', d, '--name', 'number', '--json')
check('plan --json 输出合法 JSON', r.returncode == 0 and json.loads(r.stdout)['dry_run'])
check('plan 没有改动目录', len(os.listdir(d)) == 3)

r = cli('run', '--source', d, '--name', 'number', '--rotate', 'cw90', '--json')
data = json.loads(r.stdout) if r.stdout.strip() else {}
check('run --json 成功', r.returncode == 0 and data.get('processed') == 3,
      str(data.get('failed'))[:60])
check('stdout 只有 JSON（不掺进度）', r.stdout.lstrip().startswith('{'))

r = cli('run', '--source', d, '--start', '-3', '--json')
data = json.loads(r.stdout) if r.stdout.strip() else {}
check('参数错误单独退出码 2', r.returncode == 2 and data.get('error_type') == 'option',
      f'退出码 {r.returncode}')
shutil.rmtree(d, ignore_errors=True)

print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
