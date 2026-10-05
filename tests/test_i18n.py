"""中英切换：界面换语言，**内部值一个字都不许变**。

这是 i18n 这套做法唯一真正的风险所在。界面上的选项文字同时是程序的判断
依据（`rotate_mode == '不旋转'`、`'95%' in quality_opt`、
`dict(TIMEOUT_CHOICES).get(标签)`），所以只要「读回来的值」被英文污染一次，
处理流程就会静默走到默认分支 —— 不报错，但结果全错。这个测试盯的就是它。

用法:  python tests\test_i18n.py
会真的建出窗口，需要有桌面会话。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from imgwb import i18n
from imgwb.app import ImageProcessorApp

fails = []


def check(name, ok, detail=''):
    print(f'  {"OK  " if ok else "FAIL"} {name}'
          + (f'   {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def has_cjk(s):
    return any('一' <= c <= '鿿' for c in s)


# ---------------------------------------------------------------- 纯函数部分
print('\n[1] 不建窗口也该成立的性质')

check('库的默认语言是中文', i18n.get_lang() == 'zh',
      '否则测试和 API 的行为会随机器区域设置变')
check('中文模式下 tr() 原样返回', i18n.tr('处理选项') == '处理选项')

i18n.set_lang('en')
# 所有下拉框选项：中文 → 英文 → 中文，必须回到原值。
# 这一条挂了就说明某两个中文选项译成了同一句英文，读回时分不清谁是谁。
bad = [zh for zh in i18n.OPTIONS if i18n.canon(i18n.display(zh)) != zh]
check('下拉框选项中→英→中往返无损', not bad, f'出错的: {bad[:4]}')

dup = {}
for zh, en in i18n.OPTIONS.items():
    dup.setdefault(en, []).append(zh)
collide = {en: v for en, v in dup.items() if len(v) > 1}
check('没有两个选项译成同一句英文', not collide, str(list(collide.items())[:2]))

check('英文模式下 tr() 真的翻了', i18n.tr('处理选项') == 'Options',
      i18n.tr('处理选项'))
check('Labelframe 两侧留白的标题也翻（空格要留着）',
      i18n.tr(' 处理选项 ') == ' Options ', repr(i18n.tr(' 处理选项 ')))
check('没覆盖到的句子原样留中文、不变空',
      i18n.tr('这句话绝不会在翻译表里出现') == '这句话绝不会在翻译表里出现')

# 一键按钮的文字来自 presets.py 的字典，AST 抽取器抓不到下标表达式 ——
# 前几批词条全漏了它，而这几个按钮就是简易模式的主界面。所以单独盯一条。
from imgwb.presets import PRESETS                                 # noqa: E402

miss = [f'{p["key"]}.{k}' for p in PRESETS for k in ('label', 'desc', 'warn')
        if p.get(k) and has_cjk(i18n.tr(p[k]))]
check('一键按钮的文字和说明都翻了', not miss, str(miss))

# 提示语是 `标题 —— 说明` + `⚠️ 警告` + 固定结尾拼出来的，整段匹配不上任何
# 模板，靠 tr() 的「按空行/换行/分号逐级拆开」才翻得动。这条盯的是那条回退链。
tips = []
for p in PRESETS:
    t = (f'{p["label"]} —— {p["desc"]}\n\n'
         + (f'⚠️ {p["warn"]}\n\n' if p.get('warn') else '')
         + '输出到源文件夹旁边的「xxx_已处理」，原文件不动。')
    if has_cjk(i18n.tr(t)):
        tips.append(p['key'])
check('一键按钮的悬停提示（三段拼的）也翻了', not tips, str(tips))

# 反过来：**整条词条不许被拆坏**。长提示语本身就带换行和分号，
# 逐级拆开那套逻辑如果顺序错了，它们会再也匹配不上。
long_one = [k for k in i18n.UI if '\n' in k and '；' in k]
check('本身带换行和分号的整条词条还能命中',
      long_one and all(not has_cjk(i18n.tr(k)) for k in long_one),
      f'{len(long_one)} 条')

# 占位符不许跨过分隔符 —— 跨过去会翻出错位的引号，比漏翻更糟
check('模板不会跨过竖线乱配（日志的字段分隔符）',
      i18n.tr('[操作] 开始执行 | 120 张').count('×') == 0,
      i18n.tr('[操作] 开始执行 | 120 张'))
check('拼起来的失败汇总逐截翻对',
      i18n.tr('1 个「文件是空的」；1 个「这不是图片」')
      == '1 × "the file is empty"; 1 × "not an image"',
      i18n.tr('1 个「文件是空的」；1 个「这不是图片」'))

i18n.set_lang('zh')

# ---------------------------------------------------------------- 真界面
print('\n[2] 建出窗口，中文模式下的基线')

# 测试会调 switch_language，它会把选择存到 LOCALAPPDATA。
# 先备份用户真正的偏好，跑完原样放回去 —— 测试不该改用户的设置。
_pref = i18n._pref_path()
_saved = None
if os.path.isfile(_pref):
    with open(_pref, encoding='utf-8') as fh:
        _saved = fh.read()

app = ImageProcessorApp()
app.set_mode(advanced=True)              # 下拉框都在高级面板里
app.root.update_idletasks()

# 会影响处理结果的那些设置，切语言前后必须完全一致
LOGIC = ('digit_val', 'rotate_val', 'quality_val', 'ico_format_val',
         'mem_limit_val', 'timeout_val', 'max_edge_val')


def logic_values():
    return {n: getattr(app, n).get() for n in LOGIC}


base = logic_values()
check('基线值都是中文', all(has_cjk(v) or v[0].isdigit() or v[0] == '不'
                           for v in base.values()), str(base))
check('中文模式下按钮是中文', app.btn_run.cget('text') == '🚀 开始执行',
      app.btn_run.cget('text'))

print('\n[3] 切到英文')
app.switch_language('en')
app.root.update_idletasks()

check('语言真的切了', i18n.get_lang() == 'en')
check('导出文件夹后缀的默认值跟着变成英文（它是磁盘上的文件夹名）',
      app.dir_suffix_val.get() == '_processed', app.dir_suffix_val.get())
check('按钮文字变英文', not has_cjk(app.btn_run.cget('text')),
      app.btn_run.cget('text'))
check('分组标题变英文', not has_cjk(app.lbl_mode.cget('text')),
      app.lbl_mode.cget('text'))

# ★ 这一条是整件事的核心不变量 ★
after = logic_values()
check('★ 读回来的设置值一个字都没变', after == base,
      '变了: ' + str({k: (base[k], after[k])
                      for k in base if base[k] != after[k]}))

# 下拉框显示的是英文（widget 自己的 get() 拿的是界面上那串字）
check('下拉框显示英文', not has_cjk(app.cb_quality.get()),
      app.cb_quality.get())
check('下拉框选项列表全是英文',
      not any(has_cjk(v) for v in app.cb_quality.cget('values')),
      str(app.cb_quality.cget('values')))
check('超时下拉框也翻了（它的标签要去查表换秒数，最容易被弄坏）',
      not has_cjk(app.cb_timeout.get()), app.cb_timeout.get())

# 超时标签 → 秒数：英文模式下这张表还得查得到
from imgwb.config import TIMEOUT_CHOICES
secs = dict(TIMEOUT_CHOICES).get(app.timeout_val.get(), 'NOT-FOUND')
check('英文模式下「超时标签→秒数」照样查得到', secs == 120, str(secs))

# 代码里写中文、界面显示英文
app.quality_val.set('网络压缩 (85%)')
check('用中文赋值，读回还是中文', app.quality_val.get() == '网络压缩 (85%)',
      app.quality_val.get())
check('用中文赋值，界面显示英文', not has_cjk(app.cb_quality.get()),
      app.cb_quality.get())
app.quality_val.set(base['quality_val'])

# 日志
app.log('使用多线程模式')
app.flush_logs()
tail = app.log_text.get('1.0', 'end').strip().rsplit('\n', 1)[-1]
check('日志是英文', not has_cjk(tail), tail)

print('\n[4] 切回中文')
app.switch_language('zh')
app.root.update_idletasks()

check('按钮文字还原', app.btn_run.cget('text') == '🚀 开始执行',
      app.btn_run.cget('text'))
check('分组标题还原', has_cjk(app.lbl_mode.cget('text')),
      app.lbl_mode.cget('text'))
back = logic_values()
check('★ 来回切一趟，设置值还是原样', back == base,
      '变了: ' + str({k: (base[k], back[k]) for k in base if base[k] != back[k]}))
check('下拉框显示回中文', has_cjk(app.cb_quality.get()), app.cb_quality.get())
check('导出文件夹后缀的默认值也回到中文',
      app.dir_suffix_val.get() == '_已处理', app.dir_suffix_val.get())
app.dir_suffix_val.set('_我自己起的')
app.switch_language('en')
kept_en = app.dir_suffix_val.get()
app.switch_language('zh')
check('用户自己填的后缀，来回切语言都不动',
      kept_en == '_我自己起的' and app.dir_suffix_val.get() == '_我自己起的',
      f'{kept_en} / {app.dir_suffix_val.get()}')
app.dir_suffix_val.set('_已处理')
app.log('使用多线程模式')
app.flush_logs()
tail = app.log_text.get('1.0', 'end').strip().rsplit('\n', 1)[-1]
check('日志回中文', has_cjk(tail), tail)

# ---------------------------------------------------------------- 收尾
try:
    app.root.destroy()
except Exception:
    pass

if _saved is None:
    try:
        os.remove(_pref)
    except OSError:
        pass
else:
    with open(_pref, 'w', encoding='utf-8') as fh:
        fh.write(_saved)
print('\n（已还原用户原本的语言偏好）')

print('\n' + ('全部通过' if not fails else f'失败 {len(fails)} 项: {fails}'))
sys.exit(1 if fails else 0)
