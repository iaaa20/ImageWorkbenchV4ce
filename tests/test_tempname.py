"""临时文件的名字必须足够独特，否则清扫会误删用户自己的文件。

用户问：「命名里面如果有很多个点的话你怎么识别？」—— 查下来多点名字本身没问题
（`my.photo.v2.jpg`、`2026.09.11.假期.jpg`、`UPPER.JPG` 都处理得对），
**但清扫残留临时文件的规则出过事**：

原来临时文件叫「原名.~ + 8 位十六进制 + 扩展名」，清扫就按这个规律删。
于是用户自己名为 `photo.~1a2b3c4d.jpg`、`backup.~deadbeef.png` 的文件
会被当成垃圾直接删掉 —— 实测一扫就没，三个全删。

清扫是按文件名认的（被强杀的工人留下的半截文件，父进程根本不知道它叫什么），
所以判据只能靠名字，那名字就必须独特到正常文件撞不上 —— 现在带 `imgwb` 标记。

触发清扫的时机：取消、单张超时重建进程池、整轮崩溃。也就是说这不是理论风险，
用户点一次取消就可能中招。

用法:  python tests\\test_tempname.py
不建窗口，一秒。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import hashlib
import json
import subprocess
import time

from imgwb.tasks import TEMP_PATTERN, sweep_temps, temp_owner
from imgwb.utils import (INSTANCE_TAG, SCOPE_TAG, UNKNOWN_SCOPE,
                         instance_alive, make_temp_path)

fails = []


def _identity_in_worker():
    """在 multiprocessing 的工作子进程里跑：报告它那边的实例标记和范围标记。"""
    from imgwb import utils
    return utils.INSTANCE_TAG, utils.SCOPE_TAG


# ---------------------------------------------------------------------------
# 「另一个实例」是真的另一个进程，临时文件是它用真实的处理流程写出来的。
#
# **这一组原来不是这么测的，而且因此漏掉了一个真 bug。** 上一版手工建一个
# 假的临时文件、手工把修改时间改成两天前，然后断言"它会被清掉" —— 这只是把
# 实现里的假设（"修改时间旧 = 放了很久的残骸"）原样抄进了测试。而真实流程
# 写出来的临时文件，修改时间是**从源照片继承的**：处理一张两天前的照片，
# 临时文件一生成就"看着像放了两天"。于是别的窗口一清扫，这边正等着提交的
# 产物就没了。
#
# 所以这里的规矩：**时间只设在源照片上**（那是用户真实会有的输入），
# 临时文件的任何属性都不许手工改；"另一个实例"必须是活的进程，
# 要它死就真的杀掉它。
# ---------------------------------------------------------------------------
def _owner_main(source, dest, mode):
    """子进程入口：当「另一个实例」。跑到所有产物都写成临时文件、还没提交的
    那一刻停下，把现场报给父进程，等父进程说 commit 再继续。"""
    from imgwb import api, utils

    if mode.endswith('-late'):
        # 先报上身份，等父进程说 make 才开始处理 —— 父进程要把"产物落盘"
        # 安排在一个它指定的时刻（见 clean_while_owner_is_producing）。
        mode = mode[:-len('-late')]
        print(json.dumps({'tag': utils.INSTANCE_TAG, 'scope': utils.SCOPE_TAG}),
              flush=True)
        if sys.stdin.readline().strip() != 'make':
            raise RuntimeError('父进程没有放行')

    def progress(done, total):
        if done < total:
            return
        temps = [n for n in os.listdir(dest) if temp_owner(n) is not None]
        print(json.dumps({'tag': utils.INSTANCE_TAG, 'scope': utils.SCOPE_TAG,
                          'temps': temps}), flush=True)
        if sys.stdin.readline().strip() != 'commit':
            raise RuntimeError('父进程没有放行')

    opts = {'rotate': 'cw90'} if mode == 'rotate' else {}
    r = api.run(source, output='new', dest=dest, keep_structure=False,
                name='keep', safe_write=True, workers=1, progress=progress,
                **opts)
    print(json.dumps({'ok': r['ok'], 'failed': r['failed'],
                      'outputs': r.get('outputs', [])}, ensure_ascii=False),
          flush=True)


def _start_owner(work, label, mode, age_seconds):
    """起一个真的「另一个实例」，返回 (进程, 现场, 源目录, 输出目录, 源文件时间)。"""
    from PIL import Image

    d = os.path.join(work, label)
    src, out = os.path.join(d, 'source'), os.path.join(d, 'out')
    os.makedirs(src)
    stamp = int(time.time() - age_seconds)
    for name, color in (('a.png', 'red'), ('b.png', 'blue')):
        p = os.path.join(src, name)
        Image.new('RGB', (32, 24), color).save(p)
        os.utime(p, (stamp, stamp))         # 只动**源照片**的时间
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), 'owner', src, out, mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8',
        env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    line = child.stdout.readline()
    if not line:
        raise RuntimeError('另一个实例没起来: ' + child.stderr.read()[-400:])
    return child, json.loads(line), src, out, stamp


def foreign_live_owner(work):
    """主人活着：不管源照片多旧、走的是复制还是重编码，一个都不许删，
    而且放行之后它能把结果正常提交出来。"""
    for mode, what in (('copy', '原样复制'), ('rotate', '旋转重编码')):
        for age, how_old in ((2 * 86400, '两天前的照片'), (400 * 86400, '一年多前的照片'),
                             (0, '刚拍的照片')):
            tag = f'{what} · {how_old}'
            child, state, src, out, stamp = _start_owner(
                work, f'live_{mode}_{age}', mode, age)
            try:
                staged = list(state['temps'])
                check(f'{tag}：夹具成立（两个临时文件，主人是另一个实例且还活着）',
                      len(staged) == 2 and child.poll() is None
                      and state['tag'] != INSTANCE_TAG
                      and state['scope'] == SCOPE_TAG, str(state))
                if age:
                    # 这正是那个 bug 的前提：临时文件的修改时间是从源继承的
                    ages = [time.time() - os.path.getmtime(os.path.join(out, n))
                            for n in staged]
                    check(f'{tag}：临时文件一生成，修改时间就已经是"很久以前"',
                          all(a > 86400 for a in ages),
                          f'{[round(a / 3600) for a in ages]} 小时')
                removed = sweep_temps([out])
                left = [n for n in staged if os.path.exists(os.path.join(out, n))]
                check(f'{tag}：**清扫一个都没删**',
                      removed == 0 and left == staged, f'删了 {removed} 个')
                child.stdin.write('commit\n')
                child.stdin.flush()
                stdout, stderr = child.communicate(timeout=30)
                result = json.loads(stdout.strip().splitlines()[-1])
                check(f'{tag}：那个实例随后正常提交', result['ok'] is True
                      and sorted(os.listdir(out)) == ['a.png', 'b.png'],
                      f'{result} {os.listdir(out)} {stderr[-200:]}')
                # 保留照片时间是产品功能，不能为了修清扫把它弄丢
                kept = [int(os.path.getmtime(os.path.join(out, n)))
                        for n in sorted(os.listdir(out))]
                check(f'{tag}：产物仍然保留着源照片的修改时间',
                      all(abs(k - stamp) <= 2 for k in kept),
                      f'源 {stamp} / 产物 {kept}')
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()


def foreign_dead_owner(work):
    """主人被杀掉了：它留下的临时文件才是残骸，这时不管文件时间新旧都清掉。

    「刚拍的照片」那一组是关键对照 —— 临时文件的修改时间是几秒前，
    按"看时间"的老办法它会被留下；现在凭的是"主人不在了"，所以照清。
    """
    for age, how_old in ((0, '刚拍的照片'), (2 * 86400, '两天前的照片')):
        child, state, _src, out, _stamp = _start_owner(
            work, f'dead_{age}', 'copy', age)
        staged = list(state['temps'])
        alive_before = instance_alive(state['scope'], state['tag'])
        kept = sweep_temps([out])
        child.kill()
        child.wait()
        gone_after = False
        for _ in range(50):                 # 句柄回收是系统做的，给它一点时间
            if not instance_alive(state['scope'], state['tag']):
                gone_after = True
                break
            time.sleep(0.1)
        check(f'{how_old}：主人活着时查得出"在"，杀掉之后查得出"不在"',
              alive_before is True and gone_after, f'{alive_before} -> {gone_after}')
        check(f'{how_old}：主人活着那次清扫没删', kept == 0, f'删了 {kept} 个')
        removed = sweep_temps([out])
        left = [n for n in staged if os.path.exists(os.path.join(out, n))]
        check(f'{how_old}：**主人不在之后，残骸清掉了**',
              len(staged) == 2 and removed == 2 and not left,
              f'删了 {removed} 个，还剩 {left}')


def leftovers_and_explicit_clean(work):
    """残留要**报得出来**，而且用户明确要求时清得掉 —— 但活着的和备份永远不动。

    自动清扫对"判断不了主人"的临时文件只会绕着走，所以得有两样东西：
    `find_leftovers` 告诉用户它们在哪，`clean_leftovers` 让用户拍板之后清掉。
    "另一个活着的实例"是真的进程；"判断不了主人的"只能手工造名字
    （造不出一台别的电脑）；备份文件是程序自己的命名规律。
    """
    from imgwb.tasks import (BACKUP_MARK, clean_leftovers, find_leftovers,
                             scan_files)

    child, state, src, out, _stamp = _start_owner(work, 'leftovers', 'copy', 0)
    try:
        live = sorted(os.path.join(out, n) for n in state['temps'])
        other_scope = 'abcd' if SCOPE_TAG != 'abcd' else 'dcba'
        unknown = os.path.join(out, f'u.~imgwb123456{other_scope}11111111.jpg')
        legacy = os.path.join(out, 'v.~imgwb22222222.jpg')
        backup = os.path.join(out, f'orig.jpg{BACKUP_MARK}deadbeef')
        mine = make_temp_path(os.path.join(out, 'mine.jpg'))
        user = os.path.join(out, 'holiday.jpg')
        for p, body in ((unknown, b'u'), (legacy, b'v'), (backup, b'ORIGINAL'),
                        (mine, b'm'), (user, b'user photo')):
            with open(p, 'wb') as fh:
                fh.write(body)

        found = find_leftovers([out])
        check('报得出：另一个活着的实例的临时文件', found['live'] == live,
              f'{len(found["live"])} 个')
        check('报得出：判断不了主人的临时文件',
              found['unknown'] == sorted([unknown, legacy]), str(found['unknown']))
        check('报得出：备份文件（那是原文件）', found['backups'] == [backup])

        # 扫描来源时，这些都不是输入
        class V:
            def __init__(self, v):
                self.v = v

            def get(self):
                return self.v

        cfg = type('C', (), {})()
        cfg.dropped_paths, cfg.dropped_roots = [], []
        cfg.scan_subdirs, cfg.copy_others = V(True), V(True)
        notes = []
        scanned = [os.path.basename(p) for p in scan_files(out, cfg, notes.append)]
        check('**扫描来源时不把临时文件和备份当成图片**（哪怕开着"带上不支持的文件"）',
              scanned == ['holiday.jpg'], str(scanned))
        check('而且说了跳过几个', any('6' in n for n in notes), str(notes))
        cfg.dropped_paths = [user, unknown, backup, live[0]]
        check('文件列表入口同样跳过',
              [os.path.basename(p) for p in scan_files(out, cfg)] == ['holiday.jpg'])

        plan = clean_leftovers(out, dry_run=True)
        check('试运行：只报告，不动手',
              plan['removed'] == [mine] and os.path.exists(mine), str(plan['removed']))
        done = clean_leftovers(out)
        check('默认清理：只删自己的，别的都留着',
              done['removed'] == [mine] and not os.path.exists(mine)
              and all(os.path.exists(p) for p in live + [unknown, legacy, backup, user]),
              str(done['removed']))
        check('默认清理：留下的分门别类说清楚了',
              done['kept_live'] == live
              and done['kept_unknown'] == sorted([unknown, legacy])
              and done['backups'] == [backup])
        done = clean_leftovers(out, include_unknown=True)
        check('用户拍板之后：判断不了主人的也清掉',
              sorted(done['removed']) == sorted([unknown, legacy])
              and not os.path.exists(unknown) and not os.path.exists(legacy))
        check('**活着的实例的临时文件、备份、用户文件，怎么清都不动**',
              all(os.path.exists(p) for p in live + [backup, user])
              and open(backup, 'rb').read() == b'ORIGINAL')

        # 那个实例照常提交 —— 上面这一通折腾没碰到它
        child.stdin.write('commit\n')
        child.stdin.flush()
        stdout, _stderr = child.communicate(timeout=30)
        result = json.loads(stdout.strip().splitlines()[-1])
        check('那个实例随后正常提交', result['ok'] is True
              and {'a.png', 'b.png'} <= set(os.listdir(out)), str(result))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def clean_while_owner_is_producing(work):
    """用户点清理的同时，另一个窗口正在往这个文件夹里出产物 —— 清理开始
    **之后**才落盘的产物，主人活着就不许删。

    原来 `clean_leftovers` 先拿一份"谁活着"的路径名单，再重新列目录、
    不在名单上的就删。名单拿完之后才写出来的产物不在上面，于是被删，
    那个窗口随后提交时报"临时文件已不存在"（两个真实进程 10/10）。
    上面 `leftovers_and_explicit_clean` 里活着的那些文件在清理开始前就已经
    在了，正好测不到这一段。

    另一个实例是真的进程，产物是它用真实的 `api.run` 旋转出来的；清理是真实的
    `api.clean`。**唯一动的是时序**：让清理在它第一次查看（`find_leftovers`）
    返回之后停一下，这时才放行那个实例出产物，然后清理继续。查看、列目录、
    查主人死活、删除，全是原样。
    """
    from PIL import Image

    from imgwb import api, tasks

    for order, when in (('between', '清理查看过之后才落盘'),
                        ('before', '对照 · 清理开始前就在')):
        d = os.path.join(work, f'cleanrace_{order}')
        src, out = os.path.join(d, 'source'), os.path.join(d, 'out')
        os.makedirs(src)
        os.makedirs(out)
        stamp = int(time.time() - 2 * 86400)
        for name, color in (('a.png', 'red'), ('b.png', 'blue')):
            p = os.path.join(src, name)
            Image.new('RGB', (32, 24), color).save(p)
            os.utime(p, (stamp, stamp))         # 只动**源照片**的时间
        originals = {n: hashlib.sha256(
            open(os.path.join(src, n), 'rb').read()).hexdigest()
            for n in os.listdir(src)}
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), 'owner', src, out,
             'rotate-late'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8',
            env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        real_find = tasks.find_leftovers
        try:
            line = child.stdout.readline()
            if not line:
                raise RuntimeError('另一个实例没起来: ' + child.stderr.read()[-400:])
            ident = json.loads(line)
            state = {}

            def produce():
                child.stdin.write('make\n')
                child.stdin.flush()
                state.update(json.loads(child.stdout.readline()))

            def find_then_pause(dirs):
                found = real_find(dirs)
                if order == 'between':
                    produce()
                return found

            if order == 'before':
                produce()
            tasks.find_leftovers = find_then_pause
            done = api.clean(out)
            tasks.find_leftovers = real_find

            staged = sorted(os.path.join(out, n) for n in state.get('temps', []))
            check(f'{when}：夹具成立（两个产物，主人是另一个实例且还活着）',
                  len(staged) == 2 and child.poll() is None
                  and ident['tag'] != INSTANCE_TAG
                  and instance_alive(ident['scope'], ident['tag']) is True,
                  f'{ident} {state}')
            check(f'{when}：**清理一个都没删**',
                  done['removed'] == [] and all(os.path.exists(p) for p in staged),
                  str(done['removed']))
            check(f'{when}：而且报出来了 —— 留着是因为主人还在',
                  done['kept_live'] == staged, str(done['kept_live']))
            child.stdin.write('commit\n')
            child.stdin.flush()
            stdout, stderr = child.communicate(timeout=30)
            result = json.loads(stdout.strip().splitlines()[-1])
            check(f'{when}：那个实例随后正常提交', result['ok'] is True
                  and sorted(os.listdir(out)) == ['a.png', 'b.png'],
                  f'{result} {os.listdir(out)} {stderr[-200:]}')
            sizes = []
            for n in sorted(os.listdir(out)):
                if temp_owner(n) is None:
                    with Image.open(os.path.join(out, n)) as im:
                        im.load()
                        sizes.append(im.size)
            check(f'{when}：提交出来的是真转过的图', sizes == [(24, 32)] * 2,
                  str(sizes))
            kept = [int(os.path.getmtime(os.path.join(out, n)))
                    for n in sorted(os.listdir(out))]
            check(f'{when}：产物仍然保留着源照片的修改时间',
                  all(abs(k - stamp) <= 2 for k in kept), f'源 {stamp} / 产物 {kept}')
            check(f'{when}：源照片原样还在',
                  {n: hashlib.sha256(open(os.path.join(src, n), 'rb').read())
                   .hexdigest() for n in os.listdir(src)} == originals)
        finally:
            tasks.find_leftovers = real_find
            if child.poll() is None:
                child.kill()
                child.wait()


def unknown_owner(work):
    """判断不了主人死活的，一律不碰。

    这一组的文件是手工建的 —— 它验的是"**名字**说明不了主人是谁 / 主人不在
    本机"时的规则，没有哪个真实进程能替我们造出"另一台机器"。时间故意设成
    很久以前：就算看着再像垃圾，也不许删。
    """
    d = os.path.join(work, 'unknown')
    os.makedirs(d)
    other_scope = 'abcd' if SCOPE_TAG != 'abcd' else 'dcba'
    long_ago = time.time() - 400 * 86400
    cases = {
        f'a.~imgwb123456{other_scope}11111111.jpg': '别的机器或别的登录会话上的实例',
        f'b.~imgwb123456{UNKNOWN_SCOPE}22222222.jpg': '拿不到凭据的实例',
        'c.~imgwb33333333.jpg': '老版本的名字（8 位）',
        'd.~imgwb44444444444444.jpg': '老版本的名字（14 位）',
    }
    for name in cases:
        p = os.path.join(d, name)
        with open(p, 'wb') as fh:
            fh.write(b'x')
        os.utime(p, (long_ago, long_ago))
        check(f'认得出是临时文件：{cases[name]}', temp_owner(name) is not None, name)
    removed = sweep_temps([d])
    left = set(os.listdir(d))
    for name, what in cases.items():
        check(f'**不删**：{what}（哪怕放了一年多）', name in left, name)
    check('这一轮什么都没删', removed == 0, f'删了 {removed} 个')
    check('别的范围的实例一律当作"还在"',
          instance_alive(other_scope, '123456') is True
          and instance_alive(UNKNOWN_SCOPE, '123456') is True)
    # 对照：同一个范围、根本不存在的实例标记 —— 这才查得出"不在"
    check('对照：本机本会话里不存在的实例，查得出"不在"',
          instance_alive(SCOPE_TAG, 'ffffff' if INSTANCE_TAG != 'ffffff'
                         else 'eeeeee') is False)


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}' + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


# 用户自己可能起的名字 —— 一个都不许删
USER_NAMES = [
    'photo.~1a2b3c4d.jpg',        # 和旧版临时文件格式一模一样
    'photo.~1a2b3c4d',            # 没有扩展名的版本
    'backup.~deadbeef.png',
    'photo.~zz.jpg',
    'photo.~1a2b3c4d5e.jpg',
    'my.photo.v2.jpg',            # 普通的多点名字
    '2026.09.11.假期.jpg',
    'normal.jpg',
]


def main():
    work = tempfile.mkdtemp(dir=os.environ.get('IMGWB_TEST_TMP') or None)
    try:
        print('一、临时文件名自己要能被认出来')
        tmp = make_temp_path(os.path.join(work, 'anything.jpg'))
        check('临时文件名带 imgwb 标记', 'imgwb' in os.path.basename(tmp),
              os.path.basename(tmp))
        check('临时文件名能被清扫规则认出来',
              bool(TEMP_PATTERN.search(os.path.basename(tmp))),
              os.path.basename(tmp))
        check('扩展名保留（Pillow 靠它判断存成什么格式）',
              tmp.endswith('.jpg'), os.path.basename(tmp))

        print('\n二、用户自己的文件一个都不许删')
        for n in USER_NAMES:
            with open(os.path.join(work, n), 'wb') as fh:
                fh.write(b'user data')
        # 再放一个真正的临时文件进去，确认该删的还是会删
        real = make_temp_path(os.path.join(work, 'real.jpg'))
        with open(real, 'wb') as fh:
            fh.write(b'half written by a killed worker')

        removed = sweep_temps([work])
        left = set(os.listdir(work))
        gone = [n for n in USER_NAMES if n not in left]
        check('**用户文件一个没少**', not gone, f'被删掉的: {gone}')
        check('真正的临时文件被清掉了',
              os.path.basename(real) not in left and removed == 1,
              f'删了 {removed} 个')

        print('\n三、正在等待提交的临时文件不能被扫掉')
        # 安全写入模式下，处理好的产物就以临时文件躺在同一个目录里等统一提交，
        # 它们和「被强杀的工人留下的半截文件」长得一模一样，只能靠 exclude 区分。
        keep = make_temp_path(os.path.join(work, 'staged.jpg'))
        other = make_temp_path(os.path.join(work, 'garbage.jpg'))
        for p in (keep, other):
            with open(p, 'wb') as fh:
                fh.write(b'x')
        removed = sweep_temps([work], exclude=[keep])
        check('exclude 里的临时文件留下了', os.path.exists(keep))
        check('没在 exclude 里的被清掉了',
              not os.path.exists(other) and removed == 1, f'删了 {removed} 个')
        os.remove(keep)

        print('\n四、别的实例**还活着**时，它的临时文件一个都不能扫')
        foreign_live_owner(work)

        print('\n五、主人确定不在了，才算残骸')
        foreign_dead_owner(work)

        print('\n六、看不出主人、或者主人在别的机器/会话上的，一律不碰')
        unknown_owner(work)

        print('\n六点五、残留报得出来；用户拍板才清，活着的和备份永远不动')
        leftovers_and_explicit_clean(work)

        print('\n六点六、清理进行到一半才落盘的产物，主人活着就不删')
        clean_while_owner_is_producing(work)

        print('\n七、工作子进程和主进程是同一个身份，另起的实例不是')
        import multiprocessing
        with multiprocessing.get_context('spawn').Pool(1) as pool:
            worker = pool.apply(_identity_in_worker)
        check('工作子进程沿用主进程的标记（否则主进程认不出工人留下的残骸）',
              worker == (INSTANCE_TAG, SCOPE_TAG),
              f'{(INSTANCE_TAG, SCOPE_TAG)} / {worker}')
        # 环境变量是会被继承的 —— 从一个跑着本程序的环境里再起一个独立的
        # 实例，它不能因为继承到那个变量就沿用同一个标记。
        r = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, sys.argv[1]); '
             'from imgwb.utils import INSTANCE_TAG, SCOPE_TAG; '
             'print(INSTANCE_TAG, SCOPE_TAG)', os.path.dirname(HERE)],
            capture_output=True, text=True)
        fresh = r.stdout.split()
        check('另起的独立实例有自己的实例标记',
              len(fresh) == 2 and len(fresh[0]) == 6 and fresh[0] != INSTANCE_TAG,
              f'{INSTANCE_TAG} / {fresh}')
        check('同一台机器同一个会话，范围标记相同',
              len(fresh) == 2 and fresh[1] == SCOPE_TAG, f'{SCOPE_TAG} / {fresh}')
        check('那个实例退出之后，查得出它不在了',
              len(fresh) == 2 and instance_alive(fresh[1], fresh[0]) is False)
        check('自己当然是活着的', instance_alive(SCOPE_TAG, INSTANCE_TAG) is True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print('\n' + ('全部通过' if not fails else f'失败: {fails}'))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == 'owner':
        _owner_main(*sys.argv[2:5])
    else:
        main()
