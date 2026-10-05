# -*- coding: utf-8 -*-
"""翻译覆盖率体检：把界面可见的中文全抽出来，报告哪些还没翻。

用法:  python 检查翻译覆盖.py
退出码 0 = 全覆盖，1 = 还有没翻的（清单打在屏幕上）。

思路：用 AST 找出所有「给用户看的字符串」（`log()` / `text=` / `tip()` / 弹窗
/ `logfile.op()` 等），把 f-string 的变量部分换成 `{}` 变成模板，然后在英文模式
下逐条 `tr()`，看结果里还有没有汉字。

**两个踩过的坑都在抽取器自己身上**（工具和被测代码犯同一个错时，什么都看不见）：

1. 一开始把 key `.strip()` 了，于是 `text=' 处理选项 '` 这种靠空格留白的标题
   报的是「已翻」—— 实际上 `tr()` 当时没剥行尾空格，界面上那 7 个分组标题全是中文。
2. 一开始遇到三元表达式就整句返回 None，于是 banner 那句
   `f'启动 {APP_NAME}' + ('（打包版）' if frozen else '（源码运行）')`
   从没被收集到。改成**把两个分支都展开**成独立模板 —— 只粗化成 `{}` 的话
   会拼出一个根本不存在的模板，反过来误报一堆「没翻」。

已知局限：从字典或变量里取的文案抽不到，比如一键按钮的 `preset['label']`。
那部分由 `tests/test_i18n.py` 直接遍历 `PRESETS` 来盯。
"""
import ast
import glob
import itertools
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from imgwb import i18n                                        # noqa: E402

CJK = re.compile(r'[\u4e00-\u9fff]')
BASE = os.path.join(HERE, 'imgwb')
EXTRA = [os.path.join(HERE, '融合_v4ce.py')]
UI_CALLS = {'log', 'showinfo', 'showwarning', 'showerror', 'askokcancel',
            'askyesno', 'tip', 'op', 'write', 'title'}
UI_KW = {'text', 'title', 'values', 'message'}

# 故意保持双语的几条，不该报「没翻」
ALLOW = {'🌐 中 / EN',
         '切换界面语言（中文 / English）\n'
         'Switch interface language. The log file follows the same setting.'}


def templates(node):
    """这个字符串节点可能长成的样子，返回列表（三元表达式展开成多条）。"""
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            out.append(str(part.value) if isinstance(part, ast.Constant)
                       else '{}')
        return [''.join(out)]
    if isinstance(node, ast.IfExp):
        return templates(node.body) + templates(node.orelse)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = templates(node.left), templates(node.right)
        if left and right:
            return [a + b for a, b in itertools.product(left, right)]
        return []
    return []


def collect():
    found = {}
    files = [p for p in sorted(glob.glob(os.path.join(BASE, '*.py')))
             if os.path.basename(p) != 'i18n.py'] + EXTRA
    for p in files:
        tree = ast.parse(open(p, encoding='utf-8').read())
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            name = getattr(n.func, 'attr', None) or getattr(n.func, 'id', None)
            cands = []
            if name in UI_CALLS:
                for a in n.args:
                    cands += templates(a)
            for kw in n.keywords:
                if kw.arg in UI_KW:
                    cands += templates(kw.value)
            for t in cands:
                if t and CJK.search(t):
                    found.setdefault(t, set()).add(os.path.basename(p))
    return found


def main():
    i18n.set_lang('en')
    found = collect()
    miss = []
    skipped = 0
    for t, files in found.items():
        if t in ALLOW:
            skipped += 1
            continue
        # 占位符模板：填个样例值再翻，才和运行时一致
        if CJK.search(i18n.tr(t.replace('{}', '7'))):
            miss.append((t, sorted(files)))

    total = len(found) - skipped
    print('界面可见的中文模板：%d 条（另有 %d 条故意双语，不计）'
          % (total, skipped))
    print('翻完的：%d 条' % (total - len(miss)))
    print('还没翻的：%d 条' % len(miss))
    # 清单直接打在屏幕上，不落文件 —— 这是个随手跑的检查工具，
    # 不该在项目目录里留下产物。
    for t, files in sorted(miss, key=lambda kv: (kv[1], kv[0])):
        print('  [%s] %s' % (','.join(files), t.replace('\n', ' / ')[:92]))
    return 1 if miss else 0


if __name__ == '__main__':
    sys.exit(main())
