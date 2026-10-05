"""单张图片的处理流水线。

多进程模式下子进程只会加载到这一层，所以这里刻意不 import 任何 GUI 库。
"""

# ============================================================
# 【导读】pipeline.py —— 「一张图怎么处理」。在工作子进程里运行。
# 核心是 process_single_image（里面按「第 N 步」标了路标）。
# 其他：identify（开工前识别一张图）、probe_peak（估内存）、
#       recompress_pointless（判断再压是否白干）、get_quality_params（画质参数）、save_animated（动图）。
# 下面紧跟着的一大段带表格的常量说明，是反复实测出来的数据 —— 改数字之前先看懂那些表。
# 整体流程见项目根目录的《代码导读.md》。
# ============================================================
import os
import shutil
import struct
import time
import traceback

from PIL import (Image, ImageChops, ImageOps, ImageSequence,
                 UnidentifiedImageError)

from .config import ICO_MAX_SIZE, FileInfo, TaskResult
from .utils import (discard_temp, flush_to_disk, long_path, make_temp_path,
                    same_file)

# 这些目标格式存得下多帧，可以整段动画写回去。其余格式（.jpg/.bmp/.ico）
# 天生只有一帧，多帧源只能取第一帧。
ANIMATED_FORMATS = ('.webp', '.gif', '.png', '.tif', '.tiff')

# 估算内存用的系数。**必须每个用例单独起进程量峰值工作集** —— 峰值是累计的，
# 同一进程里连着量几张，后面全是被前面污染的数字；tracemalloc 也不行，
# 它看不见 Pillow 的 C 缓冲区。
#
# 下面记了两次标定，第一次的表留着是为了说明「为什么不能一个数走天下」。
#
# **第一次：按格式分开**。早期用一个 4.5 走天下，是在小合成图上标的；
# 拿 3 ~ 44.7 MP 的图逐个独立进程量峰值工作集，比值非常稳，但**格式之间差一倍多**：
#
#   格式   3.0MP   12.0MP  27.0MP  44.7MP   倍数
#   JPEG     53MB   210MB   473MB   783MB   4.59
#   PNG      35MB   138MB   310MB   512MB   3.01
#   WebP     91MB   361MB   808MB  1377MB   7.9~8.1
#
# WebP 被低估了 43% —— 闸门按 4.5 放行，实际要 8 倍，几张一起就把内存冲爆；
# PNG 反过来被高估 50%，白占额度让并发上不去。
# 倍数是「峰值工作集 ÷ 宽×高×4 字节」，按**源格式**取（解码那一侧是大头）。
#
# **第二次：系数不只跟格式有关，还跟这次要做什么有关。**
# 上面那版一个格式一个数，结果两头不讨好 —— 常见情况过保守（白占额度、
# 并发上不去、CPU 利用率只有 35%），最坏情况反而不够（会 OOM）。
# 拿真实素材逐个独立进程量峰值工作集（6046x4030 JPEG / 3840x2560 PNG）：
#
#   格式/档位          不旋转   顺时针
#   JPEG 100%           3.81     4.85      <- 旧的 4.6 在这里**不够**
#   JPEG 95%            3.66     4.66      <- 旧的 4.6 高估 26%
#   JPEG 85%            2.83     3.83
#   PNG 100%/95%        2.05     3.04      <- 旧的 3.1 高估 51%
#   PNG 85%（量化）     4.04     5.04      <- 旧的 3.1 **严重不够**
#   WebP 95% / 85%      7.10 / 6.53        <- 旧的 8.1 基本准
#
# 规律很干净：**旋转恰好 +1.0**（多一份完整 RGBA 副本），
# **PNG 的 85% 档因为要做调色板量化再 +2.0**。所以拆成「基线 + 增量」。
# 取值都在实测最大值上留了几个百分点的余量。
# **第三次：分母必须是「解码后真正占多少字节」，不能一律按 RGBA 算。**
# 上一版拿 宽x高x4 当分母，在真实素材上两个方向都翻车（13 个真实样本）：
#
#   样本                     模式    估/实
#   灰度 JPEG（扫描件常见）   L      3.37x / 3.57x   <- 白占 3.5 倍额度
#   PNG 改名成 .jpg          RGBA   0.48x           <- **低估一半，会撑爆**
#
# 灰度图 Pillow 内部只占 1 字节/像素，按 4 算就是白扔 3/4 的闸门额度；
# 而扩展名和真实格式不符时（PNG 改名成 .jpg，真实素材里就有），
# RGBA 要先转 RGB 再交给 JPEG 编码器，比 PNG->PNG 贵得多。
#
# 所以：**分母 = 宽 x 高 x 该模式的内部字节数**，
# **系数 = max(源格式, 目标格式)** —— 峰值由重的那一侧决定。
# 按这个口径重算 13 个真实样本（另加 4 张大图）：
#
# 分母试过两种，**按通道数（bands）比按内部字节数更稳**：
#
#   路径                 按内部字节      按通道数
#   JPEG RGB             3.57 ~ 3.66     4.75 ~ 4.88
#   JPEG L（灰度）       4.38 ~ 4.64     4.38 ~ 4.64
#   PNG(改名) -> JPEG    4.58            4.58
#   -> JPEG 这一档的离散度   1.30x    ->   **1.11x**
#
#   PNG RGB              2.00 ~ 2.06     2.66 ~ 2.74
#   PNG RGBA             2.07 ~ 3.05     2.07 ~ 3.05
#   WebP                 7.10            9.47
#
# JPEG 收得明显更紧，PNG 两种口径差不多，所以统一用通道数。
# 取值 = 实测最大值 + 几个百分点余量。
FORMAT_FACTORS = {'JPEG': 5.4, 'MPO': 5.4, 'PNG': 3.2, 'WEBP': 10.6,
                  'GIF': 3.2}
STILL_FACTOR = 5.4          # 认不出格式时的兜底，按最重的静态图那档给

# 每像素的通道数。**调色板图按 3 算** —— P 模式自己只有 1 个通道，但处理
# 过程中会被转成 RGB/RGBA，按 1 算就会低估。认不出的模式按 4 兜底。
MODE_BANDS = {'1': 1, 'L': 1, 'LA': 2, 'P': 3, 'PA': 4, 'RGB': 3,
              'YCbCr': 3, 'RGBA': 4, 'CMYK': 4, 'I': 4, 'F': 4, 'I;16': 2}

# 扩展名 -> 该格式的系数键，用来取「目标格式」那一侧。
EXT_FORMAT = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG',
              '.webp': 'WEBP', '.gif': 'GIF'}
# 旋转（或把 EXIF 方向烘焙进像素）要多留一份完整副本。按通道数口径实测 +1.38
# （JPEG 100% 档 5.09 -> 6.47），留一成余量。
ROTATE_EXTRA = 1.5
# PNG 的 85% 档会走调色板量化（quantize），额外再多两份。实测 4.04 - 2.05。
PNG_QUANTIZE_EXTRA = 2.2
ANIMATED_FACTOR = 1.3

# **动图写成 WebP / APNG 时，「帧数 × 1.3」是低估的**（2026-10-04 实测）。
# 那个系数只算了"全部帧同时在内存里"这一项，漏了两样：
#
#   * 编码器自己的工作内存 —— 跟**单帧**大小成正比、跟帧数无关。帧少而大的
#     动图上它是大头（1600x1600 x 12 帧的 WebP：旧估算 114 MB，实测 256 MB）。
#   * APNG 的写入器会把每一帧**再存一份**（先攒一张帧表、再统一写），
#     所以每帧的占用是两份而不是一份。
#
# 每个用例一个独立子进程，量「开始处理前的工作集 → 峰值工作集」的净增
# （旋转 180°，整条 process_single_image）：
#
#     用例                    旧估算    实测     旧估/实测
#     WebP  600x600  x40 RGB     54      85      0.63
#     WebP 1200x1200 x30 RGB    161     244      0.66
#     WebP 1600x1600 x12 RGB    114     256      0.45
#     WebP  800x800  x60 RGBA   190     199      0.95
#     APNG  600x600  x40 RGB     54     119      0.45
#     APNG 1200x1200 x30 RGB    161     363      0.44
#     APNG 1600x1600 x12 RGB    114     294      0.39
#     APNG  800x800  x60 RGBA   190     309      0.62
#
# 低估到 0.4 倍意味着闸门会放进来两倍半的量。把上面的数按
# 「帧数 × 输出像素 × a + 源像素 × b」解出来（单位：字节/像素）：
# WebP a≈4.4 b≈53，APNG a≈8.0 b≈24 —— **跟源是 RGB 还是 RGBA 基本无关**
# （两种编码器内部都按 4 字节/像素存帧），所以这里按像素数算，不乘通道数。
# 取值 = 解出来的数 + 一成左右余量。GIF 是真流式，旧公式本来就偏保守，不动。
#
# 这组数是合成图量的 —— 峰值内存取决于尺寸、帧数和像素格式，和画面内容
# 关系不大（跟"压缩收益"那类必须用真实素材标的阈值不是一回事）。
# 回归在 tests/test_probe.py：实测值是地板，估算不许低于它。
ANIMATED_COST = {'WEBP': (5.0, 60.0), 'PNG': (8.8, 28.0)}

# 跳过判据的门槛。**这一版是拿真实素材标的**（用户本机某真实素材目录下的 900 个样本：
# 2333 张 jpg / 310 张 png / 219 张 webp，中位长边 800px、中位画质 q80），
# 上一版用算法生成的合成图标，结论在真实图上是错的 —— 合成图纹理太规则，
# 重编码的表现和真实照片/插画差很远。**别再用合成图标这些数。**
#
# 标定方法：对每个文件既跑判据、又真的编一遍当标准答案，统计两类错误 ——
# 「误跳过」（跳了但其实能省 ≥5%）和「白干」（没跳但编完被护栏丢弃）。
#
# JPEG（740 样本，95% 档下 17% 真能省）：
#   阈值=目标(95)     跳 628，误跳过 13  <- 上一版，太激进
#   阈值=目标-5(90)   跳 572，误跳过  4  <- 现在这版，几乎不少跳但误伤降到三分之一
#   阈值=80           跳 456，误跳过  3
#
# WebP（53 样本）：
#   95% 档一律跳过    跳 53，误跳过 6（漏掉 120% 体积）  <- 上一版，错得最狠
#   95% 档 bpp<=1.5   跳 37，误跳过 2
#   85% 档 bpp<=1.0   跳 27，误跳过 5   <- 上一版
#   85% 档 bpp<=0.8   跳 22，误跳过 4   <- 现在这版
#
# PNG 试过用同样的办法判断"要不要无损重打包"，**没做成，别再走 bpp 这条路**：
#   内容   存档 level   bpp     重打包(6)之后
#   噪点   6            8.33    1.00x  白干
#   噪点   1           12.93    0.64x  真收益
#   纯色   6            0.04    1.00x  白干
#   纯色   1            0.25    0.18x  真收益
# bpp 0.25 有收益而 8.33 没有 —— 收益取决于源用了什么压缩级别，bpp 主要反映
# 内容复杂度，两者正交，单一门槛必然误判。而 PNG 重打包只要 0.02~0.06 秒、
# 真收益能到 0.35x，值得赌。所以 PNG 照常处理，压不小时由体积护栏兜底。
# **只在目标画质 >=95 的档位上跳 WebP。**
# 85% 档试过按 bpp 跳，量下来站不住：真实样本里 44 张有损 WebP，按 85% 重编码
# 有 23 张真能省 ≥5%（实测压到 0.32~0.50 倍），而 bpp 根本预测不了谁能省 ——
# bpp 0.79 那张压到了 0.47，比 bpp 3.12 那张（0.34）只差一点。
# 阈值取 0.5 / 0.8 / 1.0 分别误跳过 3 / 4 / 5 张，怎么调都在误伤。
# 结论：q85 本身就够狠，几乎总能压小，这一档不该跳，交给体积护栏兜底。
WEBP_BPP_HIGH_TARGET = 1.5
# JPEG 判据留的余量。源画质正好等于目标时重编码往往还能省一点
# （元数据、缩略图、不同编码器的表），所以要低于目标 5 个点才跳。
JPEG_QUALITY_MARGIN = 5

ROTATIONS = {
    '顺时针': Image.Transpose.ROTATE_270,
    '逆时针': Image.Transpose.ROTATE_90,
    '180': Image.Transpose.ROTATE_180,
}


def oversized(size, max_edge: int) -> bool:
    """这张图的最长边超了没有。max_edge=0 表示不限。"""
    return bool(max_edge) and max(size) > max_edge


def shrink_to(im: Image.Image, max_edge: int) -> Image.Image:
    """按最长边等比缩小。**只缩不放** —— 小图原样返回，不做无谓的放大。"""
    if not oversized(im.size, max_edge):
        return im
    w, h = im.size
    scale = max_edge / float(max(w, h))
    # 至少留 1 像素，别把细长条缩成 0 宽
    target = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    return im.resize(target, Image.Resampling.LANCZOS)


def rotate_frame(im: Image.Image, rot: str) -> Image.Image:
    """按用户选的方向转一帧。不旋转时原样返回。"""
    for key, method in ROTATIONS.items():
        if key in rot:
            return im.transpose(method)
    return im


def save_animated(img: Image.Image, out_path: str, dst_ext: str,
                  config, frame_count: int):
    """逐帧处理并整体写回，保住动画。

    帧是以生成器交给 Pillow 的，但**能不能真的流式取决于目标格式**：

    * **GIF / TIFF** 的编码器只遍历一遍，是真流式，内存只占一帧。
    * **WebP 不是。** ``WebPImagePlugin`` 里有一句
      ``append_images = list(...)``，会把生成器整个物化成列表，全部帧同时在
      内存里。所以峰值内存是「单帧 × 帧数」量级，估算见 ``estimate_peak_mb``。
    * **PNG（APNG）更糟：给生成器会静默只写下第一帧。**
      ``PngImagePlugin`` 要遍历两遍 —— 先数一遍算出 ``num_frames`` 写进
      acTL 块，再走一遍真写。生成器第一遍就被耗尽，第二遍拿到空的，于是
      6 帧的 APNG 出来只剩 1 帧，**不抛异常、没有任何提示**。
      最小复现（与本项目无关）::

          frames[0].save(out, save_all=True, append_images=(f for f in frames[1:]))
          # -> n_frames == 1
          frames[0].save(out, save_all=True, append_images=frames[1:])
          # -> n_frames == 6

      实测只有 PNG 这一个格式坏：GIF/WebP/TIFF 喂生成器都是 6/6。
      所以目标是 PNG 时必须先物化成列表。内存代价和 WebP 那条一样，
      而 ``probe_peak`` 本来就按「单帧 × 帧数」估多帧图，已经算进去了。

    帧延时和循环次数从源图带过来，否则播放速度会变。
    """
    # 必须先 load() 再读 info。Pillow 的 WebP 读取端是在 load() 里才把
    # duration 填进 info 的，只 seek 不解码的话取到的全是 None，写出去就成了
    # 0 —— 动画会以最快速度播放。GIF 在 seek 时就填好了，这里一并兼容。
    # **源 GIF 自带的全局调色板要在这里读** —— 下面那个 durations 循环会把
    # img 走到最后一帧，而带透明的 GIF 从第 1 帧起会被 Pillow 合成成 RGBA，
    # 那时 `getpalette()` 就返回 None 了。
    #
    # 拿到之后把每一帧都映射回**这张原表**再写出去。不这么做的话 Pillow 会给
    # 每帧各自重新量化，颜色跟原图对不上。用户那张 zzz.gif（1254x1254、20 帧、
    # 带透明、GIMP 导出）实测：
    #     Pillow 自己量化   1.30 MB (0.12x)   平均色差 1.44
    #     映射到源的原表    0.86 MB (0.08x)   平均色差 0.46
    # 又小又准 —— 原图本来就只有这 256 种颜色，没必要另起炉灶。
    gif_master = None
    if dst_ext == '.gif':
        try:
            _pal = img.getpalette()
        except Exception:
            _pal = None
        if _pal:
            gif_master = Image.new('P', (1, 1))
            gif_master.putpalette(_pal)

    # **透明索引也必须在这里读，和调色板一个道理。** GIF 没有 alpha 通道，
    # 它的透明是「在调色板里指定一个索引当透明色」。下面那个 durations 循环
    # 会把 img 走到最后一帧，走完之后 `info['transparency']` 就是 None 了 ——
    # 在那之后再读，拿到的永远是空（我第一版就踩在这里）。
    #
    # 原来这条路是 `convert('RGB')` 之后无条件 `pop('transparency')`，
    # alpha 和输出透明索引一起没了，透明背景直接变不透明
    # （实测：首帧 84 个透明像素变 0 个）。
    gif_trans = None
    if gif_master is not None and isinstance(img.info.get('transparency'), int):
        gif_trans = img.info['transparency']

    # disposal 同理，也得在遍历帧之前读。而且 Pillow 不一定放在 `info` 里 ——
    # 有的 GIF 它解析到帧对象的 `disposal_method` 上，`info['disposal']` 是空的
    # （以前就这么漏过：源图明明写了 2，我们读出来是 None）。
    # 两个地方都看一眼。
    src_disposal = img.info.get('disposal')
    if src_disposal is None:
        src_disposal = getattr(img, 'disposal_method', None)

    # **循环次数也在这里读，而且要分清"没写"和"写了 0"。**
    # GIF 里没有 NETSCAPE 循环扩展 = 播一遍就停；`loop=0` = 无限循环。
    # 原来是保存时 `img.info.get('loop', 0)` —— 源里没写就替它填 0，
    # 只播一次的 GIF 处理完变成了无限循环。
    # 和上面 disposal 那条是同一类错：**"没写"不能用一个有含义的默认值顶替。**
    src_loop = img.info.get('loop')         # None = 源里没写

    # **APNG 的「默认图」不属于动画。** `default_image=True` 时第 0 张只是给
    # 不认 APNG 的看图软件看的静态预览，没有帧时长；动画从第 1 张开始。
    # 下面那一遍帧会把它也收进 `durations`，而 Pillow 写 APNG 时
    # （保存出去的首帧会从源继承这个标记）时长表是从**第一个动画帧**
    # 开始对的 —— 多出来的那一项让后面全部错一位：源 `[预览, 80, 110]`
    # 写出去成了 `[预览, 0, 80]`。帧数一张没少，
    # 所以只数帧数的断言拦不住它。
    has_default_image = dst_ext == '.png' and bool(
        getattr(img, 'default_image', False) or img.info.get('default_image'))

    # **给"被量化错了的不透明像素"准备一个退路索引。**
    #
    # 量化是按 RGB 找最近的调色板条目，它不知道哪个条目是透明的。调色板里
    # 很常见"透明的黑"和"不透明的黑"两个条目 RGB 完全一样（索引 0 和 1 都是
    # 0,0,0）—— 这时黑色前景会被映射到透明索引上，整块前景凭空消失。
    # 实测首帧不透明像素 25 → 9；换一个夹具更狠：
    # **整张图 25 → 0，两帧还因为变得完全一样被编码器合并成一帧。**
    #
    # 修法是量化之后把"alpha 不为 0、却落在透明索引上"的像素挪走。挪到哪：
    # 颜色离透明槽最近的那个**非透明**索引。上面那种"两个黑"的情形里，
    # 最近的就是同样 RGB 的那一个，颜色一点不差；更一般的情形下会有极小的
    # 色偏 —— 但总好过整块前景不见了。
    gif_alt = None
    if gif_trans is not None:
        _pal2 = gif_master.getpalette() or []
        _n = len(_pal2) // 3
        if _n > 1:
            _tr = _pal2[gif_trans * 3: gif_trans * 3 + 3]
            gif_alt = min((i for i in range(_n) if i != gif_trans),
                          key=lambda i: sum((_pal2[i * 3 + k] - _tr[k]) ** 2
                                            for k in range(3)))

    default_dur = img.info.get('duration') or 0
    durations = []
    # **声明了透明索引 ≠ 真的用到了。** 调色板里留一个透明槽、但整段动画一个
    # 透明像素都没有，这种 GIF 很常见。真用到了才值得付 disposal=2 的代价
    # （实测 4.14 倍体积，见下面 disposal 那段）；没用到就按不透明写，
    # 画面一模一样、体积不涨。
    #
    # 检查顺着本来就要走的这一遍帧做，不额外多解码一次：
    # P 帧查直方图里透明索引的计数（C 层一遍扫完，很快），
    # 带透明的 GIF 从第 2 帧起会被 Pillow 合成成 RGBA，那就看 alpha 的最小值。
    uses_trans = gif_trans is None
    # **GIF 允许每帧带自己的局部调色板。** 母版只取了首帧那张表，后面帧独有的
    # 颜色不在里面 —— 量化时会被映射到母版里最接近的颜色。实测：第 1 帧红、
    # 第 2 帧蓝的合法 GIF，旋转后第 2 帧变成**黑色**（10/10）。
    # 透明度断言和"数不透明像素"都拦不住它：红变黑，像素数一个不差。
    #
    # 所以顺着这一遍帧把各帧**实际用到**的颜色收一下，母版里没有的记下来。
    # 代价很小：1254x1254 / 20 帧实测只多花 0.03 秒（getcolors 超限会提前放弃）。
    master_rgb, extra_rgb = set(), []
    if gif_master is not None:
        _pal = gif_master.getpalette() or []
        master_rgb = {tuple(_pal[i * 3:i * 3 + 3])
                      for i in range(len(_pal) // 3)}
    for frame in ImageSequence.Iterator(img):
        frame.load()
        durations.append(frame.info.get('duration') or default_dur)
        if not uses_trans:
            try:
                if frame.mode == 'P':
                    hist = frame.histogram()
                    uses_trans = (len(hist) > gif_trans
                                  and hist[gif_trans] > 0)
                elif frame.mode == 'RGBA':
                    uses_trans = frame.getchannel('A').getextrema()[0] == 0
            except Exception:
                uses_trans = True       # 看不出来就按"用到了"算，宁可大也别错
        if gif_master is not None and extra_rgb is not None:
            try:
                got = frame.convert('RGB').getcolors(256)
            except Exception:
                got = None
            if got is None:
                extra_rgb = None        # 这一帧颜色就超 256，母版肯定不够用
            else:
                for _count, rgb in got:
                    if rgb not in master_rgb and rgb not in extra_rgb:
                        extra_rgb.append(rgb)
    if not uses_trans:
        gif_trans = None                # 不写透明，disposal 也就回到原来的规则

    # 母版丢掉之后，透明改由 RGBA 帧自己带着走（见下面 converted）。
    alpha_frames = False
    # **`extra_rgb` 有三种状态，一种都不能混：**
    #   `[]`    母版够用，什么都不用做
    #   `[...]` 有母版里没有的颜色，试着追加
    #   `None`  某一帧合成后就超过 256 色，数都数不过来 —— 母版肯定不够
    # 原来这里写的是 `if gif_master is not None and extra_rgb:`，`None` 和 `[]`
    # 都是假值，于是"数不过来"被当成了"不用处理"，照旧往首帧那张表上映射：
    # 第二帧右侧一块纯蓝，旋转后蓝通道最大值 255 → 0。
    # GIF 的帧是叠在画布上的，每帧自己不超 256 色，**合成后的画面**可以超。
    if gif_master is not None and (extra_rgb is None or extra_rgb):
        # 有母版装不下的颜色。**只往母版末尾追加，绝不重排** ——
        # 重排会让 `gif_trans` 那个索引指向别的颜色，透明度当场错位。
        _pal = gif_master.getpalette() or []
        if extra_rgb is not None and len(_pal) // 3 + len(extra_rgb) <= 256:
            _pal = list(_pal) + [v for rgb in extra_rgb for v in rgb]
            gif_master = Image.new('P', (1, 1))
            gif_master.putpalette(_pal)
        else:
            # 追加不下（整段动画超过 256 色）。这时候没有哪一张表能同时装下
            # 所有帧 —— 交给 Pillow 自己逐帧量化，颜色准但体积大一点。
            # 宁可文件大，也不要整帧颜色错掉。
            #
            # **透明不能跟着母版一起丢。** 透明索引是母版里的位置，母版没了
            # 它确实作废；但真用到了透明的话，就改成把每帧转成 RGBA 交出去 ——
            # Pillow 的 GIF 写入器会给 RGBA 帧自己挑一个透明索引。
            alpha_frames = gif_trans is not None
            gif_master = None
            gif_trans = None            # 索引跟着母版走，母版没了它也无效

    save_kwargs = get_quality_params(dst_ext, config.quality_opt)
    save_kwargs.pop('_quantize', None)

    # **GIF 的透明要专门保住。** GIF 没有 alpha 通道，它的透明是
    # 「在调色板里指定一个索引当透明色」。原来这条路是 `convert('RGB')`
    # 之后无条件 `pop('transparency')` —— alpha 和输出透明索引一起没了，
    # 透明背景直接变成不透明（实测：首帧 84 个透明像素变 0 个）。
    #
    # 源的透明索引直接拿来用：下面量化时映射的就是源的那张调色板
    # （gif_master），索引是对齐的。源里没有透明索引就不用管。
    # 一帧一帧地吐出处理好的画面（旋转、缩放、颜色对回源调色板），
    # 边处理边交给编码器，不用把整段动画一次性塞进内存。
    def converted():
        for frame in ImageSequence.Iterator(img):
            # copy() 不能省：Iterator 复用同一个对象，不复制的话所有帧最后
            # 都会变成同一张图。
            src_frame = frame.copy()
            if gif_trans is not None or alpha_frames:
                # **先转 RGBA，再做几何变换。** 这样 alpha 跟着旋转/缩放一起
                # 走，不用单独搬一遍，也绕开了「变换之后 info 里的
                # transparency 还在不在」这个说不清的问题。
                src_frame = src_frame.convert('RGBA')
            out = rotate_frame(src_frame, config.rotate_mode)
            out = shrink_to(out, getattr(config, 'max_edge', 0))
            if dst_ext == '.webp' and out.mode not in ('RGB', 'RGBA'):
                out = out.convert('RGBA')
            elif gif_master is not None:
                alpha = out.getchannel('A') if out.mode == 'RGBA' else None
                out = out.convert('RGB').quantize(palette=gif_master)
                # 量化出来的帧会继承 RGBA 帧 info 里的 transparency ——
                # 那是个元组，Pillow 的 GIF 写入器拿去 o8() 会直接崩。
                # 所以先清掉，真正要写的透明索引走 save_kwargs。
                out.info.pop('transparency', None)
                if alpha is not None and gif_trans is not None:
                    # 先救被量化错的：alpha 不为 0、却落到透明索引上的像素，
                    # 挪到最近的非透明索引去（见上面 gif_alt 的说明）。
                    # **顺序不能反** —— 先按回透明索引的话，这里就分不清
                    # "本来就该透明"和"被量化错了"。
                    if gif_alt is not None:
                        on_trans = out.point(
                            lambda p: 255 if p == gif_trans else 0, mode='L')
                        opaque = alpha.point(lambda a: 255 if a != 0 else 0)
                        wrong = ImageChops.multiply(on_trans, opaque)
                        if wrong.getbbox():
                            out.paste(gif_alt, None, wrong)
                    # GIF 的透明是二值的：alpha 为 0 的像素按回透明索引。
                    # 半透明没法表达，只能当不透明处理 —— GIF 本来如此。
                    mask = alpha.point(lambda a: 255 if a == 0 else 0)
                    out.paste(gif_trans, None, mask)
            yield out

    stream = converted()
    first = next(stream)

    save_kwargs['save_all'] = True
    # PNG 必须物化：它要遍历两遍，生成器第一遍就空了，结果静默只剩首帧。
    # 其余格式保持流式，省内存。
    save_kwargs['append_images'] = list(stream) if dst_ext == '.png' else stream

    if dst_ext in ('.webp', '.gif', '.png'):
        if has_default_image:
            # 默认图不算动画帧：明确告诉写入器首帧是默认图，
            # 时长表从第一个动画帧开始给（理由见上面读 default_image 那段）。
            save_kwargs['default_image'] = True
            durations = durations[1:]
        save_kwargs['duration'] = durations
        if src_loop is not None:
            save_kwargs['loop'] = src_loop
        elif dst_ext != '.gif':
            # WebP / APNG 的循环次数是格式里的必填字段，读出来一定有值，
            # 走到这里只可能是别的格式转过来的 —— 沿用原来的默认。
            save_kwargs['loop'] = 0
        # GIF 且源里没写循环扩展：不传 loop，写入器就不写扩展，照旧只播一遍。
    if gif_trans is not None:
        # 每帧的 info 里已经清掉了，真正生效的是这个 —— 写入器据此在
        # 调色板里把这一个索引标成透明。不给的话上面那些按回去的像素
        # 只是一个普通颜色，看着就是不透明的背景。
        save_kwargs['transparency'] = gif_trans
    if dst_ext == '.gif':
        if gif_trans is not None or alpha_frames:
            # **写了透明，就必须填 disposal=2。**
            #
            # 原来这里的理由是"我们写出去的每帧都是完整合成画面，不需要
            # disposal"，而且 disposal=2 会逼编码器放弃帧间差分、整帧全写
            # （zzz.gif 实测 0.86 MB → 5.94 MB，大 7 倍）。
            #
            # **这个理由只在输出不透明时成立。** 一旦帧里有透明像素，
            # GIF 的播放语义是"透明处保留画布原样" —— disposal=0 时画布上
            # 留着的就是上一帧，于是前一帧的内容从这一帧的透明区域透出来，
            # 变成残影。实测：两帧各 15 个不透明像素的源，
            # 输出成了 15、30 —— 第二帧整整多出一份上一帧。
            #
            # 所以：保住透明是有代价的，代价就是文件变大。两者冲突时
            # 以**画面正确**为准 —— 体积大是难看，画面错是坏了。
            save_kwargs['disposal'] = 2
        elif src_disposal is not None:
            # 输出不带透明：按源里写的来，源里没写就别替它写。
            save_kwargs['disposal'] = src_disposal

    first.save(out_path, **save_kwargs)


def get_quality_params(ext: str, quality_opt: str, img: Image.Image = None) -> dict:
    """根据文件格式和画质选项返回保存参数"""
    save_kwargs = {}
    ext = ext.lower()

    if ext in ('.jpg', '.jpeg'):
        save_kwargs['optimize'] = True
        # progressive 是这条路上唯一真正白捡的：**像素一个不变**（平均差和最大差
        # 与非 progressive 完全一致），体积实测 0.94~0.95x。原理是把系数按频率
        # 分几遍扫描存，熵编码效率更高，不是少存了信息。
        # 兼容性上现代软件和浏览器全支持（网页图片优化的标准做法）。
        #
        # 顺带记一条**没有采纳**的：4:2:0 色度二次采样在照片上也能省 5%
        # 且几乎看不出来（平均差 1.59 -> 1.60），但在插画/AI 出图上
        # **最大误差从 12 跳到 90** —— 硬色边会糊出色边。程序没法可靠分辨
        # 内容类型，所以保持 4:4:4。
        save_kwargs['progressive'] = True
        if '100%' in quality_opt:
            save_kwargs['quality'] = 100
            save_kwargs['subsampling'] = 0
        elif '95%' in quality_opt:
            save_kwargs['quality'] = 95
            save_kwargs['subsampling'] = 0
        elif '85%' in quality_opt:
            save_kwargs['quality'] = 85
        else:
            save_kwargs['quality'] = 95

    elif ext == '.webp':
        # method 是「花多少力气找更小的编码」，0~6。6 是最慢的一档，
        # 在大图上代价失控：实测 44.7 MP 编一张要 46 秒，而体积只比
        # method=4 小 3%（1500x1125 上 0.31s vs 0.15s、441K vs 453K）。
        # 大图一律降到 4 —— 这一档的收益远抵不上翻倍的时间。
        # 阈值取 12 MP：再往上单张就超过 3 秒了。
        big = img is not None and (img.size[0] * img.size[1]) > 12_000_000
        save_kwargs['method'] = 4 if big else 6
        if '100%' in quality_opt:
            save_kwargs['quality'] = 100
            save_kwargs['lossless'] = True
        elif '95%' in quality_opt:
            save_kwargs['quality'] = 95
        elif '85%' in quality_opt:
            save_kwargs['quality'] = 85

    elif ext == '.png':
        if '85%' in quality_opt and img is not None:
            save_kwargs['_quantize'] = True
        save_kwargs['compress_level'] = 6

    elif ext == '.gif':
        # **GIF 不压，如实返回空。** 返回空之后 `recompress_pointless` 会按
        # 「没有可调的画质参数」判成原样复制，省掉白编一遍。
        #
        # 试过减色（GIF 唯一真正的杠杆），量下来收益小、风险大，**撤掉了**：
        #   12 帧真实照片：128 色 0.91x、64 色 0.76x、32 色 0.61x
        #   而 `optimize=True` 反而更大 —— Pillow 的 GIF optimize 会给每帧写
        #   局部调色板，多帧照片上开销盖过收益
        # 调色板策略也比过：逐帧自适应在「连贯动画」上 0.75x/色差 0.20，
        # 全局表 0.82x/0.25；「场景切换」上逐帧最坏帧 0.74、全局 6.00 ——
        # 逐帧两头都更好。但**用户在真实素材上仍然看到明显的偏色**，而合成
        # 夹具复现不出来（要么被体积护栏兜住、要么误差小到看不见）。
        # 收益只有一两成、失效模式又刻画不清，不值得赌画质。
        #
        # 真要压 GIF，正确的工具是 gifsicle（外部程序），不是 Pillow。
        pass

    elif ext in ('.tif', '.tiff'):
        # 原来这里什么都不返回，画质选项对 TIFF 完全是空操作。重存时 Pillow 会
        # 沿用源文件的压缩方式，所以只在用户明确要「网络压缩」时才换成 LZW
        # （无损，不掉画质），其余情况保持原样。
        if '85%' in quality_opt:
            save_kwargs['compression'] = 'tiff_lzw'

    return save_kwargs


# libjpeg 的标准亮度量化表（JPEG Annex K）。JPEG 存进文件的量化表就是
# 「这张表 × 一个由 quality 决定的缩放系数」，所以反过来能从表里把 quality 解出来。
_STD_LUM_QT = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)


def jpeg_quality(im) -> float:
    """从量化表反推这张 JPEG 原本的 quality（1~100），拿不到返回 0。

    **只用文件头**，不解码像素：Pillow 在 ``Image.open`` 时就把量化表填进
    ``im.quantization`` 了。实测读头+反推 0.05 ms，完整解码 0.74 ms，快 16 倍；
    对 q30~q100 的标准表反推误差 ≤ 0.1（q100 因为表被钳到 1，报 99.1）。
    """
    table = (getattr(im, 'quantization', None) or {}).get(0)
    if not table or len(table) < 64:
        return 0.0
    scales = sorted(val * 100.0 / std
                    for std, val in zip(_STD_LUM_QT, list(table)[:64]) if val > 0)
    if not scales:
        return 0.0
    # 取中位数：首项容易被钳到 1，单看它会把高画质全判成 100
    scale = scales[len(scales) // 2]
    # libjpeg: scale = quality < 50 ? 5000/quality : 200 - 2*quality
    qual = (200 - scale) / 2.0 if scale <= 100 else 5000.0 / scale
    return max(1.0, min(100.0, qual))


def target_quality(quality_opt: str) -> int:
    """画质档位对应的目标 quality。"""
    for tag, val in (('100%', 100), ('95%', 95), ('85%', 85)):
        if tag in quality_opt:
            return val
    return 95


def source_bpp(im, size_bytes: int) -> float:
    """源文件的每像素比特数 —— 衡量"这张图已经被压到多狠"的便宜指标。

    不解码，只要文件大小和尺寸。未压缩的 RGB 是 24 bpp；一张压得很狠的
    有损图在 0.5~1 bpp 量级。
    """
    try:
        w, h = im.size
        return size_bytes * 8.0 / (w * h) if w and h else 0.0
    except Exception:
        return 0.0


PNG_MAGIC = bytes([137, 80, 78, 71, 13, 10, 26, 10])


def png_flevel(path: str):
    """读 PNG 第一个 IDAT 的 zlib 头，取出源文件当初用了多狠的压缩级别。

    **只读文件头附近几十个字节，不解码。** 返回 0~3，读不出来返回 None。

    zlib 的 FLG 字节高两位就是 FLEVEL，而它是按压缩级别填的：
    level 1 -> 0、2~5 -> 1、**6 -> 2**、7~9 -> 3。
    我们自己写 PNG 用的是 ``compress_level=6``，所以 **FLEVEL >= 2 的源等于
    已经用了不低于我们的级别，再压一遍必然不会更小**。

    这就是文档里说「PNG 没做成、别再走 bpp 那条路」缺的那个判据 —— bpp 反映的是
    内容复杂度，而收益取决于源用了什么级别，两者正交。FLEVEL 直接把级别写在
    文件里，不用猜。

    318 个真实 PNG 实测：

        FLEVEL      样本   占比   重编能省>=5%   命中率   平均编码耗时
        0 最快       14     4%        7           50%      18 ms
        1 快         16     5%        5           31%      36 ms
        2 默认      288    91%        2            1%     366 ms

    跳过 FLEVEL>=2：少编 288 张、省 105.3 秒，代价是错过 2 张共 0.1 MB。
    而且它恰好是最贵的一档（366 ms，是另外两档的十倍）。
    """
    if not path:
        return None
    try:
        with open(path, 'rb') as fh:
            if fh.read(8) != PNG_MAGIC:
                return None
            while True:
                head = fh.read(8)
                if len(head) < 8:
                    return None
                length, kind = struct.unpack('>I4s', head)
                if kind == b'IDAT':
                    two = fh.read(2)
                    return (two[1] >> 6) & 3 if len(two) == 2 else None
                fh.seek(length + 4, 1)     # 跳过数据和 CRC
    except (OSError, struct.error):
        return None


# 各格式的「我写完了」标记。跳过判断只看文件头，这里补一道看文件尾的。
FILE_TAILS = {
    'PNG': b'IEND',
    'JPEG': b'\xff\xd9',
    'MPO': b'\xff\xd9',
    'GIF': b'\x3b',
}


def tail_looks_complete(path: str, fmt: str) -> bool:
    """文件尾部有没有「写完了」的标记。**只读末尾 64 个字节，代价是 O(1)。**

    为什么需要它：跳过判断是按**文件头**做的 —— 不解码当然快，但也意味着
    **坏文件会被原样复制走、还报成功**。实测过：一张截断到 60 字节的 PNG，
    因为 zlib 头说「源已按不低于 level 6 压过」就被判成「再压也没用」，
    直接复制过去，界面显示成功，用户拿到的是一张打不开的图。
    JPEG（按量化表反推画质）、GIF/BMP（没有画质参数）的跳过路径同样有这毛病。

    判不出来就返回 True —— 宁可放过，也不要把好文件误判成坏的。
    """
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as fh:
            if fmt == 'WEBP':
                head = fh.read(12)
                if len(head) == 12 and head[:4] == b'RIFF':
                    # RIFF 头里写着「后面还有多少字节」，对不上就是没写完
                    want = int.from_bytes(head[4:8], 'little') + 8
                    return size >= want
                return True
            fh.seek(max(0, size - 64))
            tail = fh.read(64)
    except OSError:
        return True

    marker = FILE_TAILS.get(fmt)
    if not marker:
        return True
    if fmt == 'GIF':
        return tail.endswith(marker)
    return marker in tail


def recompress_pointless(im, ext: str, quality_opt: str,
                         size_bytes: int = 0) -> str:
    """「再压一遍」是不是注定白干？是就返回原因，否则返回空串。

    **在解码之前判断**，这是这条路上最值钱的一步：一张图解码+编码要几十到
    几百毫秒，而这里只读文件头，0.05 ms。用户实测 3393 张里 3352 张编完之后
    被体积护栏原样丢弃 —— 白烧了 1956 秒（占全程 95%）。

    判据按格式分，全部有实测支撑：

    **JPEG** —— 量化表能精确反推源画质。源画质低于「目标 - 5」才跳。
        那 5 个点的余量是真实数据要求的：源画质正好等于目标时，重编码往往
        还能挤掉一点（元数据、内嵌缩略图、编码器差异），一刀切会误伤。

    **WebP** —— 头里读不到画质，只能用 bpp，而且**只在目标 >=95 时才跳**
        （bpp<=1.5）。85% 档量下来几乎总能压小，bpp 预测不了谁能省，不跳。

    **PNG** —— 不跳，照常处理。试过按 bpp 判断"是否已经压过"，量下来分不开
        （见文件顶部那张表）；而它便宜（0.02~0.06 秒）、真收益能到 0.35x。

    **BMP 等没有画质参数的格式** —— `get_quality_params` 返回空，
        重存纯属把文件原样解码再编码一遍，体积一字不差。

    有 EXIF 方向标记的一律不跳 —— 「仅压缩」承诺过会把方向烘焙进像素，
    跳过就等于毁约（那批照片会在别的看图软件里躺倒）。
    """
    # **坏文件绝不能走「跳过」这条路。** 跳过 = 原样复制 + 报成功，
    # 用户会拿到一张打不开的图，还以为处理好了。尾部标记缺失说明没写完，
    # 那就让它走正常解码流程 —— 那边会如实报出「数据不完整」。
    _path = getattr(im, 'filename', '') or ''
    if _path and not tail_looks_complete(_path, (im.format or '').upper()):
        return ''

    # PNG 单独一条路，而且**绝不能碰 exif** —— PNG 的 `getexif()` 会强制整张
    # 解码（eXIf 块允许出现在 IDAT 之后，Pillow 只能先 `load()` 一遍才敢说没有）。
    # 实测 42 MP 的 PNG 要 293 ms + 220 MB，其它格式都是 0.0 ms。而
    # `probe_peak` 是在**主编排线程**上每张图跑一次的，解一张就串行一张。
    if ext == '.png':
        # 85% 档要做调色板量化，那一档几乎总能压小很多，不跳。
        if '85%' in quality_opt:
            return ''
        # 源已经按不低于 level 6 压过的话，我们再压一遍必然不会更小。
        # 判据和实测数据见 `png_flevel`。用户那批 3469 张里有 1946 张
        # 「编完又被丢弃」，绝大部分就是这种。
        level = png_flevel(getattr(im, 'filename', '') or '')
        if level is not None and level >= 2:
            return f'PNG 源已按不低于 level 6 压过（zlib FLEVEL={level}）'
        return ''

    try:
        if (im.getexif() or {}).get(0x0112, 1) not in (0, 1):
            return ''                      # 还得靠重存把方向烘焙进去
    except Exception:
        return ''

    want = target_quality(quality_opt)
    params = get_quality_params(ext, quality_opt, im)

    if ext in ('.jpg', '.jpeg'):
        have = jpeg_quality(im)
        if have and have <= want - JPEG_QUALITY_MARGIN:
            return f'源画质已是 {have:.0f}%，明显低于目标 {want}%'
        return ''

    if ext == '.webp':
        # 85% 档不跳：那一档几乎总能压小（见文件顶部的标定）
        if want < 95:
            return ''
        bpp = source_bpp(im, size_bytes)
        if bpp and bpp <= WEBP_BPP_HIGH_TARGET:
            return (f'WebP 已压到 {bpp:.2f} bpp'
                    f'（门槛 {WEBP_BPP_HIGH_TARGET}），再压多半更大')
        return ''

    if not params:
        return f'{ext} 没有可调的画质参数'
    return ''


def probe_peak(path: str, config=None, dst: str = '') -> tuple:
    """返回 ``(峰值 MB, 帧数)``。**只读文件头，不解码像素。**

    帧数一并返回是给调用方用的：多帧图在不旋转时只会被复制（见
    ``process_single_image``），日志里要说清楚"这些动图是带过去的、没重编码"，
    否则用户看到动图体积没变会以为程序漏处理了。

    ``config`` 非空时会把"这张图到底会不会被重编码"算进去 —— 不重编码的
    多帧图峰值内存约等于 0，不该白占内存闸门的额度。判断条件必须和
    ``process_single_image`` 里那段保持一致，改一处就要改另一处。
    """
    try:
        with Image.open(path) as im:
            width, height = im.size
            frames = getattr(im, 'n_frames', 1)
            fmt = im.format
            mode = im.mode
            ext = os.path.splitext(path)[1].lower()
            # 这一张会不会根本不解码、只是原样复制？会的话额度记 0。
            # 少了这一条，一批「已经是最优」的图会按"解码后要吃 40 MB"记账，
            # 把闸门占满 —— 实测用户那批 3393 张里 3293 张只是复制，却把
            # 608 MB 的预算吃光，同时只能跑 15 个。磁盘慢的时候恰恰需要更多
            # 并发的读写排队，这笔虚账等于自己给自己限速。
            try:
                _size = os.path.getsize(path)
            except OSError:
                _size = 0
            # 方向标记在 with 块里读（出了块 im 就关了）。PNG 不读，理由同上。
            im_exif = None
            if ext != '.png':
                try:
                    im_exif = im.getexif()
                except Exception:
                    im_exif = None
            # 多帧图 + 开了「动图也重编码」时**不能**走这条跳过判断 ——
            # 那种情况下 `process_single_image` 会真的逐帧重编码，而这里
            # 一旦判成「原样复制」就会返回 0 MB，闸门按 0 记账，实际却要解
            # 一整段动画。判据必须和 `process_single_image` 那边对得上。
            _reenc_anim = (frames > 1 and config is not None
                           and getattr(config, 'reencode_animated', False))
            skip_copy = bool(
                config is not None and not config.is_ico
                and not _reenc_anim
                and config.compress_only
                and config.rotate_mode == '不旋转'
                and not oversized(im.size, getattr(config, 'max_edge', 0))
                and recompress_pointless(im, ext, config.quality_opt, _size))
    except Exception:
        return 0.0, 1

    if skip_copy:
        return 0.0, frames

    # 峰值由「源解码」和「目标编码」里重的那一侧决定。扩展名和真实格式不符
    # 的文件（真实素材里有 PNG 改名成 .jpg 的）只看源格式会严重低估。
    factor = FORMAT_FACTORS.get(fmt, STILL_FACTOR)
    if dst:
        factor = max(factor, FORMAT_FACTORS.get(
            EXT_FORMAT.get(os.path.splitext(dst)[1].lower(), ''), 0.0))
    if config is not None:
        # 要旋转，或者源图带着 EXIF 方向标记（「仅压缩」承诺会把方向烘焙进
        # 像素），都会多出一份完整 RGBA 副本。
        # 方向标记只对非 PNG 读 —— PNG 的 getexif() 会强制整张解码，
        # 而这是在主编排线程上每张图跑一次，代价见 recompress_pointless。
        rotating = config.rotate_mode != '不旋转'
        if not rotating and ext != '.png':
            try:
                rotating = (im_exif or {}).get(0x0112, 1) not in (0, 1)
            except Exception:
                rotating = False
        if rotating:
            factor += ROTATE_EXTRA
        if fmt == 'PNG' and '85%' in (config.quality_opt or ''):
            factor += PNG_QUANTIZE_EXTRA
    per_frame_mb = width * height * MODE_BANDS.get(mode, 4) / 1048576
    if frames > 1 and fmt != 'MPO':
        # **超了最长边的动图不是复制。** `process_single_image` 那边的复制捷径
        # 带着 `not oversized(...)`，这里原来漏了 —— 要逐帧解码、缩放、重编码
        # 的动图被按 0 MB 记账，闸门形同虚设，预扫描还说它"原样复制"
        # （120x80 的 GIF、最长边 60，估 0 MB，实际输出 60x40）。
        # 这正是第四节那张表里「内存闸门记 0」那一行。
        if (config is not None and not config.is_ico
                and config.rotate_mode == '不旋转'
                and not config.reencode_animated
                and not oversized((width, height),
                                  getattr(config, 'max_edge', 0))):
            return 0.0, frames        # 只复制，不解码
        # 按**目标**格式的编码器估（见 ANIMATED_COST 的说明）。转 ICO 时动图
        # 只取首帧、不走动图编码，那种情况留给下面的旧公式。
        cost = None
        if not (config is not None and config.is_ico):
            cost = ANIMATED_COST.get(
                EXT_FORMAT.get(os.path.splitext(dst)[1].lower()) if dst
                else fmt, None) or ANIMATED_COST.get(fmt)
        if cost:
            px_mb = width * height / 1048576
            out_px_mb = px_mb
            edge = getattr(config, 'max_edge', 0) if config is not None else 0
            if oversized((width, height), edge):
                # 攒在内存里的是**缩小之后**的帧，解码那一侧仍是原尺寸的一帧
                out_px_mb = px_mb * (edge / max(width, height)) ** 2
            return out_px_mb * cost[0] * frames + px_mb * cost[1], frames
        return per_frame_mb * frames * ANIMATED_FACTOR, frames
    return per_frame_mb * factor, frames


# 目标格式存不下透明通道的，源图带 alpha 就会被压成不透明。
ALPHA_MODES = ('RGBA', 'LA', 'PA', 'P')
OPAQUE_EXTS = ('.jpg', '.jpeg', '.bmp')


def identify(args) -> FileInfo:
    """开工前认一张图。**只读文件头，不解码像素**，几毫秒。

    ``args`` 是 ``(src, dst, config, copy_only)``。做成模块级函数、参数打成
    元组，是为了能直接丢进进程池 —— 3000 张在主线程上一张张认要十几秒，
    工人全在旁边等着，这正是之前 CPU 利用率上不去的原因之一。

    认出来的东西有三个用处：
      1. **明确范围**：真要重编码的有几张、原样复制的有几张、带过去的有几张
      2. **预估耗时和内存**：闸门直接拿 ``est_mb``，主线程不用再 probe 一遍
      3. **提前报问题**：打不开的、扩展名和真实格式对不上的，开工前就说
    """
    src, dst, config, copy_only = args[0], args[1], args[2], args[3]
    info = FileInfo(path=src)
    try:
        info.size_bytes = os.path.getsize(src)
    except OSError:
        pass

    if copy_only:
        info.plan = 'carry'
        info.reason = '不支持的格式，跟着编号原样带走'
        return info

    try:
        with Image.open(long_path(src)) as im:
            info.fmt = im.format or ''
            info.mode = im.mode
            info.width, info.height = im.size
            info.frames = getattr(im, 'n_frames', 1)
    except Exception as exc:
        info.plan = 'error'
        info.error = f'{type(exc).__name__}: {exc}'
        info.reason = '打不开或格式损坏'
        return info

    src_ext = os.path.splitext(src)[1].lower()
    dst_ext = os.path.splitext(dst)[1].lower()
    # 扩展名和真实格式对不上。EXT_FORMAT 认得的才判，省得把 .tif/.heic 误报。
    claimed = EXT_FORMAT.get(src_ext)
    if claimed and info.fmt and claimed != info.fmt:
        info.mislabeled = True
    # 带透明通道的源要写进存不下 alpha 的目标格式 —— 透明会丢，而且是静默的。
    if info.mode in ALPHA_MODES and dst_ext in OPAQUE_EXTS:
        info.alpha_loss = True

    info.est_mb, info.frames = probe_peak(src, config, dst)
    if info.est_mb == 0.0:
        info.plan = 'copy'
        info.reason = ('多帧图不重编码，原样复制' if info.frames > 1
                       else '再压一遍也省不下，原样复制')
    return info


def estimate_peak_mb(path: str, config=None, dst: str = '') -> float:
    """粗估解这一张图峰值要吃多少内存（MB）。**只读文件头，不解码像素。**

    压缩后的文件大小完全说明不了问题 —— 一张 2 MB 的 JPEG 可能是 8000x8000，
    解出来就是几百 MB。所以必须看真实尺寸。

    单帧：同一时刻会同时存在解码缓冲、解出来的原图、exif_transpose 的副本、
    旋转的副本和保存缓冲，实测是 RGBA 字节数的 ``STILL_FACTOR`` 倍。

    **多帧（动图）要按帧数整个乘上去** —— 原因见 ``save_animated`` 的说明：
    WebP 编码器会把帧列表物化，全部帧同时在内存里。少算这一项的后果是闸门
    放行一堆动图，几秒内把内存冲爆（用户遇到过，5731 张跑到最后 1-2 秒内
    涨到 5 GB）。

    两个系数都是量出来的，见文件顶部的说明。

    只要 MB 数时用这个；还要帧数就直接用 ``probe_peak``，别开两次文件。
    """
    return probe_peak(path, config, dst)[0]


# 文件头 -> 这到底是个什么东西。认不出图片时拿它说清楚「那是什么」，
# 而不是一句笼统的「无法识别的图片格式或文件损坏」。
FILE_SIGNATURES = (
    (b'\x89PNG\r\n', 'PNG 图片'),
    (b'\xff\xd8\xff', 'JPEG 图片'),
    (b'GIF8', 'GIF 图片'),
    (b'BM', 'BMP 图片'),
    (b'II*\x00', 'TIFF 图片'),
    (b'MM\x00*', 'TIFF 图片'),
    (b'RIFF', 'RIFF 容器（WebP / WAV / AVI）'),
    (b'PK\x03\x04', 'ZIP 压缩包（docx、xlsx、apk 也是这个头）'),
    (b'%PDF', 'PDF 文档'),
    (b'Rar!', 'RAR 压缩包'),
    (b'7z\xbc\xaf', '7z 压缩包'),
    (b'\x1f\x8b', 'gzip 压缩包'),
    (b'ID3', 'MP3 音频'),
    (b'OggS', 'Ogg 音视频'),
    (b'ftyp', 'MP4 / MOV 视频'),
    (b'<svg', 'SVG 矢量图（本程序不支持）'),
    (b'<?xml', 'XML 文本'),
    (b'<!DOC', 'HTML 文本'),
)


def describe_broken(path: str) -> tuple:
    """打不开的时候，尽量说清楚「这到底是个什么文件」。

    原来不管什么情况都回同一句「无法识别的图片格式或文件损坏」——
    空文件、改了扩展名的文本、下载到一半的图，三种完全不同的毛病同一句话，
    用户既不知道发生了什么，也不知道该怎么办（用户原话：「你要给出具体是
    什么错误」）。

    返回 ``(给用户看的话, 给日志看的技术细节)``。
    只读开头 16 个字节，再贵也就一次 open。
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return f'文件读不到（{exc.strerror or exc}）', f'getsize 失败: {exc!r}'
    if size == 0:
        return '文件是空的（0 字节），里面什么都没有', 'size=0'
    try:
        with open(path, 'rb') as fh:
            head = fh.read(16)
    except OSError as exc:
        return f'文件打不开（{exc.strerror or exc}）', f'open 失败: {exc!r}'

    hexed = ' '.join(f'{b:02x}' for b in head[:8])
    # 小文件别显示成「0 KB」—— 空文件和 45 字节的文本看着会一模一样
    human = f'{size} 字节' if size < 1024 else f'{size / 1024:.0f} KB'
    what = None
    for sig, label in FILE_SIGNATURES:
        # ftyp 在第 5 个字节才出现，其余都在开头
        if head.startswith(sig) or (sig == b'ftyp' and sig in head[:12]):
            what = label
            break

    if what and '图片' in what:
        # 文件头是对的 —— 说明不是「不是图片」，而是内容坏了或没下载完
        return (f'{what}的开头是正常的，但后面的数据已损坏或不完整'
                f'（文件 {human}，可能没下载完 / 复制中断）',
                f'size={size} head={hexed}')
    if what:
        return (f'这不是图片，看内容像是{what}（{human}）',
                f'size={size} head={hexed}')

    printable = sum(1 for b in head if 32 <= b < 127 or b in (9, 10, 13))
    if head and printable == len(head):
        try:
            peek = head[:12].decode('ascii')
        except Exception:
            peek = repr(head[:12])
        return (f'这不是图片，像是文本文件（{human}，开头是「{peek}」）',
                f'size={size} head={hexed}')
    return (f'认不出的文件格式（{human}，文件头 {hexed}）',
            f'size={size} head={hexed}')


def process_single_image(args: tuple) -> TaskResult:
    """处理单张图片。stage=True 时先写临时文件，由主流程统一提交。

    每一段都掐了表放进 ``TaskResult.timings``：主进程把它跟「从提交到收到
    结果」的墙上时间一比，就知道慢的是解码、是写盘，还是根本不在这个函数里
    （排队、进程回收、结果传输）。开销只有几次 perf_counter，可以忽略。
    """
    src, dst, config, stage = args[0], args[1], args[2], args[3]
    copy_only = args[4] if len(args) > 4 else False
    tmp = None
    kept_original = False
    frames_dropped = 0
    did_resize = False
    max_edge = getattr(config, 'max_edge', 0)
    t0 = time.perf_counter()
    marks = {}

    # 掐表：记下某一段（打开 / 解码 / 编码写盘 / 复制）用了多少秒。
    def mark(name, since):
        marks[name] = time.perf_counter() - since

    # 收尾时把总耗时也记上，连同各段耗时一起交回主程序（日志里的「耗时分解」就来自这里）。
    def timings():
        marks['total'] = time.perf_counter() - t0
        return marks

    side = None             # 直写模式下的旁路文件，见 target()
    opened = []             # 打开着的源图，换到位之前要先关掉

    def target():
        """本次要写入的路径。**两种模式都不直接往最终位置写。**

        * 暂存（安全写入）：写临时文件，留给主流程最后统一提交 —— 取消时
          整批都能撤回。
        * 直写：也先写到旁边的临时文件，写完整了由 `place()` **立刻**一步
          换到最终位置。

        直写原来是真的"直接往目标上写"。目标就是源文件本身时（同名原地处理），
        编码器一打开文件原图就被截断了：之后磁盘满、文件被占用、编码器抛错，
        留下的是一个损坏的原文件，而且没有任何备份。"边读边写丢帧"
        也是同一个根源。现在直写和暂存的区别只剩"取消时能不能整批撤回"和
        "同一时刻多占多少磁盘"（直写只多占正在处理的那一张）。
        旁路名字符合临时文件的命名规律，进程被强杀也能被清扫认出来。
        """
        nonlocal tmp, side
        if stage:
            tmp = make_temp_path(dst)
            return tmp
        side = make_temp_path(write_dst)
        return side

    def place():
        """直写模式：把写完整的旁路文件换到最终位置。暂存模式什么都不做。

        换之前先把源图关掉 —— 目标就是源文件时，Windows 不让替换一个还开着的
        文件（多帧格式为了能 seek 会一直开着）。
        """
        nonlocal side
        # 这一张最后会顶替或删掉原文件的话，先把新文件的数据刷到盘上
        # （暂存模式下顶替发生在提交时，但刷盘放在这里做 —— 工人是并行的）。
        work = side if side is not None else tmp
        if at_stake and work and os.path.exists(work):
            flush_to_disk(work)
        if side is None:
            return
        for im in opened:
            try:
                im.close()
            except Exception:
                pass
        os.replace(side, write_dst)
        side = None

    def cleanup():
        """清掉自己建的临时文件（暂存的和旁路的）。最终位置上的文件不碰。"""
        if stage:
            discard_temp(tmp)
        discard_temp(side)

    def revert_to_original(out_path):
        """产物没压小 → 保留原件。成功返回 None，失败返回错误信息。

        直写、而且目标就是源文件时，原件根本还没被动过 —— 丢掉旁路文件就是
        "保留原件"，不用再复制一遍。其余情况照旧用原件顶替产物。
        """
        nonlocal side
        if side is not None and write_dst == src:
            discard_temp(side)
            side = None
            return None
        return keep_original_instead(out_path)

    def keep_original_instead(out_path):
        """产物没压小 → 用原件顶替它。成功返回 None，失败返回错误信息。

        **先复制到旁边，复制成功了再一步替换。** 原来这里是
        `os.remove(out_path)` + `shutil.copy2(src, out_path)` 裹在同一个 try 里、
        尾巴是 `except OSError: pass`：复制要是**在截断目标之后**失败，
        残缺文件就留在原地，错误被吞掉，函数照样返回 success=True ——
        提交器只看"暂存文件在不在"，于是把残缺产物转正、把原件的备份删掉。

        实测（10/10 复现）：74 字节的 PNG 处理完只剩一个
        **0 字节**的文件，原件和恢复备份都没有，而 API 报 `ok=true`。
        **备份保护拦不住一个被错标成功的产物** —— 所以错误必须往上报。

        用 `make_temp_path` 取旁路名字：它符合临时文件的命名规律，
        万一进程被强杀留下残骸，`sweep_temps` 认得出来、会清掉。
        """
        side = make_temp_path(out_path)
        try:
            shutil.copy2(src, side)
            os.replace(side, out_path)
        except OSError as exc:
            discard_temp(side)
            return f'回退保留原件时写入失败: {exc}'
        return None

    def fallback_failed(msg, out_path):
        """回退失败的收尾：清掉这次留下的半成品，然后报失败。

        `out_path` 在两种模式下都是临时文件（见 target()），`cleanup()` 就够了；
        最终位置上的文件这时还没被碰过。
        """
        cleanup()
        return TaskResult(False, src, dst, msg, 'io', timings=timings())
    rot = config.rotate_mode
    quality_opt = config.quality_opt
    compress_only = config.compress_only

    src = long_path(src)
    dst = long_path(dst)

    try:
        dst_dir = os.path.dirname(dst)
        if dst_dir and not os.path.exists(dst_dir):
            os.makedirs(dst_dir, exist_ok=True)

        # ━━ 第 1 步：判断这张图到底要不要真的处理 ━━
        # 不支持的格式、什么设置都不改的，后面直接原样复制就结束。
        #
        # 不支持的格式（gif、mp4、txt…）只跟着编号走一份复制，绝不尝试解码。
        # copy2 会把修改时间等属性一并带过去。
        need_process = not copy_only and (
            rot != '不旋转' or compress_only or config.is_ico)

        # 设了最长边上限时，本来只打算原样复制的图（比如「一键编号」）也得
        # 真处理一遍 —— 前提是它确实超了。只读文件头判断，不解码。
        if max_edge and not copy_only and not need_process:
            try:
                with Image.open(src) as probe:
                    need_process = oversized(probe.size, max_edge)
            except Exception:
                pass

        src_ext = os.path.splitext(src)[1].lower()
        # 没装 pillow-heif 时 .heic 不在支持列表里，会走 copy_only —— 那就别
        # 再把它拉回解码分支，否则一定失败。
        is_heic = src_ext in ('.heic', '.heif') and not copy_only
        if is_heic:
            need_process = True
            # 转 ICO 时目标就该是 .ico；这里无条件改成 .jpg 会写出一个名为 .jpg
            # 的 ICO 文件，而且那个文件名从没在查重表里登记过，可能覆盖别人。
            if not config.is_ico and os.path.splitext(dst)[1].lower() != '.jpg':
                dst = os.path.splitext(dst)[0] + '.jpg'

        # **直写时，目标和源可能是同一个文件、只是名字大小写不同。**
        # Windows 的目录不分大小写：把 `img_001.jpg` 原地改成 `IMG_001.jpg`，
        # 两个名字指的是同一个文件。编排那边按规范化路径认出了这一点（所以才
        # 不给暂存文件），可这里和提交器原来都只比字符串 —— 于是下面一串
        # `out_path != src` 的护栏全部看走眼，提交器更是把这个**唯一的文件**
        # 当成"要让位的旧文件"挪成备份、再当成功删掉：压缩或旋转之后目录
        # 直接变空，而返回值是成功（10/10）。
        #
        # 所以这种情况下**就往源路径上写**。写的是同一个文件，但下面所有
        # "目标是不是源本身"的判断从此按字符串比就是对的。改大小写这件事
        # 留给提交器最后做一次改名（`TaskResult.dst_path` 还是新名字）。
        # 只认"同一条路径"，不认硬链接之类的别名 —— 导出到别的目录时，
        # 绝不能因为某种别名关系就往源文件上写。
        write_dst = dst
        if (not stage and src != dst
                and os.path.normcase(os.path.abspath(src))
                == os.path.normcase(os.path.abspath(dst))):
            write_dst = src

        # 产物和源在同一个文件夹里 = 原地处理：这一张最后会顶替或删掉原文件。
        # 这种情况下换到位之前要先把数据刷到盘上，见 `utils.flush_to_disk`。
        at_stake = (os.path.normcase(os.path.dirname(os.path.abspath(src)))
                    == os.path.normcase(os.path.dirname(os.path.abspath(dst))))

        # ── 不需要处理：原样复制过去，这张图就做完了 ──
        if not need_process:
            if src == write_dst:
                # 名字没变，或者只是改大小写（那一步由提交器来做）
                return TaskResult(True, src, dst, timings=timings())
            t_copy = time.perf_counter()
            shutil.copy2(src, target())
            place()
            mark('copy', t_copy)
            return TaskResult(True, src, dst, tmp_path=tmp, timings=timings())

        try:
            src_stat = os.stat(src)
            original_times = (src_stat.st_atime, src_stat.st_mtime)
        except:
            original_times = None

        out_path = target()

        t_open = time.perf_counter()
        # ━━ 第 2 步：打开图片（此时只读了文件头，像素还没解码） ━━
        with Image.open(src) as img:
            opened.append(img)
            mark('open', t_open)
            # 多帧图像（动图 WebP/GIF、APNG、多页 TIFF）逐帧处理后整体写回，
            # 动画得以保留。MPO 是双摄手机拍的普通照片，第二帧只是附属图，
            # 按单帧处理即可。目标格式存不下多帧时（例如转 ICO、转 JPG）
            # 才退回第一帧。
            frame_count = getattr(img, 'n_frames', 1)
            dst_ext_early = os.path.splitext(dst)[1].lower()
            # 多帧源要被写进一个存不下多帧的格式（转 ICO、转 JPG、伪装成 .bmp
            # 的动图…）时，动画就没了。原来这件事**一声不吭** —— 日志不写、
            # 返回值里连"帧"这个字都没有，用户拿到一批看着正常、实际不会动的
            # 文件，统计里还都算"成功"。现在带回去汇总报出来。
            if (frame_count > 1 and img.format != 'MPO'
                    and (config.is_ico or dst_ext_early not in ANIMATED_FORMATS)):
                frames_dropped = frame_count
            # ━━ 第 3 步：动图（多帧）单独走这条路 ━━
            # 不旋转、又没开「动图也重编码」→ 原样复制；否则逐帧处理后写回，再过一遍体积护栏。
            if (frame_count > 1 and img.format != 'MPO'
                    and not config.is_ico and dst_ext_early in ANIMATED_FORMATS):
                # 不旋转就没有非重编码不可的理由，原样复制走人。
                #
                # 重编码一个多帧图代价极高，收益是负的。实测（24 帧 700x700
                # 的照片式内容，源文件 quality=80）：
                #     不旋转 = 复制      0.00 s   1.99 MB（源的 100%）
                #     顺时针 90° 重编码  2.98 s   2.82 MB（源的 142%）
                # 已经有损压过的源再按「通用高清 95%」编一遍，画质只降不升、
                # 体积反而涨四成；60 帧 1200x1200 那档峰值工作集 228 MB、耗时
                # 3.1 s，而复制是 22 MB、0.00 s。用户点「仅压缩」，得到的却是
                # 又慢、又不省、还卡在最后一批动图上（5731 张那次就卡在这里）。
                #
                # 复制保留全部帧、帧延时、循环次数和文件属性，比重编码更忠实。
                # 一旦选了旋转就必须真的逐帧转，那时才走 save_animated；
                # 高级设置里的「动图也重编码」也能把这条捷径关掉。
                # `tests/test_core.py` 对这三种情况都有断言。
                if (rot == '不旋转' and dst_ext_early == src_ext
                        and not config.reencode_animated
                        and not oversized(img.size, max_edge)):
                    img.close()
                    t_copy = time.perf_counter()
                    # **源和目标可能就是同一个文件。** 原地直写时
                    # （保留原名 + 仅压缩 + 关掉安全写入）最终位置就是 src，
                    # 而这条分支本来什么变换都不做 —— 文件已经就是我们想要的
                    # 样子，不动它才是对的（原来在这里 `copy2(src, src)`
                    # 直接报错）。
                    if stage or write_dst != src:
                        shutil.copy2(src, out_path)
                        place()
                    mark('copy', t_copy)
                    return TaskResult(True, src, dst, tmp_path=tmp,
                                      timings=timings())

                # 这一轮会不会真的缩尺寸：`save_animated` 里逐帧调的
                # `shrink_to` 只在超过上限时才动手，条件就是这一句。
                # **缩了就要报上去** —— 原来这条路返回的 TaskResult 没带
                # resized，于是 API 的 `resized` 和界面的缩放计数都漏掉动图
                # （已复现：12x8 缩成 6x4 而 resized 还是空的）。
                anim_resized = oversized(img.size, max_edge)

                t_enc = time.perf_counter()
                # **不能一边读源文件、一边往同一个文件上写。**
                # `save_animated` 是把帧以生成器交给编码器的：编码器先打开
                # 输出文件（**截断**），再回头向生成器要后面的帧 —— 而那些
                # 帧还得从这个已经被截断的文件里读。原地直写旋转时
                # （10/10）：4 帧的 GIF 剩 1 帧还报成功，
                # 多页 TIFF 直接报错、原文件已经打不开了。
                # APNG / WebP 没事只是因为它们恰好先把帧全物化了。
                #
                # `out_path` 现在两种模式下都是旁边的临时文件（见 target()），
                # 源读完、关掉之后才由 place() 换到位，所以这里不会再撞上。
                # 没有改成"把帧全读进内存" —— 那会把大动图的内存问题请回来
                # （见 ANIMATED_COST）。
                save_animated(img, out_path, dst_ext_early, config,
                              frame_count)
                mark('encode', t_enc)

                # **体积护栏，动图这条路上原来是没有的。** 用户勾了「动图也
                # 重编码」跑 GIF：95% 档没有可调的画质参数，重编一遍产出反而
                # 大 21%，而它照样被当成成功写了出去。护栏的条件跟单帧那条
                # 一致 —— 只在「就地压缩、不旋转、不换格式、不缩尺寸」时兜底，
                # 因为那几种情况下丢掉结果会把旋转/缩放一起撤销。
                if (compress_only and rot == '不旋转' and not config.is_ico
                        and dst_ext_early == src_ext
                        and not oversized(img.size, max_edge)):
                    try:
                        bigger = (os.path.getsize(out_path)
                                  >= os.path.getsize(src))
                    except OSError:
                        bigger = False      # 读不到大小就不回退，保持产物
                    if bigger:
                        why_fail = revert_to_original(out_path)
                        if why_fail:
                            return fallback_failed(why_fail, out_path)
                        kept_original = True

                if original_times:
                    try:
                        os.utime(out_path, original_times)
                    except OSError:
                        pass
                place()
                return TaskResult(True, src, dst, tmp_path=tmp,
                                  timings=timings(),
                                  kept_original=kept_original,
                                  resized=bool(anim_resized))

            # ━━ 第 4 步：单张图：解码之前先判断「再压一遍是不是注定白干」 ━━
            # 是的话直接复制原文件，省掉整个解码 + 编码（整条流水线里省时间最多的就是这一步）。
            #
            # 解码之前先问一句：这一趟重存是不是注定白干？
            # 是的话直接复制原字节 —— 省掉整个解码+编码，实测快几十倍。
            # 只在「就地压缩」这一档做：选了旋转、转 ICO、换格式都另有目的。
            if (compress_only and rot == '不旋转' and not config.is_ico
                    and dst_ext_early == src_ext
                    and not oversized(img.size, max_edge)):
                try:
                    src_size = os.path.getsize(src)
                except OSError:
                    src_size = 0
                why = recompress_pointless(img, dst_ext_early, quality_opt,
                                           src_size)
                if why:
                    img.close()
                    t_copy = time.perf_counter()
                    # 同上：原地直写、最终位置就是源文件时，什么都不用写 ——
                    # 原件原样留着就是结果。
                    if stage or write_dst != src:
                        shutil.copy2(src, out_path)
                        place()
                    mark('copy', t_copy)
                    return TaskResult(True, src, dst, tmp_path=tmp,
                                      timings=timings(), kept_original=True)

            # ━━ 第 5 步：解码像素，并按照片里记录的方向（EXIF）自动转正 ━━
            icc_profile = img.info.get('icc_profile')

            # 这一趟除了压缩，是不是还顺手把 EXIF 方向烘焙进像素了？
            # 是的话体积护栏就不能兜底 —— 见下面那段说明。
            try:
                baked_orientation = (img.getexif() or {}).get(0x0112, 1) not in (0, 1)
            except Exception:
                baked_orientation = False

            # exif_transpose 会触发真正的像素解码，所以这一段量的就是解码耗时
            t_dec = time.perf_counter()
            img = ImageOps.exif_transpose(img)
            mark('decode', t_dec)

            # exif 必须从转置后的图上取。exif_transpose 已经把方向烘焙进像素并
            # 删掉了 Orientation 标记；若沿用打开时的原始 exif，看图软件会按旧
            # 标记再转一次 —— 这就是「仅压缩」把照片转歪 90° 的原因。
            exif_data = img.info.get('exif')

            # ━━ 第 6 步：按设置旋转；再按「最长边不超过」缩小（只缩不放） ━━
            if rot == '不旋转':
                out = img
            elif '顺时针' in rot:
                out = img.transpose(Image.Transpose.ROTATE_270)
            elif '逆时针' in rot:
                out = img.transpose(Image.Transpose.ROTATE_90)
            elif '180' in rot:
                out = img.transpose(Image.Transpose.ROTATE_180)
            else:
                out = img

            # 缩尺寸放在旋转之后、保存之前。放在旋转之前也行，但那样
            # 「长边」的含义会随旋转前后的宽高互换而改变，容易让人算不明白。
            before_size = out.size
            out = shrink_to(out, max_edge)
            did_resize = out.size != before_size

            dst_ext = os.path.splitext(dst)[1].lower()
            # ━━ 第 7 步：按格式和画质档准备保存参数（JPEG 画质、PNG 压缩级别等） ━━
            save_kwargs = get_quality_params(dst_ext, quality_opt, out)

            # 名字整个就是一个"扩展名"时（`.jpg`、`..jpg` 这种，从相机或聊天
            # 工具导出时并不罕见），os.path.splitext 认为它没有扩展名 ——
            # splitext('.jpg') == ('.jpg', '')。Pillow 推断不出格式会抛
            # "unknown file extension"。这时把源图的格式显式告诉它，
            # 文件名保持原样不动。
            if not dst_ext:
                save_kwargs['format'] = (getattr(img, 'format', None)
                                         or 'JPEG')

            if save_kwargs.pop('_quantize', False):
                if out.mode == 'RGBA':
                    out = out.quantize(colors=255, method=Image.Quantize.FASTOCTREE)
                elif out.mode in ('RGB', 'L'):
                    out = out.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
                elif out.mode == 'LA':
                    out = out.convert('RGBA')
                    out = out.quantize(colors=255, method=Image.Quantize.FASTOCTREE)

            if exif_data and dst_ext in ('.jpg', '.jpeg', '.webp', '.tiff', '.tif'):
                save_kwargs['exif'] = exif_data

            if icc_profile:
                save_kwargs['icc_profile'] = icc_profile

            # JPEG 只认 RGB/L/CMYK。调色板图（GIF 内容、量化过的 PNG）和带
            # 透明通道的 HEIC 直接存会抛 OSError，且被误报成「IO错误」。
            if dst_ext in ('.jpg', '.jpeg') and out.mode not in ('RGB', 'L', 'CMYK'):
                out = out.convert('RGB')

            # .jpg 和 ICO 早就有这层兜底，.png / .bmp 一直没有，于是源图的颜色
            # 模式直接撞进编码器：印刷厂常见的 CMYK JPEG 存成 png 报
            # "cannot write mode CMYK as PNG"，16 位或 LA 的 PNG 存成 bmp 同理。
            # 同一个文件转 ICO 反而成功 —— 只因为那条路提前 convert('RGBA') 了。
            if dst_ext == '.png' and out.mode == 'CMYK':
                out = out.convert('RGB')
            elif dst_ext == '.bmp' and out.mode not in ('RGB', 'L', 'P'):
                out = out.convert('RGB')

            # ━━ 第 8 步：写文件：转 ICO 图标走这边，普通图片走下面的 else ━━
            if config.is_ico:
                ico_sizes_raw = config.ico_sizes or [256]

                bmp_fmt = None
                if '强制 PNG' in config.ico_format:
                    bmp_fmt = 'png'
                elif '强制 BMP' in config.ico_format:
                    bmp_fmt = 'bmp'

                if out.mode != 'RGBA':
                    out = out.convert('RGBA')

                orig_w, orig_h = out.size
                icon_images = []
                processed_sizes = []
                final_sizes_to_process = []
                for s_val in ico_sizes_raw:
                    if s_val == 'multi':
                        final_sizes_to_process.extend([f'fixed_{sz}' for sz in (256, 128, 64, 48, 32, 16)])
                    else:
                        final_sizes_to_process.append(s_val)

                for s_val in final_sizes_to_process:
                    is_auto = False
                    target_limit = 0

                    if s_val.startswith('auto_'):
                        is_auto = True
                        target_limit = int(s_val.split('_')[1])
                    elif s_val.startswith('fixed_'):
                        target_limit = int(s_val.split('_')[1])
                    else:
                        try:
                            target_limit = int(s_val)
                        except:
                            continue

                    # ICO 的目录项用一个字节存宽高，256 是格式上限。Pillow 对超
                    # 出的尺寸是静默跳过的，全部超出就会写出一个 6 字节的空文件。
                    target_limit = min(target_limit, ICO_MAX_SIZE)
                    target_size = (target_limit, target_limit)

                    if is_auto and orig_w <= target_limit and orig_h <= target_limit:
                        actual_size = (orig_w, orig_h)
                        icon_img = out.copy()
                    elif (orig_w, orig_h) == target_size:
                        actual_size = target_size
                        icon_img = out.copy()
                    else:
                        actual_size = target_size
                        icon_img = out.resize(actual_size, Image.Resampling.LANCZOS)

                    if actual_size not in processed_sizes:
                        icon_images.append(icon_img)
                        processed_sizes.append(actual_size)

                combined = list(zip(icon_images, processed_sizes))
                combined.sort(key=lambda x: x[1][0], reverse=True)
                icon_images = [x[0] for x in combined]
                final_sizes = [x[1] for x in combined]

                if not icon_images:
                    icon_images = [out.resize((256, 256), Image.Resampling.LANCZOS)]
                    final_sizes = [(256, 256)]

                save_args = {
                    'format': 'ICO',
                    'append_images': icon_images[1:],
                    'sizes': final_sizes
                }
                if bmp_fmt:
                    save_args['bitmap_format'] = bmp_fmt

                t_enc = time.perf_counter()
                icon_images[0].save(out_path, **save_args)
                mark('encode', t_enc)

            else:
                if out.mode == 'P':
                    save_kwargs.pop('icc_profile', None)

                t_enc = time.perf_counter()
                out.save(out_path, **save_kwargs)
                mark('encode', t_enc)

                # ━━ 第 9 步：体积护栏：压完反而比原图大，就丢掉结果、换回原文件 ━━
                #
                # 体积护栏：「压缩」压出来比原来还大，就把结果丢掉、原样复制。
                #
                # 流水线原来只管按参数重存、从不回头看一眼，于是「一键压缩」
                # 会干出这种事（实测，网络压缩 85%）：
                #     照片_q60.jpg   94,187 -> 109,999   1.17x   变大了
                #     照片.png       23,970 ->  65,225   2.72x   变大了
                #     图.bmp      1,890,054 -> 1,890,054 1.00x   完全没动
                # 已经压过的 JPEG 是双输：体积涨了，画质还又掉一轮；BMP 没有
                # 画质参数，纯属白解码一遍。用户点的按钮写着「大幅缩小体积」。
                #
                # 只在「就地压缩」这一档兜底 —— 选了旋转、转 ICO、换格式时体积
                # 变大是应该的，不该拦。
                # （原来直写 + 原地同名时兜不了：产物是直接写在源文件上的，
                # 原字节已经被盖掉。现在直写也先写旁路文件，这时源还完好，
                # 所以这一档同样能兜。）
                # **烘焙过方向的不能兜底。** 护栏管的是"压缩没压小就别压"，
                # 可这一趟顺手还把躺倒的照片转正了 —— 把结果丢掉复原原文件，
                # 等于把转正也一并撤销，用户点了「仅压缩」却发现照片还是躺着的。
                # （q60 的照片按 85% 重存必然变大，所以这条一撞一个准，
                #   tests/test_formats.py 里盯着。）
                # 缩过尺寸的也不能兜底 —— 跟烘焙方向是同一类问题：护栏管的是
                # 「压缩没压小」，把结果丢掉会连缩放一起撤销，用户设了长边上限
                # 却发现图还是原来那么大。
                if (compress_only and rot == '不旋转' and not config.is_ico
                        and dst_ext == src_ext
                        and not baked_orientation and not did_resize):
                    try:
                        bigger = (os.path.getsize(out_path)
                                  >= os.path.getsize(src))
                    except OSError:
                        bigger = False      # 读不到大小就不回退，保持产物
                    if bigger:
                        why_fail = revert_to_original(out_path)
                        if why_fail:
                            return fallback_failed(why_fail, out_path)
                        kept_original = True

        if original_times:
            try:
                os.utime(out_path, original_times)
            except:
                pass

        # 直写模式：产物已经完整地写在旁路文件里，这里才换到最终位置。
        # 源图在上面的 with 结束时已经关了。
        place()

        return TaskResult(True, src, dst, tmp_path=tmp, timings=timings(),
                          kept_original=kept_original,
                          frames_dropped=frames_dropped,
                          resized=did_resize)

    # ━━ 出错时：按错误类型翻译成人话（权限不足 / 文件损坏 / 磁盘满 / 路径太长…），
    # 这张图记为失败，但不会拖垮整批 ━━
    #
    # 失败的结果也带上分段耗时 —— 慢到超时的任务往往正是失败的那个
    except PermissionError as e:
        cleanup()
        # 权限不足最常见的其实是「文件正被别的程序打开」，直说免得用户去翻权限
        return TaskResult(False, src, dst,
                          f'没有权限读写，或文件正被其它程序占用'
                          f'（{e.strerror or e}）', 'io',
                          timings=timings(), detail=repr(e))
    except Image.DecompressionBombError as e:
        # 一张 388 KB 的 PNG 能解出 20000x20000（4 亿像素）。拦下来是对的，
        # 但原来落到最后的兜底分支，用户看到的是一整句英文、归类成"其他"，
        # 既看不懂也不知道该怎么办。
        cleanup()
        size = getattr(getattr(e, 'args', [None])[0], 'size', None)
        detail = f'（{size[0]}x{size[1]}）' if size else ''
        return TaskResult(False, src, dst,
                          f'图片尺寸异常巨大{detail}，已跳过以防耗尽内存',
                          'image', timings=timings(),
                          detail=f'{type(e).__name__}: {e}')
    except UnidentifiedImageError:
        cleanup()
        # 不要再回那句笼统的「无法识别」—— 看一眼文件头就能说清是空文件、
        # 是改了扩展名的别的东西、还是下载到一半的图。
        why, detail = describe_broken(src)
        return TaskResult(False, src, dst, why, 'image',
                          timings=timings(), detail=detail)
    except OSError as e:
        cleanup()
        err_str = str(e).lower()
        # 每一条失败都要带技术细节，否则日志里只剩一句翻译过的中文，
        # 查 bug 的人还得自己复现 —— 日志就白记了。
        _det = f'{type(e).__name__}: {e}'
        # Pillow 的原文是英文，直接抛给用户就是「IO错误: Truncated File Read」——
        # 分类做得再好，落到这一句还是看不懂。常见的几种翻成人话。
        if 'cannot identify' in err_str:
            why, detail = describe_broken(src)
            return TaskResult(False, src, dst, why, 'image',
                              timings=timings(), detail=detail)
        for probe, cn in (('truncated', '文件不完整或已损坏（数据被截断）'),
                          ('broken data stream', '图片数据流已损坏'),
                          ('could not create decoder',
                           '数据不完整或该格式不受支持（解码器建不起来）'),
                          ('image file is truncated', '图片数据不完整'),
                          ('decompression bomb', '图片尺寸异常大，已被安全策略拦下'),
                          ('unsupported', '该格式或该特性不受支持')):
            if probe in err_str:
                return TaskResult(False, src, dst, f'{cn}（{e}）', 'image',
                                  timings=timings(), detail=_det)
        if 'no space' in err_str or 'disk' in err_str:
            return TaskResult(False, src, dst, f'磁盘空间不足: {e}', 'io',
                              timings=timings(), detail=_det)
        if 'path' in err_str and ('long' in err_str or '260' in err_str):
            return TaskResult(False, src, dst, f'路径过长: {e}', 'io',
                              timings=timings(), detail=_det)
        return TaskResult(False, src, dst, f'IO错误: {e}', 'io',
                          timings=timings(), detail=_det)
    except Exception as e:
        cleanup()
        # 没预料到的错：给用户留下异常类型（光一句 str(e) 经常是空的或没头没尾），
        # **完整调用栈随结果带回主进程写进日志** —— 工作子进程不自己写日志文件，
        # 多个进程抢同一个文件容易写乱。查 bug 的人要的就是这段。
        return TaskResult(False, src, dst,
                          f'{type(e).__name__}: {e}' if str(e) else type(e).__name__,
                          'unknown', timings=timings(),
                          detail=traceback.format_exc())
