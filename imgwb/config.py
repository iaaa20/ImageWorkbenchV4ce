"""常量、可选依赖探测，以及在进程间传递的数据结构。

这个模块不能依赖任何 GUI 库 —— pipeline 会被子进程加载，越轻越好。
"""

# ============================================================
# 【导读】config.py —— 常量和「数据格式」。
# 常量：支持的扩展名、超时档位、最长边档位等。
# 数据格式：ProcessConfig（一次处理的设置）、Task（一个待办）、
#           FileInfo（识别结果）、TaskResult（处理结果）。
# ============================================================
import sys
from dataclasses import dataclass
from typing import List, NamedTuple, Optional

IS_WINDOWS = sys.platform == 'win32'

# HEIC 是可选支持：装了 pillow-heif 才认这两个扩展名
HEIC_SUPPORTED = False
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    pass

# .gif 是**输入格式**，别再漏掉它。动图写回的那套（duration / loop / disposal）
# 一直是齐的，但 GIF 不在这个列表里 —— 于是用户勾了「动图也重编码」拖进一个
# GIF，它照样被当成「不支持的格式」原样带走，界面还说「已忽略」。
VALID_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tiff', '.tif',
                    '.gif')
if HEIC_SUPPORTED:
    VALID_EXTENSIONS = VALID_EXTENSIONS + ('.heic', '.heif')

# 子进程要把依赖重新 import 一遍，阈值太低会让刚过线的小批量反而更慢
PROCESS_THRESHOLD = 200
# ICO 目录项用单字节存宽高，256 是格式硬上限
ICO_MAX_SIZE = 256
# 多久没有任务完成就在日志里报一次「卡在哪」
STALL_REPORT_SEC = 20
# 不管有没有任务完成，最多隔这么久也要报一次进度。原来的进度日志是「每完成
# 5% 记一条」，几千张的批量里一条要等 130 张 —— 一旦慢下来就是几分钟一片空白，
# 用户只能盯着不动的进度条猜。
PROGRESS_HEARTBEAT_SEC = 20
# 「单张超时」的固定档位。一张 3650x3650 走完整条流水线实测 0.14 秒，所以
# 超过一分钟的任务一定是出了别的问题（卡在 I/O、被杀毒软件拦、系统在换页）。
# 与其陪着它耗下去，不如放弃处理、原样复制一份带走 —— 至少文件在、编号对。
TIMEOUT_CHOICES = [
    ('关闭 (一直等)', 0),
    ('30 秒', 30),
    ('60 秒', 60),
    ('120 秒 (默认)', 120),
    ('300 秒', 300),
]
DEFAULT_TIMEOUT_LABEL = '120 秒 (默认)'

# 「最长边不超过」的档位。缩尺寸是所有压缩手段里收益最大的一招 ——
# 实测 4000x3000 存 JPEG q90：
#     原尺寸 1377K | 长边 2560 -> 0.29x | 1920 -> 0.14x | 1280 -> 0.06x
# 省 70~94%，比任何编码器调参都狠一个量级，而且省的是"看不到的像素"，
# 不像降画质那样啃细节。默认不限 —— 它会真的改变像素尺寸，必须用户明确要。
MAX_EDGE_CHOICES = [
    ('不限 (保持原尺寸)', 0),
    ('3840 (4K)', 3840),
    ('2560 (2K)', 2560),
    ('1920 (1080p)', 1920),
    ('1280 (720p)', 1280),
]
DEFAULT_MAX_EDGE_LABEL = '不限 (保持原尺寸)'

# 隔多久抓一次「工作子进程的子进程」。全系统进程快照一次约 7 ms，派发循环
# 每秒能转几十圈，每圈都抓的话光数进程就吃掉近四成处理时间（实测 39%）。
# 子进程只在建池和 max_tasks_per_child 换代时才变，两秒一次绰绰有余。
TREE_SCAN_INTERVAL = 2.0

# 单个任务从提交到拿到结果超过这么久，就把它在工人里的分段耗时打出来。
# 用来区分「真的在算」和「算完了但卡在别处」—— 实测一张 3650x3650 走完
# 整条流水线只要 0.14 秒，所以几十秒的任务一定另有原因。
SLOW_TASK_SEC = 20
UI_UPDATE_INTERVAL = 0.15
LOG_FLUSH_INTERVAL = 0.2


@dataclass
class ProcessConfig:
    """处理配置"""
    rotate_mode: str
    quality_opt: str
    compress_only: bool
    is_ico: bool
    ico_sizes: List[int] = None
    ico_format: str = 'Auto'
    # 不旋转时多帧图默认只复制不重编码（重编码又慢又胀，见 pipeline）。
    # 这个开关是给"我就是要压动图"的人留的后门，默认关。
    reencode_animated: bool = False
    # 最长边上限（像素），0 = 不限。只缩不放。
    max_edge: int = 0


class Task(NamedTuple):
    """一个待处理文件。用 NamedTuple 而不是 tuple：字段多了以后 t[3] 这种
    下标读起来太费劲，而它同时仍然是元组，索引访问和 pickle 都照旧可用。"""
    src: str
    dst: str
    config: 'ProcessConfig'
    stage: bool          # 先写临时文件、最后统一提交
    copy_only: bool      # 不解码，原样复制（不支持的格式走这条）


# 预扫描算「预计耗时」用的吞吐，单位是 **百万像素/秒/工人**。
#
# **按字节估是不准的** —— 同样 100 MB，一批 q100 的小图和一批巨幅照片，
# 耗时能差三四倍。用户那次 7731 MB 估出 62 秒、实际跑了四分钟就是这么来的。
# 编解码的工作量跟像素数走，跟文件大小只是间接相关。
#
# 换成像素之后拿三份真实语料回算（都是并发 8）：
#   小图 JPEG  2510 Mpx / 18.7 秒 -> 16.8   大图 JPEG 5800 Mpx / 34.6 秒 -> 21.0
#   PNG         480 Mpx /  6.3 秒 ->  9.5
# 取值略保守，宁可报得比实际慢一点。
SCAN_RATE_MPXPS = {'JPEG': 18.0, 'MPO': 18.0, 'PNG': 9.0, 'WEBP': 6.0}
DEFAULT_RATE_MPXPS = 10.0
COPY_RATE_MBPS = 200.0          # 只复制不解码，走磁盘，这一档才按字节算


@dataclass
class FileInfo:
    """预扫描认出来的一张图。**只读文件头，不解码像素。**

    开工前先把整批文件认一遍，才谈得上「明确处理范围和时间」：哪些真要重编码、
    哪些原样复制、哪些根本打不开、哪些扩展名和真实格式对不上。
    """
    path: str
    size_bytes: int = 0
    fmt: str = ''                # 真实格式（读文件头，不是看扩展名）
    mode: str = ''
    width: int = 0
    height: int = 0
    frames: int = 1
    est_mb: float = 0.0          # 闸门要用的预估峰值内存
    plan: str = 'process'        # process / copy / carry / error
    reason: str = ''             # 归到这一类的原因，直接说给用户听
    error: str = ''              # 打不开时的错误原文
    # 扩展名和真实格式对不上（真实素材里有 PNG 改名成 .jpg 的）。
    # 后果不只是内存估错：目标按扩展名走，RGBA 的 PNG 会被编成 JPEG，
    # **透明通道静默丢掉**，而界面只报「成功」。
    mislabeled: bool = False
    # 源有透明通道，但按目标扩展名编出来存不下 —— 会丢。
    alpha_loss: bool = False


@dataclass
class TaskResult:
    """任务结果"""
    success: bool
    src_path: str
    dst_path: str
    error_msg: Optional[str] = None
    error_type: Optional[str] = None
    tmp_path: Optional[str] = None
    # 压缩反而变大时，输出被丢弃、原文件原样复制，这里置 True。
    kept_original: bool = False
    # 多帧源被写进存不下多帧的格式时，这里记源本来有几帧（动画没了）。
    frames_dropped: int = 0
    # 因为「最长边不超过」而缩过尺寸。
    resized: bool = False
    # 技术细节：异常类型、完整调用栈、文件头字节等。**不进界面，只进日志文件**
    # —— 用户要看的是「哪个文件、为什么」，查 bug 的人要的是能直接定位的原文。
    # 工作子进程不自己写日志（多个进程抢同一个文件容易写乱），所以细节随结果
    # 带回主进程，由主进程统一落盘。
    detail: Optional[str] = None
    # 工人里的分段耗时（秒）：open / decode / encode / total。
    # 主进程拿它跟「从提交到收到结果」的墙上时间一比，就能分辨慢在哪：
    # 分段之和 ≈ 墙上时间 -> 真的在算；差得远 -> 卡在排队、进程回收或数据传输。
    timings: Optional[dict] = None
