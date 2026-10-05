"""中英界面切换。

# ============================================================
# 【导读】i18n.py —— 界面语言。中文是母版，英文是翻译。
# 核心约定：**内部值永远是中文**。下拉框显示英文，但变量里存的、
#   传给处理流程的、测试里断言的，一律是中文原文。
# 先看：tr()（显示时翻译）、canon()（读回时还原）、options()（下拉框选项）。
# 切换语言在 app.py 的 switch_language()。
# ============================================================

**为什么是"显示层翻译"而不是把文案改成英文**

界面上的选项文字同时是程序的判断依据 —— `rotate_mode == '不旋转'`、
`'95%' in quality_opt` 在 pipeline 里出现十几处，`dict(TIMEOUT_CHOICES).get(...)`
也按中文标签查表，34 个测试里还有大量 `app.quality_val.set('通用高清 (95%)')`。
真把这些字符串换成英文，这些逻辑会**全部静默失效**（匹配不上 → 走默认分支，
不报错，但行为全错）。

所以：中文是唯一的内部表示，英文只在三个出口出现 ——

1. 控件文字（按钮、标签、提示）：切换时遍历控件树重新设文字
2. 下拉框选项：显示翻译后的，读回时用 `canon()` 还原成中文
3. 日志和弹窗：在 `log()` / 弹窗代理这两个出口把整句翻掉

**没覆盖到的句子会原样返回中文**，不会变成空白、也不会报错 —— 这是故意的，
翻译表漏一条顶多是一句中文混在英文里，而不是功能坏掉。
"""
import os
import re
import sys

__all__ = ['tr', 'canon', 'options', 'join', 'set_lang', 'get_lang', 'available',
           'load_pref', 'save_pref', 'system_default', 'default_dir_suffix']

_lang = 'zh'          # 默认中文：这样库和测试的行为是确定的，不随机器区域设置变
_cache = {}


# ---------------------------------------------------------------- 下拉框选项
# 这些字符串**同时是程序的判断依据**，翻译只影响显示，读回时必须 canon() 还原。
OPTIONS = {
    # 旋转
    '不旋转': 'No rotation',
    '顺时针 90°': 'Clockwise 90°',
    '逆时针 90°': 'Counter-clockwise 90°',
    '旋转 180°': 'Rotate 180°',
    # 画质
    '极致保真 (100%)': 'Maximum quality (100%)',
    '通用高清 (95%)': 'High quality (95%)',
    '网络压缩 (85%)': 'Web-optimized (85%)',
    # 编号位数
    '自动 (按数量)': 'Auto (by count)',
    '1位': '1 digit',
    '2位': '2 digits',
    '3位 (001)': '3 digits (001)',
    '4位': '4 digits',
    '5位': '5 digits',
    # ICO
    '自动 (推荐)': 'Auto (recommended)',
    '强制 PNG (支持透明)': 'Force PNG (keeps transparency)',
    '强制 BMP (经典兼容)': 'Force BMP (legacy)',
    '标准多尺寸 (256-16)': 'Standard multi-size (256-16)',
    # 内存上限
    '自动 (按可用内存)': 'Auto (by free memory)',
    # 最长边
    '不限 (保持原尺寸)': 'Unlimited (keep original)',
    '3840 (4K)': '3840 (4K)',
    '2560 (2K)': '2560 (2K)',
    '1920 (1080p)': '1920 (1080p)',
    '1280 (720p)': '1280 (720p)',
    # 单张超时
    '关闭 (一直等)': 'Off (wait forever)',
    '30 秒': '30 seconds',
    '60 秒': '60 seconds',
    '120 秒 (默认)': '120 seconds (default)',
    '300 秒': '300 seconds',
}

# ---------------------------------------------------------------- 静态界面文字
UI = {
    # 区块标题与按钮
    '图片来源': 'Source',
    '一键处理': 'One-click',
    '处理选项': 'Options',
    '命名规则': 'Naming',
    '导出设置': 'Output',
    '执行状态': 'Status',
    '处理日志:': 'Log:',
    '浏览...': 'Browse...',
    '选择...': 'Choose...',
    '递归扫描子文件夹': 'Scan subfolders',
    '🚀 开始执行': '🚀 Start',
    '⏸ 暂停': '⏸ Pause',
    '▶ 继续': '▶ Resume',
    '⏹ 取消': '⏹ Cancel',
    '⛔ 强制停止': '⛔ Force stop',
    '⚙ 高级设置...': '⚙ Advanced...',
    '← 返回简易模式': '← Back to simple mode',
    '📋 查看错误 (0)': '📋 Errors (0)',
    # 选项标签
    '旋转:': 'Rotate:',
    '画质:': 'Quality:',
    '仅压缩': 'Compress only',
    '转成 ICO 图标': 'Convert to ICO',
    '性能模式:': 'Performance:',
    '内存上限:': 'Memory limit:',
    '最长边不超过:': 'Max edge:',
    '单张超时:': 'Per-image timeout:',
    '安全写入': 'Safe write',
    '带上不支持的文件': 'Carry unsupported files',
    '动图也重编码': 'Re-encode animations',
    '只缩不放，小图不动': 'Shrinks only; small images untouched',
    '超时就改名直接复制，不处理': 'On timeout: rename and copy as-is',
    '慢且通常更大，除非你确实要换画质': 'Slower and usually larger, unless you really need a quality change',
    '(处理期间临时占用约一倍磁盘空间)': '(temporarily uses about twice the disk space)',
    '(省磁盘，但中途取消只能停在当前进度)': '(saves disk, but cancelling stops at current progress)',
    '(原图小时保留，大时压缩)': '(keeps small originals, compresses large ones)',
    '(Windows 10+ 选自动)': '(choose Auto on Windows 10+)',
    '✓ 保留 ICC 色彩配置  ✓ 保留文件时间  ✓ 自动修正 Exif 旋转':
        '✓ Keeps ICC profile  ✓ Keeps timestamps  ✓ Auto-fixes EXIF orientation',
    '⚠️ PNG 网络压缩模式会进行色彩量化 (有损)':
        '⚠️ Web-optimized mode quantizes PNG colors (lossy)',
    # 命名
    '命名方式:': 'Naming:',
    '前缀+编号': 'Prefix + number',
    '纯数字编号': 'Number only',
    '保留原名': 'Keep original name',
    '前缀:': 'Prefix:',
    '起始编号:': 'Start at:',
    '位数:': 'Digits:',
    '预览:': 'Preview:',
    '自动 = 按文件总数定宽，再多留一位（1234 个 → 00001）':
        'Auto = width from the file count, plus one spare digit (1234 files → 00001)',
    # 输出
    '原地修改 (⚠️ 注意备份)': 'Modify in place (⚠️ back up first)',
    '另存为新位置:': 'Save to another location:',
    '保持原目录结构': 'Keep folder structure',
    '➜  输出文件夹名:': '➜  Output folder name:',
    # ICO
    '内部格式:': 'Internal format:',
    '智能缩放:': 'Smart scaling:',
    '固定尺寸:': 'Fixed size:',
    '💡 提示：多选尺寸会打包成单个 ICO。若转换结果不符合预期，请仅勾选所需的一个尺寸。':
        '💡 Tip: selecting several sizes packs them into one ICO. '
        'If the result is not what you expect, tick only the single size you need.',
    # 状态与提示
    '准备就绪': 'Ready',
    '处理中...': 'Processing...',
    '正在取消...': 'Cancelling...',
    '正在停止...': 'Stopping...',
    '正在强制停止...': 'Force stopping...',
    '正在保存已完成的结果，请稍候…': 'Saving finished results, please wait…',
    '这个文件夹里没有可处理的文件': 'No processable files in this folder',
    '专业模式:': 'Advanced:',
    '简易模式 —— 选好文件夹，点一个按钮就行':
        'Simple mode — pick a folder, click one button',
    '高级模式 —— 自定义参数后点「开始执行」':
        'Advanced mode — set the options, then click Start',
    '选好文件夹，点一个就行 —— 结果输出到源文件夹旁边的「xxx_已处理」，原文件不动。':
        'Pick a folder and click one button. Results go to a sibling folder '
        '"xxx_processed"; your originals are untouched.',
    '💡 也可以直接拖拽文件夹或多张图片到这里':
        '💡 You can also drag a folder or images here',
    '📥 拖拽文件夹或图片到此处快速导入（地址栏也可用分号隔开填多条路径）':
        '📥 Drop a folder or images here (the path box also accepts several '
        'paths separated by semicolons)',
    '选择图片来源文件夹': 'Choose source folder',
    '选择导出位置': 'Choose output location',
    '并入': 'Merge',
    '另建新目录': 'New folder',
    '空': 'empty',
    '两批产物混在一起，之后分不清哪个是哪次的结果。':
        'The two batches end up mixed together and you cannot tell them apart later.',
    # 悬停提示
    '停止本次处理。安全写入开启时会完整撤回已做的改动。':
        'Stops this run. With Safe write on, everything already done is rolled back.',
    '停止派发新任务，在跑的几张会先完成。继续时从断点接着来，不重跑。':
        'Stops handing out new work; images already running finish first. '
        'Resuming continues where it left off — nothing is redone.',
    '立刻杀掉全部工作进程，不等任何在途任务。':
        'Kills every worker process immediately, without waiting for anything in flight.',
    '把图片做成 Windows 图标。\n尺寸上限 256 —— 这是 ICO 格式本身的限制（目录项用单字节存宽高）。':
        'Turns images into Windows icons.\nMaximum size is 256 — a limit of the '
        'ICO format itself (its directory stores width and height in one byte).',
    '不旋转时也按「画质」重新编码一遍。\n想单纯压缩体积、或者想把手机照片的方向标记烘焙进像素，就开它。':
        'Re-encodes at the chosen quality even when no rotation is applied.\n'
        'Turn it on to shrink files, or to bake a phone photo orientation flag '
        'into the pixels.',
}

# ---------------------------------------------------------------- 带变量的句子
# 左边是中文模板（{} 代表一个变量），右边是英文模板（{0} {1} 按顺序对应）。
# 运行时拿正则把实际数值抠出来，再填进英文模板。
MSG = [
    # ---- 扫描与识别 ----
    ('开始扫描文件...', 'Scanning files...'),
    ('找到 {} 个图片文件', 'Found {0} image files'),
    ('找到 {} 个图片 (含 {} 个 HEIC)', 'Found {0} images ({1} HEIC)'),
    ('找到 {} 个文件，可以开始', 'Found {0} files — ready to start'),
    ('已选 {} 个文件，可以开始', '{0} files selected — ready to start'),
    ('🔍 正在识别文件…', '🔍 Identifying files...'),
    ('📋 识别完成（{} 秒）—— 共 {} 个文件、{} MB',
     '📋 Identified in {0}s — {1} files, {2} MB'),
    ('预计耗时约 {}（{} 百万像素，并发 {}，单张平均预估 {} MB，内存上限同时放得下 {} 张；开跑后以进度条上的实际速度为准）',
     'Estimated time about {0} ({1} megapixels, {2} workers, {3} MB average per '
     'image, memory limit fits {4} at once; the progress bar shows the real rate '
     'once it starts)'),
    ('📄 {} 个不支持的文件将按原样复制并一起编号: {}',
     '📄 Unsupported files to copy as-is and number along: {0} ({1})'),
    ('ℹ️ 已跳过 {} 个不支持的文件: {}', 'ℹ️ Skipped {0} unsupported files: {1}'),
    ('ℹ️ 已跳过 {} 个隐藏文件', 'ℹ️ Skipped {0} hidden files'),
    ('ℹ️ 其中 {} 个文件格式不支持，已忽略',
     'ℹ️ {0} of them are in unsupported formats and were ignored'),
    ('⚠️ {} 个文件的扩展名和真实格式对不上，例如 {}（实际是 {}）',
     '⚠️ {0} files whose extension does not match their real format, '
     'e.g. {1} (actually {2})'),
    ('⚠️ {} 张带透明通道的图会按目标格式编码，**透明会丢**，例如 {}（{}）',
     '⚠️ {0} images with transparency will be encoded to the target format and '
     '**lose their transparency**, e.g. {1} ({2})'),
    ('⚠️ 本批有 {} 个 PNG，网络压缩档会把它们量化成 256 色（有损，可能出现色带）',
     '⚠️ This batch has {0} PNGs; Web-optimized mode quantizes them to 256 '
     'colors (lossy, may cause banding)'),
    # ---- 进度 ----
    ('处理中: {}/{}', 'Processing: {0}/{1}'),
    ('处理中: 0/{}', 'Processing: 0/{0}'),
    ('已暂停 {}/{}', 'Paused {0}/{1}'),
    ('▶ 从 {}/{} 继续', '▶ Resuming from {0}/{1}'),
    ('⏸ 已暂停（{}/{}），在跑的几张会先完成',
     '⏸ Paused ({0}/{1}); images already running will finish'),
    ('⏳ 已 {} 秒没有任务完成，{} 个还在处理：',
     '⏳ Nothing finished for {0}s; {1} still running:'),
    ('{}  预估 {} MB  已跑 {} 秒', '{0}  est. {1} MB  running {2}s'),
    ('ⓘ 大图 {} 预计占用 {} MB，已按闸门限流',
     'ⓘ Large image {0} needs about {1} MB — throttled by the memory gate'),
    # ---- 结果与统计 ----
    ('性能模式 — {}', 'Performance mode — {0}'),
    ('输出到 {}', 'Output to {0}'),
    ('⏱ 分段：扫描 {}s、编排 {}s、处理 {}s、收尾 {}s',
     '⏱ Phases: scan {0}s, plan {1}s, process {2}s, finish {3}s'),
    ('⏱ 实际吞吐 {} MB/秒（共搬运 {} MB）  子进程用过 {} 个',
     '⏱ Real throughput {0} MB/s ({1} MB moved), {2} worker processes used'),
    ('⏱ 耗时分解：墙上 {} 秒 × 并发 {} = {} 工人秒，实际在图像处理上 {} 秒（{}%）',
     '⏱ Where the time went: {0}s wall × {1} workers = {2} worker-seconds, '
     '{3}s actually spent on images ({4}%)'),
    ('其中 {}', 'of which {0}'),
    ('有 {} 秒不在图像处理上 —— 排队、子进程回收或结果传输，不是图片本身慢',
     '{0}s was spent outside image work — queueing, worker recycling or result '
     'transfer, not the images themselves'),
    ('ℹ️ {} 张已经是最优（重新压缩反而更大或该格式没有画质参数），已保留原文件',
     'ℹ️ {0} images were already optimal (recompressing made them larger, or the '
     'format has no quality setting) — originals kept'),
    ('ℹ️ 时间几乎都花在编码上，而其中 {} 张编完又被丢弃了（压不小）。换个更低的画质档，或者这批本来就不需要压缩。',
     'ℹ️ Almost all the time went into encoding, and {0} of those results were '
     'thrown away for being larger. Try a lower quality tier, or this batch may '
     'simply not need compressing.'),
    ('🔻 {} 张已缩到最长边 ≤ {}', '🔻 {0} images resized to a max edge of {1}'),
    ('⚠️ {} 张动图因目标格式存不下多帧，只保留了第一帧（动画没了）',
     '⚠️ {0} animations lost their motion — the target format cannot hold '
     'multiple frames, so only the first was kept'),
    ('ⓘ 动图 {}（{} 帧）不旋转，原样带过、不重编码',
     'ⓘ Animation {0} ({1} frames): no rotation, copied as-is without re-encoding'),
    ('ⓘ 共 {} 个动图原样带过（不旋转时不重编码，帧、帧延时和循环次数原样保留）',
     'ⓘ {0} animations copied as-is (no re-encode without rotation; frames, '
     'delays and loop count are preserved)'),
    ('ⓘ {} 个多帧图不重编码，原样带过',
     'ⓘ {0} multi-frame images copied as-is without re-encoding'),
    ('全部完成！共 {} 张', 'All done — {0} images'),
    ('✅ 完成！共 {} 张，耗时 {} 分钟（平均 {} 张/秒）',
     '✅ Done — {0} images in {1} minutes ({2} images/s)'),
    # ---- 失败与错误 ----
    ('⚠️ {} 个失败：{}', '⚠️ {0} failed: {1}'),
    ('[失败] {} | {} | {}', '[FAILED] {0} | {1} | {2}'),
    ('详情 {}: {}', 'detail {0}: {1}'),
    ('✗ 失败较多，后续只计数，跑完点「查看错误」看全部',
     '✗ Too many failures — counting only from here; click Errors when it '
     'finishes to see them all'),
    ('❌ {} 打不开：{}', '❌ Cannot open {0}: {1}'),
    ('❌ 另有 {} 个打不开的文件（详见系统日志）',
     '❌ {0} more files could not be opened (see the log file)'),
    ('❌ 错误: {}', '❌ Error: {0}'),
    ('完整调用栈见日志: {}', 'Full traceback in the log file: {0}'),
    ('预扫描打不开: {} -> {}', 'Pre-scan could not open: {0} -> {1}'),
    ('⚠️ 识别阶段出错（{}），改成边处理边估算',
     '⚠️ Identification failed ({0}); falling back to estimating while processing'),
    # ---- 取消 / 停止 ----
    ('⚠️ 用户请求取消...', '⚠️ Cancel requested...'),
    ('⚠️ 已取消！完成 {}/{}', '⚠️ Cancelled — {0}/{1} done'),
    ('已取消\n完成: {}/{}', 'Cancelled\nDone: {0}/{1}'),
    ('↩️ 已撤回全部 {} 个改动，原文件未受影响',
     '↩️ Rolled back all {0} changes; your originals are untouched'),
    ('↩️ 已撤回 {} 个改动；直写模式下另有 {} 个文件已经落盘，无法撤回',
     '↩️ Rolled back {0} changes; {1} files were written directly and cannot be '
     'rolled back'),
    ('⛔ 强制停止：正在终止全部工作进程...',
     '⛔ Force stop: terminating all worker processes...'),
    ('⏹ 已终止 {} 个工作进程', '⏹ Terminated {0} worker processes'),
    ('已终止 {} 个工作进程，正在撤回临时文件...',
     'Terminated {0} worker processes; rolling back temporary files...'),
    ('直写模式：等在途任务写完，以免留下截断的文件',
     'Direct-write mode: waiting for in-flight work to finish so no truncated '
     'files are left behind'),
    ('🧹 清理了 {} 个残留临时文件', '🧹 Cleaned up {0} leftover temporary files'),
    ('🧹 已清空导出目录，删除 {} 项', '🧹 Output folder emptied — {0} items deleted'),
    # ---- 超时与调度 ----
    ('单张超时 {} 秒，超过就改名直接复制',
     'Per-image timeout {0}s — anything slower is renamed and copied as-is'),
    ('ℹ️ 单张超时只在多进程模式下有效（超过 {} 个文件才启用），本次不生效',
     'ℹ️ The per-image timeout only works in multi-process mode (over {0} files); '
     'it is inactive this run'),
    ('⏱ {} 超过 {} 秒还没好，放弃处理、改名直接复制',
     '⏱ {0} took more than {1}s — giving up on processing, renamed and copied as-is'),
    ('⏱ 有 {} 张超过 {} 秒，已放弃处理、原样复制带走（文件在、编号也对）',
     '⏱ {0} images took more than {1}s and were copied as-is instead '
     '(the files are there and correctly numbered)'),
    ('已重建工作进程池，{} 个任务重排',
     'Worker pool rebuilt; {0} tasks requeued'),
    ('子进程每处理 {} 张才回收（这批图平均预估 {} MB，回收得太勤反而拖慢）',
     'Workers are recycled every {0} images (this batch averages {1} MB each; '
     'recycling more often would only slow things down)'),
    ('子进程已降到后台优先级，磁盘 I/O 让给前台程序',
     'Workers run at background priority, leaving disk I/O to foreground apps'),
    ('（当前 Python 不支持子进程回收，内存占用会偏高，建议升级到 3.11+）',
     '(this Python cannot recycle workers, so memory use will be higher — '
     'consider upgrading to 3.11+)'),
    # ---- 内存 ----
    ('内存上限 {} MB（手动设定）', 'Memory limit {0} MB (manual)'),
    ('内存上限 {} MB（自动）', 'Memory limit {0} MB (auto)'),
    ('当前约 {} MB', 'currently about {0} MB'),
    ('⚠️ 内存告急，已把并发压到 1、内存上限降到 {} MB',
     '⚠️ Memory critical — concurrency dropped to 1 and the limit lowered to {0} MB'),
    ('⚠️ 系统内存吃紧（剩余 {} GB / {} GB）。可以点「暂停」缓一缓，或换到「节能」模式降低并发。',
     '⚠️ System memory is tight ({0} GB free of {1} GB). You can Pause for a '
     'moment, or switch to Eco mode to lower concurrency.'),
    # ---- 诊断提示 ----
    ('⚠️ 绝大部分时间**不在图像处理上** —— 通常是磁盘慢、杀毒软件实时扫描、或子进程反复重建。可以试试关掉「安全写入」、把源和导出放在同一块盘、或把杀软对这两个目录设为排除。',
     '⚠️ Most of the time was **not spent on image work** — usually a slow disk, '
     'real-time antivirus scanning, or workers being rebuilt over and over. '
     'Try turning off Safe write, keeping source and output on the same drive, '
     'or excluding both folders from your antivirus.'),
    ('⚠️ 同时吞吐低于 20 MB/秒，优先怀疑网络盘/云同步目录或杀软实时扫描。',
     '⚠️ Throughput is also below 20 MB/s — suspect a network or cloud-synced '
     'folder, or real-time antivirus scanning.'),
    # ---- 拖拽与路径 ----
    ('📥 已拖拽导入 {} 个文件，来自 {} 条源路径',
     '📥 Imported {0} dropped files from {1} source paths'),
    ('📁 已通过拖拽设置源文件夹: {}', '📁 Source folder set by drop: {0}'),
    ('📁 已通过拖拽设置导出位置: {}', '📁 Output location set by drop: {0}'),
    ('📁 已切换到文件夹模式，之前拖拽的文件不再参与处理',
     '📁 Switched to folder mode; previously dropped files are no longer included'),
    ('📁 {} 条源路径：{}', '📁 {0} source paths: {1}'),
    ('📁 本批并入已有的导出目录', '📁 This batch is merged into the existing output folder'),
    ('📁 本批改用后缀「{}」，与上一批分开',
     '📁 This batch uses the suffix "{0}" to keep it separate from the last one'),
    ('• …… 其余 {} 条', '• ... {0} more'),
    ('清空后写入（删 {} 项）', 'Empty it first ({0} items will be deleted)'),
    ('导出目录里已经有 {} 个文件：', 'The output folder already contains {0} files:'),
    ('支持: {}', 'Supported: {0}'),
    ('固定 {} MB', 'Fixed {0} MB'),
    ('自动 {}', 'Auto {0}'),
    ('📋 查看错误 ({})', '📋 Errors ({0})'),
    ('IO错误: {}  |  图片损坏: {}  |  其他: {}',
     'I/O errors: {0}  |  Corrupt images: {1}  |  Other: {2}'),
]

# ---- 第二批：流水线的错误原因、识别摘要、失败汇总 ----------------------
# 这些句子不是直接传给 log() 的，而是拼出来的（错误原因塞进 "✗ 文件 — 原因"，
# 摘要拼成一行），所以第一遍抽取没抓到。引擎支持对拼进去的中文片段递归翻译。
MSG += [
    # 认不出文件时的具体说法（describe_broken）
    ('文件是空的（0 字节），里面什么都没有', 'The file is empty (0 bytes)'),
    ('文件读不到（{}）', 'Cannot read the file ({0})'),
    ('文件打不开（{}）', 'Cannot open the file ({0})'),
    ('这不是图片，像是文本文件（{}，开头是「{}」）',
     'Not an image — looks like a text file ({0}, starts with "{1}")'),
    ('这不是图片，看内容像是{}（{}）',
     'Not an image — the contents look like {0} ({1})'),
    ('{}的开头是正常的，但后面的数据已损坏或不完整（文件 {}，可能没下载完 / 复制中断）',
     'The header of this {0} is fine, but the data after it is damaged or '
     'incomplete ({1} — possibly an interrupted download or copy)'),
    ('认不出的文件格式（{}，文件头 {}）',
     'Unrecognized file type ({0}, header {1})'),
    ('{} 字节', '{0} bytes'),
    # 文件类型标签
    ('PNG 图片', 'a PNG image'),
    ('JPEG 图片', 'a JPEG image'),
    ('GIF 图片', 'a GIF image'),
    ('BMP 图片', 'a BMP image'),
    ('TIFF 图片', 'a TIFF image'),
    ('RIFF 容器（WebP / WAV / AVI）', 'a RIFF container (WebP / WAV / AVI)'),
    ('ZIP 压缩包（docx、xlsx、apk 也是这个头）',
     'a ZIP archive (docx, xlsx and apk share this header)'),
    ('PDF 文档', 'a PDF document'),
    ('RAR 压缩包', 'a RAR archive'),
    ('7z 压缩包', 'a 7z archive'),
    ('gzip 压缩包', 'a gzip archive'),
    ('MP3 音频', 'MP3 audio'),
    ('Ogg 音视频', 'Ogg media'),
    ('MP4 / MOV 视频', 'MP4 / MOV video'),
    ('SVG 矢量图（本程序不支持）', 'an SVG vector image (not supported here)'),
    ('XML 文本', 'XML text'),
    ('HTML 文本', 'HTML text'),
    # 其它失败原因
    ('无法识别的图片格式或文件损坏',
     'Unrecognized image format, or the file is damaged'),
    ('无法识别的图片格式或文件已损坏',
     'Unrecognized image format, or the file is damaged'),
    ('文件不完整或已损坏（数据被截断）（{}）',
     'Incomplete or damaged file — the data is truncated ({0})'),
    ('图片数据流已损坏', 'The image data stream is damaged'),
    ('图片数据不完整', 'The image data is incomplete'),
    ('数据不完整或该格式不受支持（解码器建不起来）',
     'Incomplete data, or an unsupported format (the decoder could not start)'),
    ('图片尺寸异常大，已被安全策略拦下',
     'The image is unusually large and was blocked by a safety check'),
    ('图片尺寸异常巨大{}，已跳过以防耗尽内存',
     'The image is enormous{0} — skipped to avoid exhausting memory'),
    ('该格式或该特性不受支持', 'That format or feature is not supported'),
    ('没有权限读写，或文件正被其它程序占用（{}）',
     'No permission, or the file is open in another program ({0})'),
    ('磁盘空间不足: {}', 'Not enough disk space: {0}'),
    ('路径过长: {}', 'Path too long: {0}'),
    ('IO错误: {}', 'I/O error: {0}'),
    ('任务异常终止: {}', 'Task terminated unexpectedly: {0}'),
    ('超过 {} 秒仍未完成，复制也没成功',
     'Still unfinished after {0}s — even copying failed'),
    ('临时文件已不存在，放弃提交以免弄丢原文件: {}',
     'The temporary file is gone; skipping the commit so the original is not '
     'lost: {0}'),
    # 跳过原因
    ('源画质已是 {}%，明显低于目标 {}%',
     'Source quality is already {0}%, well below the {1}% target'),
    ('WebP 已压到 {} bpp（门槛 {}），再压多半更大',
     'This WebP is already at {0} bpp (threshold {1}); recompressing would '
     'most likely make it bigger'),
    ('PNG 源已按不低于 level 6 压过（zlib FLEVEL={}）',
     'This PNG was already compressed at level 6 or higher (zlib FLEVEL={0})'),
    ('{} 没有可调的画质参数', '{0} has no adjustable quality setting'),
    ('不支持的格式，跟着编号原样带走',
     'Unsupported format — copied as-is and numbered along'),
    ('打不开或格式损坏', 'Cannot be opened, or the format is damaged'),
    ('多帧图不重编码，原样复制',
     'Multi-frame image — copied as-is without re-encoding'),
    ('再压一遍也省不下，原样复制',
     'Recompressing would not save anything — copied as-is'),
    # 识别摘要的拼装件
    ('最多重新编码 {} 张（{} MB）', 'at most {0} images re-encoded ({1} MB)'),
    ('原样复制 {} 张（再压也省不下 / 多帧图）',
     '{0} copied as-is (nothing to gain, or multi-frame)'),
    ('跟着编号带走 {} 个（不支持的格式）',
     '{0} carried along with the numbering (unsupported formats)'),
    ('**打不开 {} 个**', '**{0} cannot be opened**'),
    ('格式分布：{}', 'Formats: {0}'),
    ('{} {} 张', '{0} ×{1}'),
    ('{} 分钟', '{0} min'),
    # 耗时分解的几个标签（在 app.py 里拼成「打开 0.1s、解码 1.7s、…」）
    ('打开', 'open'),
    # 性能模式那一行（perf.describe 拼的）
    ('{}：并发 {}，{}', '{0}: {1} workers, {2}'),
    ('节能', 'Eco'),
    ('均衡', 'Balanced'),
    ('高性能', 'Turbo'),
    ('自定义', 'Custom'),
    ('后台优先级（含磁盘 I/O）', 'background priority (including disk I/O)'),
    ('低于普通优先级', 'below-normal priority'),
    ('普通优先级', 'normal priority'),
    ('使用多进程模式', 'Using multi-process mode'),
    ('使用多线程模式', 'Using multi-threaded mode'),
    # 失败汇总里的原因是被截断过的（只取括号前那一截），单独给词条
    ('文件是空的', 'the file is empty'),
    ('这不是图片', 'not an image'),
    ('文件不完整或已损坏', 'incomplete or damaged file'),
    ('无法识别的图片格式或文件损坏', 'unrecognized format or damaged file'),
    ('认不出的文件格式', 'unrecognized file type'),
    ('没有权限读写，或文件正被其它程序占用', 'no permission, or file in use'),
    ('磁盘空间不足', 'not enough disk space'),
    ('路径过长', 'path too long'),
    ('IO错误', 'I/O error'),
    ('解码', 'decode'),
    ('编码写盘', 'encode+write'),
    ('复制', 'copy'),
    ('{} 秒', '{0}s'),
    # 失败汇总
    ('✗ {} — {}', '✗ {0} — {1}'),
    ('{} 个「{}」', '{0} × "{1}"'),
    ('；另有 {} 类', '; {0} more kinds'),
    ('  · {} 个 {}', '  · {0} × {1}'),
    ('  · 另有 {} 类，详见清单', '  · {0} more kinds — see the full list'),
    ('完成！成功: {}，失败: {}\n\n失败原因：\n{}\n\n点界面下方的「查看错误」可以看完整清单。',
     'Finished. Succeeded: {0}, failed: {1}\n\nWhy they failed:\n{2}\n\n'
     'Click Errors at the bottom of the window for the full list.'),
]


# ---- 第三批：长提示语、操作日志、日志头 --------------------------------
# 长提示语是从源码里原样取出来的（见 scratchpad/patch_batch3.py），
# 不是手抄 —— 差一个标点就永远匹配不上，而且不会报错。
UI.update({
    'mp4、txt、psd 这类不参与图像处理的文件，原样复制到输出目录并一起参与编号：不解码、不重编码，扩展名和文件属性（修改时间等）全保留。\n\n这样输出目录就是源目录的完整镜像，编号也不会因为跳过而错位。':
        'Files that are not images (mp4, txt, psd and so on) are copied to the output folder as-is and numbered along with everything else: no decoding, no re-encoding, extension and file attributes (modified time and the rest) all preserved.\n\nThe output folder is then an exact mirror of the source, and the numbering has no gaps where files were skipped.',
    '「自动」= 先算这一批最大编号需要几位，再多留一位。\n1234 个文件 → 00001~01234。多留的一位是给以后往同一目录追加文件用的，否则超过 9999 之后 9999 和 10000 混排会乱。':
        '"Auto" works out how many digits the largest number in this batch needs, then keeps one more in reserve.\n1234 files becomes 00001-01234. The spare digit is for files you add to the same folder later; without it, once you pass 9999 the names 9999 and 10000 sort out of order.',
    '一张图超过这个时间还没处理完，就放弃处理、按命名规则原样复制一份 ——\n文件在、编号也对，不会因为一两张卡住把整批拖住。\n\n一张 3650x3650 走完整条流水线实测 0.14 秒，撞到门槛的基本都是卡在磁盘、杀毒软件或系统换页上，等下去也不会变快。':
        'If an image is still unfinished after this long, it is left unprocessed and simply copied under its new name --\nthe file is there and the numbering is right, so one or two stuck images cannot hold up the whole batch.\n\nA 3650x3650 image measured 0.14s through the full pipeline; anything that hits this limit is almost always stuck on the disk, on antivirus or on system paging, and waiting will not make it faster.',
    '同时在解码的图片总量上限。超过就排队等，不会硬挤。\n一张 8000x8000 约 610 MB；176 帧的动图约 1 GB。':
        'Upper bound on the total size of the images being decoded at the same time. Over the limit, work waits in line rather than being forced through.\nOne 8000x8000 image is about 610 MB; a 176-frame animation about 1 GB.',
    '开启：所有输出先写临时文件，全部完成后才统一落盘。中途取消能完整撤回，原目录一点不变；代价是处理期间临时占用约一倍磁盘空间。\n\n关闭：直接写目标文件，省磁盘，但中途取消只能停在当前进度。':
        'On: every output is written to a temporary file first and only committed once the whole batch is done. Cancelling halfway rolls back completely and the source folder is untouched; the cost is roughly twice the disk space while it runs.\n\nOff: writes straight to the target files -- saves disk, but cancelling halfway leaves you stopped at whatever progress was reached.',
    '极致保真 100% —— 几乎不掉画质，文件最大\n通用高清 95% —— 肉眼基本看不出差别，日常够用\n网络压缩 85% —— 体积明显变小，适合发网上\n\nPNG 在 85% 下会做色彩量化（有损）；BMP 没有画质参数；TIFF 只在 85% 时换成 LZW 无损压缩。':
        'Maximum quality 100% -- almost no loss of quality, largest files\nHigh quality 95% -- no visible difference in practice, fine for everyday use\nWeb-optimized 85% -- clearly smaller, good for posting online\n\nAt 85% PNG goes through colour quantization (lossy); BMP has no quality setting at all; TIFF switches to lossless LZW compression only at 85%.',
    '超过这个尺寸的图会等比缩小，**只缩不放**，小图原样不动。\n\n这是所有压缩手段里省得最多的一招：4000x3000 存 JPEG q90 实测1377 KB，限到 1920 只剩 193 KB（0.14 倍）。而且省的是看不到的像素，不像降画质那样啃细节。\n\n会真的改变像素尺寸，所以默认不限；一键按钮也一律不缩。':
        'Images larger than this are scaled down proportionally -- **shrink only, never enlarge**; smaller images are left alone.\n\nThis saves more than any other compression setting: 4000x3000 saved as JPEG q90 measured 1377 KB, capped at 1920 it is only 193 KB (0.14x). And what it throws away are pixels you could not see anyway, unlike lowering quality, which eats into detail.\n\nIt genuinely changes the pixel dimensions, so it is unlimited by default; the one-click buttons never scale either.',
    '默认关闭：动图（多帧 WebP/GIF、多页 TIFF）在不旋转时原样复制，帧、帧延时和文件属性全保留。\n\n开启后按画质设置整段重编码 —— 慢得多、内存吃得多，而且有损源再编一遍往往比原来还大（实测涨到 142%）。选了旋转时无论开关如何都必须重编码。':
        'Off by default: animations (multi-frame WebP/GIF, multi-page TIFF) are copied as-is when no rotation is set -- frames, frame delays and file attributes all preserved.\n\nTurned on, the whole thing is re-encoded at the chosen quality -- much slower, much more memory, and re-encoding an already-lossy source often comes out larger than the original (measured at 142%). If a rotation is selected, re-encoding happens either way.',
    '选好文件夹，点一个就行 —— 结果输出到源文件夹旁边的「xxx_已处理」，原文件不动。':
        'Pick a folder, click one button -- the results go next to the source folder and the originals are not touched.',
})

MSG += [
    ('图片处理工作台 V4ce', 'Image Workbench V4ce'),
    ('🌐 界面语言已切换', '\\U0001f310 Interface language switched'),
    ('选择来源文件夹', 'Choose source folder'),
    ('拖拽设置导出位置', 'Set output folder by drag and drop'),
    ('点击一键按钮', 'One-click button'),
    ('切换界面语言', 'Switch interface language'),
    ('开始执行', 'Start'),
    ('取消', 'Cancel'),
    ('关闭窗口', 'Close window'),
    ('使用{}模式', 'Using {0} mode'),
    ('多进程', 'multi-process'),
    ('多线程', 'multi-threaded'),
    ('{} | {} | 来源 {}', '{0} | {1} | source {2}'),
    ('导出目录逃逸 | 来源 {} | 导出 {}', 'Output folder escape | source {0} | output {1}'),
    ('[警告] {}: {} ({}:{})', '[WARNING] {0}: {1} ({2}:{3})'),
    ('可执行文件 {}', 'Executable {0}'),
    ('日志文件 {}', 'Log file {0}'),
]

# ---- 第四批：启动入口（融合_v4ce.py）的几句 ------------------------------
MSG += [
    ('ℹ️ 检测到已经有一个窗口在运行。两个窗口各跑各的，'
     '但如果它们处理同一批文件会互相覆盖。',
     'ℹ️ Another window is already running. The two work '
     'independently, but if they process the same files they will overwrite '
     'each other.'),
    ('图片处理工作台 V4ce（另一个窗口也开着 · {}）',
     'Image Workbench V4ce (another window is open too - {0})'),
    ('缺少依赖', 'Missing dependencies'),
    ('缺少以下库:', 'The following libraries are missing:'),
    ('请先安装:', 'Install them first with:'),
    ('拖拽', 'drag-and-drop'),
]

# ---- 第五批：预设按钮、启动 banner、操作日志 ------------------------------
# 预设按钮的文字来自 presets.py 的字典（`preset['label']`），下标表达式
# 抽不出来，所以前几批全漏了 —— 而这几个按钮就是简易模式的主界面。
UI.update({
    '📦 一键压缩': '\U0001f4e6 Compress',
    '大幅缩小体积，文件名不变': 'Much smaller files, names unchanged',
    '🔄 一键转正': '\U0001f504 Auto-rotate',
    '把躺倒的手机照片摆正（最高画质重存，非无损）':
        'Straighten sideways phone photos (re-saved at maximum quality, '
        'not lossless)',
    '🔢 一键编号': '\U0001f522 Number',
    '按顺序改名 001、002…，不重新编码':
        'Rename in order 001, 002 ... without re-encoding',
    '↻ 顺时针 90°': '\u21bb Rotate right 90\u00b0',
    '整批向右转，文件名不变': 'Turn the whole batch right, names unchanged',
    '↺ 逆时针 90°': '\u21ba Rotate left 90\u00b0',
    '整批向左转，文件名不变': 'Turn the whole batch left, names unchanged',
    '🎨 转成图标': '\U0001f3a8 To icon',
    '做成多尺寸 ICO（256~16）': 'Build a multi-size ICO (256 down to 16)',
    'PNG 会被压成 256 色（有损，可能出现色带）；\n'
    '已经压过的 JPEG 若压不动，会自动保留原文件。':
        'PNG is reduced to 256 colours (lossy; banding is possible).\n'
        'A JPEG that will not get any smaller keeps its original file.',
    '输出到源文件夹旁边的「xxx_已处理」，原文件不动。':
        'Output goes next to the source folder; the originals are untouched.',
    '未安装': 'not installed',
    '[操作]': '[action]',
})

MSG += [
    # 启动 banner。原文是 f'启动 {APP_NAME}' + ('（打包版）' if ... else ...)，
    # 三元表达式拼的，所以抽取器一开始没抓到。
    ('启动 {}（打包版）', 'Starting {0} (packaged build)'),
    ('启动 {}（源码运行）', 'Starting {0} (running from source)'),
    # 单张太慢时的提示
    ('🐌 {} 用了 {} 秒，其中在工人里 {} 秒（{}）',
     '\U0001f40c {0} took {1}s, {2}s of that inside a worker ({3})'),
    ('🐌 {} 用了 {} 秒，其中在工人里 {} 秒',
     '\U0001f40c {0} took {1}s, {2}s of that inside a worker'),
    # 预设按钮的提示语和日志是拼出来的，逐段/逐行翻之后还要这两条收尾
    ('{} —— {}', '{0} — {1}'),
    ('▶ {} —— {}', '\u25b6 {0} — {1}'),
    ('⚠️ {}', '\u26a0\ufe0f {0}'),
]

# ---- 第六批：体检脚本修好后挖出来的几条 ----------------------------------
# 抽取器原来遇到三元表达式就整句放弃，这几条一直没被收集到。
MSG += [
    ('✅ 完成！共 {} 张，耗时 {} 分钟', '✅ Done — {0} images in {1} minutes'),
    ('⏹ 已终止 {} 个工作进程', '⏹ Terminated {0} worker processes'),
    ('已终止 {} 个工作进程', 'Terminated {0} worker processes'),
    ('已终止 {} 个工作进程，正在撤回临时文件...',
     'Terminated {0} worker processes; rolling back temporary files...'),
    ('池子里已经没有活着的工作进程了',
     'No worker processes were still alive in the pool'),
    ('完整调用栈已写入日志:', 'The full traceback was written to the log:'),
    ('完整调用栈见日志: {}', 'Full traceback in the log: {0}'),
    # on_close 记的当时状态
    ('处理中 {}/{}', 'processing {0}/{1}'),
    ('空闲', 'idle'),
    ('暂停', 'Pause'),
    ('继续', 'Resume'),
]

# ---- 第七批：2026-10-04 新增的提示 ------------------------
UI.update({
    '名字填得不对': 'That name will not work',
    '拒绝执行': 'Refused to start',
    '拒绝执行：输出目录落回来源':
        'Refused to start: the output folder resolves back into the source',
    '清空导出目录时保护了来源': 'Protected the source while clearing output',
    '前缀': 'Prefix',
    '目录后缀': 'Folder suffix',
    '导出目录后缀': 'Output folder suffix',
})

MSG += [
    # 清空导出目录时跳过了来源
    ('⚠️ 跳过 {} 项：它们就是来源、或者装着来源，删了等于删掉你的原图',
     '⚠️ Skipped {0} item(s): they are the source, or contain it — '
     'deleting them would delete your originals'),
    ('保留 {}', 'kept {0}'),
    # 界面回调异常
    ('[界面回调异常] {}: {}', '[UI callback error] {0}: {1}'),
    ('⚠️ 界面出错：{}: {}（完整调用栈已写进日志）',
     '⚠️ Interface error: {0}: {1} '
     '(the full traceback was written to the log)'),
    # 提交阶段的新错误（tasks.commit_staged）
    ('目标 {} 是另一张没能提交的原文件，已跳过以免覆盖它',
     'The target {0} is another original that could not be committed; '
     'skipped so it is not overwritten'),
    ('ℹ️ 已跳过 {} 个本程序自己的临时文件或备份文件',
     'ℹ️ Skipped {0} temporary or backup files created by this program'),
    # 开工前检查残留（app.process_worker）
    ('🧹 开工前清理了 {} 个以前留下的临时文件',
     '🧹 Cleaned up {0} temporary files left over from earlier runs'),
    ('⚠️ 发现 {} 个备份文件（名字里带 .imgwb-backup-）。它们是**原文件**：'
     '上次处理在最后一步被打断时留下的，请确认后手工改回原名。例如 {}',
     '⚠️ Found {0} backup files (names containing .imgwb-backup-). They are '
     'your **originals**, left behind when an earlier run was interrupted in '
     'its last step; please check them and rename them back by hand. '
     'For example {1}'),
    ('ℹ️ 发现 {} 个来历不明的临时文件（名字里带 .~imgwb）：'
     '可能是另一台电脑正在处理，也可能是很久以前留下的。'
     '程序不会自动删除它们。例如 {}',
     'ℹ️ Found {0} temporary files of unknown origin (names containing '
     '.~imgwb): another computer may be working here, or they may be very '
     'old. They are never deleted automatically. For example {1}'),
    ('开工前检查残留', 'Leftover check before start'),
    ('清理 {} 个，备份 {} 个，来历不明 {} 个',
     'cleaned {0}, backups {1}, unknown {2}'),
    # 关窗口时提交没做完（app._await_worker_exit）
    ('关闭窗口时提交还没做完，等了 {} 秒后放弃等待。'
     '没有清扫临时文件：原文件可能留在 {} 开头的备份名下，'
     '产物可能还是 .~imgwb 临时文件，都在这些目录里: {}',
     'The window was closed while results were still being committed; gave up '
     'waiting after {0} s. Temporary files were NOT swept: originals may be '
     'left under backup names starting with {1}, and results may still be '
     '.~imgwb temporary files, all in these folders: {2}'),
    # 输出目录落回来源（tasks.output_lands_in_source）
    ('导出目录算下来正好是来源文件夹本身（{}）—— 产物会落回扫描范围，'
     '再跑一次就被当成新图重新处理，文件每跑一次翻一倍。'
     '给「导出目录后缀」填个名字，或者换一个导出位置。',
     'The output folder works out to be the source folder itself ({0}) - '
     'the results would land back in the scan range and be processed again '
     'as new images on the next run, doubling the files every time. '
     'Give "Output folder suffix" a name, or pick another export location.'),
    ('导出目录算下来落在来源文件夹里面（{}）—— 产物会落回扫描范围，'
     '文件每跑一次翻一倍。换一个导出位置。',
     'The output folder works out to be inside the source folder ({0}) - '
     'the results would land back in the scan range, doubling the files '
     'every run. Pick another export location.'),
    ('目标 {} 是另一张没能提交的原文件，已跳过以免覆盖它；原文件保留在 {}',
     'The target {0} is another original that could not be committed; '
     'skipped so it is not overwritten; the original is kept at {1}'),
    ('目标 {} 是另一张没能提交的原文件，已跳过以免覆盖它；原文件没能放回原位',
     'The target {0} is another original that could not be committed; '
     'skipped so it is not overwritten; the original could not be put back'),
    ('文件已处理好，但没能改成新名字 {}: {}',
     'The file was processed, but could not be renamed to {0}: {1}'),
    ('两个文件被排到了同一个输出位置，已中止以免互相覆盖：{} 和 {} 都要写到 {}',
     'Two files were assigned the same output location; aborted so they do '
     'not overwrite each other: {0} and {1} would both be written to {2}'),
    ('原文件挪不开，已跳过不提交: {}',
     'Could not move the original out of the way; skipped: {0}'),
    ('写入失败: {}（原文件已还原）',
     'Write failed: {0} (the original was restored)'),
    ('写入失败: {}；原文件保留在 {}',
     'Write failed: {0}; the original is kept at {1}'),
    ('产物路径逃出了它该在的目录，已中止以免覆盖别处的文件：{}（应在 {} 下）',
     'The output path escaped the folder it belongs in; aborted so nothing '
     'elsewhere is overwritten: {0} (should be under {1})'),
    # 名字片段校验（utils.name_segment_error）
    ('{}前后不能有空格', '{0} cannot start or end with a space'),
    ('{}不能包含这些字符：{}（它是一个名字，不是路径）',
     '{0} cannot contain these characters: {1} (it is a name, not a path)'),
    ('{}不能是绝对路径', '{0} cannot be an absolute path'),
    ('{}不能是 . 或 ..', '{0} cannot be . or ..'),
    ('{}「{}」是 Windows 的保留设备名，换一个',
     '{0} "{1}" is a reserved Windows device name — pick another'),
    ('{}不能以句点结尾（Windows 会把它悄悄去掉）',
     '{0} cannot end with a period (Windows silently drops it)'),
]

# ---------------------------------------------------------------- 弹窗
DIALOG = {
    '完成': 'Done',
    '错误': 'Error',
    '处理出错': 'Processing error',
    '已取消': 'Cancelled',
    '无错误': 'No errors',
    '没有处理失败的文件': 'No files failed',
    '未找到图片文件': 'No image files found',
    '格式不支持': 'Unsupported format',
    '拖拽的文件中没有支持的图片格式':
        'None of the dropped files are in a supported image format',
    '请选择导出位置': 'Please choose an output location',
    '请选择有效的源文件夹或拖拽图片到窗口。要一次处理多个文件夹，可以用分号隔开多条路径。':
        'Please choose a valid source folder, or drop images onto the window. '
        'To process several folders at once, separate the paths with semicolons.',
    '起始编号必须是数字': 'The starting number must be a number',
    '导出位置不对': 'Invalid output location',
    '仍在处理中': 'Still processing',
    '还有任务正在处理。\n关闭窗口会取消本次处理，确定吗？':
        'There is still work in progress.\nClosing the window will cancel this '
        'run. Are you sure?',
    '强制停止': 'Force stop',
    '立刻终止全部工作进程。\n安全写入开启时会完整撤回，否则已写出的文件会保留。\n\n确定吗？':
        'This terminates every worker process immediately.\nWith Safe write on '
        'everything is rolled back; otherwise files already written are kept.'
        '\n\nAre you sure?',
    '拒绝执行': 'Refused to run',
}


# ---------------------------------------------------------------- 引擎
def _build_patterns():
    """把中文模板编成正则。按字面长度倒序 —— 长的先试，免得短模板抢匹配。"""
    out = []
    for zh, en in MSG:
        if '{}' not in zh:
            continue
        parts = zh.split('{}')
        # **占位符不许跨过竖线和分号。** 这两个都是分隔符：`|` 隔开日志的
        # 字段，`；` 隔开拼起来的几截话（失败汇总就是 `2 个「A」；1 个「B」`）。
        # （`[操作] 开始执行 | 120 张`）。用 `(.*?)` 的话，`'{} {} 张'` 这种
        # 短模板会一口气跨过竖线把整行吃掉，翻出 `[操作] ×开始执行 | 120`
        # —— 比漏翻更糟：用户会以为日志坏了。
        rx = '^' + '([^|；]*?)'.join(re.escape(p) for p in parts) + '$'
        literal = sum(len(p) for p in parts)
        out.append((literal, re.compile(rx, re.S), en))
    out.sort(key=lambda x: -x[0])
    return out


_PATTERNS = _build_patterns()
_EXACT = {}
for _d in (OPTIONS, UI, DIALOG):
    _EXACT.update(_d)
# 有些词条在界面上是**带 ⚠️ 前缀**拼进去的（预设按钮提示语里的那条警告）。
# 整段去套 `'⚠️ {}'` 这个模板是匹配不上的 —— 正文里带分号，而占位符不许跨分号
# （见 _build_patterns 的护栏）。所以把带前缀的写法也登记成精确词条，
# 让它在精确表里一次命中。多出来的这一百来条字典项不值一提。
for _zh, _en in list(_EXACT.items()):
    _EXACT.setdefault('⚠️ ' + _zh, '⚠️ ' + _en)
for _zh, _en in MSG:
    if '{}' not in _zh:
        _EXACT[_zh] = _en
_REVERSE = {v: k for k, v in OPTIONS.items()}


def available():
    return ('zh', 'en')


def get_lang():
    return _lang


# 界面里「导出文件夹后缀」的默认值。它是会落到磁盘上的文件夹名字，不是显示
# 文字 —— 英文界面的用户拿到一个中文名的输出文件夹会摸不着头脑，所以默认值
# 跟着界面语言走。只管界面的默认值：命令行和 api 的默认值不随语言变
# （脚本要的是确定的结果），用户自己填过的后缀也不动。
DIR_SUFFIX = {'zh': '_已处理', 'en': '_processed'}


def default_dir_suffix(lang=None):
    """`lang`（不传就是当前语言）界面下，导出文件夹后缀的默认值。"""
    return DIR_SUFFIX.get(lang or _lang, DIR_SUFFIX['zh'])


def set_lang(lang):
    """切换语言。只影响显示，不影响任何内部值。"""
    global _lang
    if lang not in available():
        return False
    if lang != _lang:
        _lang = lang
        _cache.clear()
    return True


_CJK = re.compile(r'[\u4e00-\u9fff]')


def _piece(s):
    """翻译一个被拼进大句子里的片段。直接交给 tr() —— 回退链在那里。"""
    return tr(s)


def tr(text):
    """翻译一句给用户看的话。**翻不了就原样返回中文 —— 绝不返回空。**

    怎么找译文，一级一级来（前一级没命中才试下一级）：

    1. **精确表**：整句一模一样地查字典。静态控件文字、提示语走这条。
    2. **模板**：`'找到 {} 个文件'` 这类带变量的句子，用正则把变量抠出来，
       套到英文模板上。抠出来的片段如果还有中文（错误原因是拼进
       `✗ 文件名 — 原因` 里的），递归再翻一遍；文件名和数字不含中文，
       不会被误伤。
    3. **拆开再翻**：整句匹配不上，但很可能是拼出来的。按空行、换行、分号
       从粗到细拆，每截单独翻。

    另外两件小事：

    * **两侧的空格要摘掉再翻、翻完贴回去**。Labelframe 的标题写成
      `text=' 处理选项 '`（靠空格留白），日志里又有 `'   ✗ ...'` 这种缩进 ——
      把空格算进模板，同一句话就得存好几个版本，必然漏。
    * 翻过的结果缓存起来：同一句日志一轮里会出现成百上千次。
    """
    if _lang == 'zh' or not text:
        return text
    hit = _cache.get(text)
    if hit is not None:
        return hit

    # 两侧的空格都要摘掉再翻、翻完贴回去。Labelframe 的标题写成
    # `text=' 处理选项 '`（靠空格留白），日志里又有 `'   ✗ ...'` 这种缩进 ——
    # 把空格算进模板，同一句话就得存好几个版本，必然漏。
    body = text.strip(' ')
    if not body:
        return text
    indent = text[:len(text) - len(text.lstrip(' '))]
    trail = text[len(text.rstrip(' ')):]

    out = _EXACT.get(body)
    if out is None:
        for _literal, rx, en in _PATTERNS:
            m = rx.match(body)
            if m:
                groups = [_piece(g) if _CJK.search(g) else g
                          for g in m.groups()]
                try:
                    out = en.format(*groups)
                except (IndexError, KeyError):
                    out = None
                break
    if out is None:
        # 整句匹配不上，但很可能是**拼出来的**。按分隔符从粗到细拆开，
        # 每截单独去翻，有一截翻动了就算这一级成功。
        #
        #   '\n\n' 预设按钮的提示语：`标题 —— 说明` + `⚠️ 警告` + 固定结尾
        #   '\n'   多行提示语里逐行写的要点
        #   '；'    失败汇总：`2 个「这不是图片」；1 个「文件是空的」`
        #
        # **顺序必须是从粗到细**：有些完整词条本身就带换行或分号
        # （预设那条两行的 ⚠️ 警告两样都有），先拆细了就再也匹配不上它。
        # 整句先试、再一级一级拆，粗的那一级命中就不会继续拆。
        for _sep, _join in (('\n\n', '\n\n'), ('\n', '\n'), ('；', '; ')):
            if _sep not in body:
                continue
            pieces = [tr(p) if p.strip() else p for p in body.split(_sep)]
            joined = _join.join(pieces)
            if joined != body:
                out = joined
                break
    if out is None:
        out = body                      # 没覆盖到：原样留中文，不影响功能
    out = indent + out + trail
    _cache[text] = out
    return out


def canon(value):
    """把下拉框里显示的选项还原成中文内部值。

    **所有读取下拉框的地方都要先过这一道**，否则英文界面下
    `rotate_mode == '不旋转'` 之类的判断会全部落空。
    """
    if not value:
        return value
    return _REVERSE.get(value, value)


def options(zh_list):
    """把一串中文选项变成当前语言的显示列表（顺序不变）。"""
    if _lang == 'zh':
        return list(zh_list)
    return [OPTIONS.get(x, x) for x in zh_list]



def join(parts, zh_sep='，'):
    """把几段中文拼成一句。英文模式下逐段翻译，再用英文的逗号连起来。

    识别摘要那几行是拼出来的（"最多重新编码 N 张，原样复制 M 张，..."）。
    整句去套模板是套不上的 —— 组合是动态的。所以在拼装处就逐段翻好。
    """
    parts = [p for p in parts if p]
    if _lang == 'zh':
        return zh_sep.join(parts)
    return ', '.join(tr(p) for p in parts)

def display(zh_value):
    """单个选项值 -> 当前语言的显示文字。"""
    if _lang == 'zh':
        return zh_value
    return OPTIONS.get(zh_value, zh_value)


# ---------------------------------------------------------------- 偏好存取
def _pref_paths():
    """语言偏好可能放的位置，按优先级排。跟日志同一套思路（见 logfile.py）：

    1. **exe 旁边**（或源码树根）—— 便携版整个文件夹拷走，设置跟着走。
    2. `%LOCALAPPDATA%\\imgwb\\` —— 第 1 个写不进去时的退路，比如程序放在
       Program Files 或只读介质上。

    **不用 `os.access(base, os.W_OK)` 来预判能不能写** —— logfile.py 里已经为
    这件事写过教训：Windows 上 Program Files 的写入虚拟化、只读介质、权限限制，
    `os.access` 一律报「能写」，真写才知道不行。所以这里只列候选，
    能不能用交给实际的读写去决定。
    """
    out = []
    try:
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out.append(os.path.join(base, 'lang.txt'))
    except Exception:
        pass
    local = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    out.append(os.path.join(local, 'imgwb', 'lang.txt'))
    return out


def _pref_path():
    """主位置。真正读写请用 load_pref / save_pref，它们会把退路一起试。"""
    return _pref_paths()[0]


def load_pref():
    """读上次选的语言；没存过返回 None。"""
    for p in _pref_paths():
        try:
            with open(p, encoding='utf-8') as fh:
                v = fh.read().strip()
        except OSError:
            continue
        if v in available():
            return v
    return None


def save_pref(lang):
    """存下用户选的语言。主位置写不进去就退到 LOCALAPPDATA。

    存不下来也只是「下次打开没记住」，不该因此报错打断用户，所以静默返回 False。
    """
    for p in _pref_paths():
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write(lang)
            return True
        except OSError:
            continue
    return False


def system_default():
    """按系统语言猜一个默认值。**只在用户从没选过时用。**

    不用 `locale.getdefaultlocale()` —— 它在 3.11 起已弃用，而本程序把
    DeprecationWarning 也写进日志（见 logfile.install_hooks），结果是每次启动
    都往用户的日志里塞一条没用的告警。日志得保持干净，不然真出问题时没人看。

    所以优先问 Windows 要「用户界面语言」：这正好是该用哪种界面语言的依据，
    比区域设置（它管的是日期和数字格式）更贴题。拿不到再退回 locale。
    """
    try:
        import ctypes
        # 低 10 位是主语言 ID，0x04 = LANG_CHINESE（简繁都算中文）
        if (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x04:
            return 'zh'
        return 'en'
    except Exception:
        pass
    try:
        import locale
        code = (locale.getlocale()[0] or '').lower()
    except Exception:
        code = ''
    # Windows 上 getlocale() 返回的是 'Chinese (Simplified)_China' 这种写法
    return 'zh' if code.startswith('zh') or 'chinese' in code else 'en'
