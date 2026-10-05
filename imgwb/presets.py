"""一键预设 —— 主界面上那几个「点了就能用」的按钮。

两条设计约束：

1. **默认必须安全**。主界面是给不想琢磨参数的人用的，所以每个预设都导出到
   源文件夹旁边的「xxx_已处理」，绝不动原文件。要原地修改请去高级设置。
2. **一次只做一件事**。预设不叠加，点哪个就只做哪个，免得用户猜不到结果。

``settings`` 里的键就是 ImageProcessorApp 上那些 Tk 变量的属性名，
apply_preset 会逐个 set 过去，所以高级设置里能看到预设选了什么。
"""

# ============================================================
# 【导读】presets.py —— 简易模式里那几个「一键」按钮，各自对应哪一组设置。
# ============================================================

PRESETS = [
    {
        'key': 'compress',
        'label': '📦 一键压缩',
        'desc': '大幅缩小体积，文件名不变',
        # 这一档对 PNG 会走调色板量化（181 万色 -> 256 色，肉眼可见色带）。
        # 高级模式底部本来就有一行警告，但简易模式看不到 —— 而点这个按钮的
        # 恰恰是那批看不到警告的用户。所以把它写进按钮自己的提示里。
        # chr(10) 而不是转义符：这份文件里其它地方也没有转义，保持一致。
        'warn': ('PNG 会被压成 256 色（有损，可能出现色带）；' + chr(10)
                 + '已经压过的 JPEG 若压不动，会自动保留原文件。'),
        'style': 'success',
        'settings': dict(rotate_val='不旋转', quality_val='网络压缩 (85%)',
                         compress_only=True, name_mode='keep', is_ico=False),
    },
    {
        'key': 'upright',
        'label': '🔄 一键转正',
        # 别再写「画质无损」：这一档走的是 quality=100 重存 JPEG，而 JPEG 的
        # 100% 依然要过一遍 DCT 量化。实测同一张 q85 照片连跑三轮，体积 4.56x
        # 而且逐轮还在涨，最大像素差 4->5 —— 像素真的在变。真无损得做 jpegtran
        # 式的无损变换，Pillow 给不了；只改 EXIF 标记位又会让照片在所有看图
        # 软件里都躺倒，更不对。所以退一步，如实说是「最高画质重存」。
        'desc': '把躺倒的手机照片摆正（最高画质重存，非无损）',
        'style': 'info',
        'settings': dict(rotate_val='不旋转', quality_val='极致保真 (100%)',
                         compress_only=True, name_mode='keep', is_ico=False),
    },
    {
        'key': 'number',
        'label': '🔢 一键编号',
        'desc': '按顺序改名 001、002…，不重新编码',
        'style': 'primary',
        'settings': dict(rotate_val='不旋转', quality_val='极致保真 (100%)',
                         compress_only=False, name_mode='number', is_ico=False),
    },
    {
        'key': 'cw90',
        'label': '↻ 顺时针 90°',
        'desc': '整批向右转，文件名不变',
        'style': 'warning',
        'settings': dict(rotate_val='顺时针 90°', quality_val='通用高清 (95%)',
                         compress_only=False, name_mode='keep', is_ico=False),
    },
    {
        'key': 'ccw90',
        'label': '↺ 逆时针 90°',
        'desc': '整批向左转，文件名不变',
        'style': 'warning',
        'settings': dict(rotate_val='逆时针 90°', quality_val='通用高清 (95%)',
                         compress_only=False, name_mode='keep', is_ico=False),
    },
    {
        'key': 'ico',
        'label': '🎨 转成图标',
        'desc': '做成多尺寸 ICO（256~16）',
        'style': 'secondary',
        'settings': dict(rotate_val='不旋转', quality_val='极致保真 (100%)',
                         compress_only=False, name_mode='keep', is_ico=True,
                         ico_mode_val='multi', ico_format_val='自动 (推荐)'),
    },
]

BY_KEY = {p['key']: p for p in PRESETS}
