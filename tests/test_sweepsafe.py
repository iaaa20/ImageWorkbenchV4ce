"""中途清扫临时文件，绝不能把已完成的成果一起扫掉。

用户实测：2831 张的批量，**只因为有 1 张超过 120 秒超时**，最后报出
「成功 271，失败 2560」。原因是超时分支调了 `sweep_temps(self._output_dirs)`
无差别清扫 —— 而安全写入模式下，每一张处理好的产物都以临时文件躺在同一个
目录里等最后统一提交，跟"被强杀的工人留下的半截文件"长得一模一样，
于是整批成果被删光，`commit_staged` 挨个 `os.replace` 全部失败。

**原地修改模式下后果更重**：`commit_staged` 是先删源文件再改名，
临时文件没了就等于原图和产物一起没了。所以这里还盯住第二道保险：
提交前先确认每个临时文件都在，缺了就整批不提交，绝不动源文件。

用法:  python tests\\test_sweepsafe.py
不建窗口。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from imgwb.config import TaskResult
from imgwb.tasks import commit_staged, sweep_temps
from imgwb.utils import make_temp_path

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


tmp = tempfile.mkdtemp()


def staged_batch(n, out_dir, sources=None):
    """造 n 个"已完成、等提交"的结果：每个都有一个真实存在的临时文件。"""
    rows = []
    for i in range(n):
        dst = os.path.join(out_dir, f'IMG_{i:03d}.jpg')
        t = make_temp_path(dst)
        with open(t, 'wb') as fh:
            fh.write(b'done' + str(i).encode())
        src = (sources[i] if sources else
               os.path.join(out_dir, f'src{i}.jpg'))
        rows.append(TaskResult(True, src, dst, tmp_path=t))
    return rows


print('=== 1. 清扫要放过「已完成待提交」的临时文件 ===')
out = os.path.join(tmp, 'out1')
os.makedirs(out)
staged = staged_batch(5, out)
# 再放一个"被强杀的工人留下的半截文件"，它才是清扫的目标
orphan = make_temp_path(os.path.join(out, 'orphan.jpg'))
with open(orphan, 'wb') as fh:
    fh.write(b'half written')

removed = sweep_temps([out], exclude=[r.tmp_path for r in staged])
check('只扫掉孤儿文件', removed == 1, f'扫掉 {removed} 个')
check('已完成的临时文件一个不少',
      all(os.path.exists(r.tmp_path) for r in staged))
check('孤儿文件确实没了', not os.path.exists(orphan))

errors = commit_staged(staged, is_export=True)
check('提交成功、没有失败', errors == [], str(errors[:2]))
check('产物都落盘了',
      all(os.path.exists(r.dst_path) for r in staged))

print('\n=== 2. 不传 exclude 就是无差别清扫（旧行为，会闯祸）===')
out2 = os.path.join(tmp, 'out2')
os.makedirs(out2)
staged2 = staged_batch(4, out2)
removed = sweep_temps([out2])
check('确实会把已完成的一起删掉', removed == 4, f'{removed} 个')
errors = commit_staged(staged2, is_export=True)
check('于是提交全军覆没（用户遇到的就是这个）', len(errors) == 4,
      f'{len(errors)} 个失败')

print('\n=== 3. 第二道保险：临时文件缺了就别动源文件 ===')
# 原地修改：源文件和产物在同一个目录，commit 是"先删源、再改名"
out3 = os.path.join(tmp, 'out3')
os.makedirs(out3)
sources = []
for i in range(4):
    p = os.path.join(out3, f'src{i}.jpg')
    with open(p, 'wb') as fh:
        fh.write(b'ORIGINAL-' + str(i).encode())
    sources.append(p)
staged3 = staged_batch(4, out3, sources=sources)
# 模拟"临时文件被中途清扫误删"
os.remove(staged3[1].tmp_path)
os.remove(staged3[2].tmp_path)

errors = commit_staged(staged3, is_export=False)
check('缺失的那两个记成失败', len(errors) == 2, f'{len(errors)} 个')
# 临时文件还在的那两个，本来就该正常提交（源被产物取代是预期行为）；
# 要保住的是**缺失的那两个**的源文件 —— 它们的产物已经没了，
# 源再删掉就是彻底丢失。
check('缺失的那两个，源文件必须还在',
      os.path.exists(sources[1]) and os.path.exists(sources[2]),
      f'src1={os.path.exists(sources[1])} src2={os.path.exists(sources[2])}')
check('正常的那两个照常提交',
      not os.path.exists(sources[0]) and not os.path.exists(sources[3])
      and os.path.exists(staged3[0].dst_path))

# 全部临时文件都没了（用户那次的形态）-> 一个源文件都不许动
out4 = os.path.join(tmp, 'out4')
os.makedirs(out4)
src4 = []
for i in range(5):
    q = os.path.join(out4, f's{i}.jpg')
    with open(q, 'wb') as fh:
        fh.write(b'ORIGINAL')
    src4.append(q)
staged4 = staged_batch(5, out4, sources=src4)
sweep_temps([out4])                      # 模拟无差别清扫
errors4 = commit_staged(staged4, is_export=False)
check('临时文件全没了 -> 源文件一个不动',
      len(errors4) == 5 and all(os.path.exists(q) for q in src4),
      f'{len(errors4)} 个失败，源文件还剩 {sum(os.path.exists(q) for q in src4)}/5')
check('失败原因说清楚了',
      all('临时文件已不存在' in e[1] for e in errors),
      errors[0][1] if errors else '')

shutil.rmtree(tmp, ignore_errors=True)
print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
sys.exit(1 if fails else 0)
