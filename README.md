# 图片处理工作台 V4ce · Image Workbench V4ce

一款 Windows 批量图片处理工具，用来压缩照片、调整方向和尺寸、统一编号，以及制作 ICO 图标。支持拖入文件或文件夹，提供中文、英文界面，也可通过命令行调用。

A batch image tool for Windows. Use it to shrink photos, fix their orientation, resize them, give a whole folder tidy sequential names, or turn images into ICO icons. You can drag files or folders straight in, the interface comes in Chinese and English, and there's a command line if you'd rather script it.

**日常使用下载 EXE 即可，无需安装 Python。** 简易模式的一键任务会另存处理结果，保留来源文件。

**For everyday use, just grab the EXE — no Python needed.** The one-click tasks in Simple mode always save to a new folder and leave your originals alone.

## 下载与启动 · Download and run

在本项目的 **Releases** 页面选择一种程序包：

Head to the **Releases** page and pick one of the two builds:

| 文件 / File | 使用方式 / How to use it |
| --- | --- |
| `ImageWorkbenchV4ce.exe` | 单文件版，下载后双击运行<br>Single-file build. Download it and double-click. |
| `ImageWorkbenchV4ce_dir.zip` | 目录版，完整解压后运行其中的 EXE；请保留 `_internal` 文件夹<br>Folder build. Extract everything and run the EXE inside; keep the `_internal` folder next to it. |

当前提供 Windows 64 位程序。两种版本功能相同，选择一种即可。发布附件中的源码包供查看、修改和重建程序使用，运行成品不需要下载它们。

Both are 64-bit Windows programs with the same features, so either one is fine. The source archives attached to each release are there for people who want to read, change or rebuild the program — you don't need them just to run it.

## 快速上手 · Quick start

1. 打开程序，通过「浏览」选择来源文件夹，或将图片、文件夹拖入窗口。<br>Open the program and pick a source folder with **Browse...**, or drag images or folders onto the window.
2. 在简易模式中点击需要的一键任务，程序会立即开始处理。<br>In Simple mode, click the one-click task you want. Processing starts right away.
3. 等待任务结束，查看输出目录；遇到失败项目时，可在界面日志中查看原因。<br>When it's done, check the output folder. If anything failed, the log in the window tells you why.

例如，选择 `D:\照片\旅行` 后使用一键任务，结果默认放在 `D:\照片\旅行_已处理`（英文界面下是 `旅行_processed`），来源目录保留。要更改导出位置，或同时调整多个选项，请使用高级模式。

For example, pick `D:\照片\旅行` and run a one-click task: the results land in `D:\照片\旅行_processed` (or `旅行_已处理` if the interface is in Chinese), and the source folder is untouched. To send the output somewhere else, or to combine several options, switch to Advanced mode.

## 可以做什么 · What it can do

| 功能 / Task | 适用场景 / Good for |
| --- | --- |
| 一键压缩<br>Compress | 缩小图片文件，方便发送或上传，保留文件名<br>Making files smaller for sending or uploading; file names stay the same |
| 一键转正<br>Auto-rotate | 根据 EXIF 方向信息摆正照片；没有方向信息时，请手动选择旋转角度<br>Straightening photos using their EXIF orientation. If a photo has no orientation data, pick a rotation angle yourself |
| 批量旋转<br>Rotate left / right | 将一批图片顺时针或逆时针旋转 90°；高级模式还可选择 180°<br>Turning a whole batch 90° either way; Advanced mode also offers 180° |
| 一键编号<br>Number | 为图片统一编排顺序编号<br>Renaming a batch into one clean numbered sequence |
| 限制长边<br>Limit long edge | 按比例缩小大图，适合制作网页用图；不会放大较小的图片<br>Scaling big images down proportionally, handy for web use; smaller images are never enlarged |
| 转成图标<br>To icon | 导出 ICO，支持包含 16～256 像素的多尺寸图标<br>Exporting ICO files, including multi-size icons from 16 to 256 px |

高级模式可以组合旋转、压缩、缩放和命名选项，并设置名称前缀、起始编号、编号位数、子目录扫描和输出目录结构。也可选择性能模式或自定义并发数。

Advanced mode lets you mix rotation, compression, resizing and naming, and set a name prefix, starting number, number of digits, whether to scan subfolders, and how the output folders are laid out. You can also choose a performance mode or set the number of workers yourself.

## 支持的格式 · Supported formats

- **图片输入：** JPEG、PNG、BMP、WebP、TIFF、GIF。<br>**Input:** JPEG, PNG, BMP, WebP, TIFF, GIF.
- **HEIC / HEIF：** 当前发布版已包含支持组件，通常转换为 JPG；选择制作图标时输出 ICO。<br>**HEIC / HEIF:** supported out of the box in the released builds. These are normally converted to JPG, or to ICO when you're making icons.
- **图标输出：** ICO。ICO 不在当前批量输入格式列表中。<br>**Icon output:** ICO. Note that ICO is not accepted as a batch *input* format.
- **多帧图片：** 支持 GIF、动态 WebP、多页 TIFF 和 APNG；旋转或缩放会处理各帧，无需变换时通常直接复制。动画重编码可在高级模式中设置。<br>**Multi-frame images:** GIF, animated WebP, multi-page TIFF and APNG all work. Rotating or resizing is applied to every frame; when nothing needs to change, the file is usually copied as is. Re-encoding animations can be turned on in Advanced mode.

源码运行时，HEIC / HEIF 支持取决于是否安装 `pillow-heif`。实际可用格式也可通过命令行的 `capabilities` 查询。

When running from source, HEIC / HEIF support depends on whether `pillow-heif` is installed. You can always ask the program what it supports with the `capabilities` command.

## 处理前需要了解 · Good to know before you start

**压缩会怎样影响画质？**

**How does compression affect quality?**

一键压缩使用 85% 档位，其中 PNG 会量化为最多 256 色，可能出现色带；照片、渐变图或需要保留颜色细节的素材应先检查处理效果。JPEG 即使使用 100% 档位重新保存，也不是无损处理。一键转正使用最高画质重存，同样不承诺无损。

The one-click Compress task uses the 85% setting. At that setting PNG files are quantized to at most 256 colours, which can cause visible banding — check the result first for photos, gradients or anything where colour detail matters. Re-saving a JPEG is never lossless, even at 100%. Auto-rotate re-saves at the highest quality, but that isn't lossless either.

**为什么有些图片没有变小？**

**Why didn't some images get smaller?**

已经压缩过的图片不一定还能明显缩小。仅压缩且未改变方向、尺寸和格式时，如果新文件没有更小，程序会保留原文件内容；涉及 EXIF 转正、手动旋转、缩放或格式转换时，输出体积取决于处理结果。

Images that have already been compressed often can't shrink much more. When you only compress — no change to orientation, size or format — and the new file isn't smaller, the program keeps the original file's contents. Once EXIF straightening, manual rotation, resizing or format conversion is involved, the output size simply depends on the result.

**会不会改动原文件？**

**Will it change my original files?**

简易模式的一键任务使用另存输出。高级模式可以选择原地修改，这会变更来源文件，处理重要图片前请保留备份。默认开启的「安全写入」会先暂存结果，再提交更改；关闭后按文件提交。取消任务不等于撤销所有已经提交的更改。

The one-click tasks in Simple mode always save copies. Advanced mode can modify files in place, which does change the source files — back up anything important first. "Safe write" is on by default: results are staged first and only committed at the end. With it off, each file is committed as it finishes. Cancelling a task does not undo changes that were already committed.

## 从源码运行 · Running from source

建议使用 Python 3.11，并确保安装了 Tk 支持。在项目目录中执行：

Python 3.11 is recommended; make sure Tk support is installed. From the project folder:

```powershell
python -m pip install -r requirements.txt
python 融合_v4ce.py
```

必需依赖为 Pillow 和 ttkbootstrap。`tkinterdnd2` 提供拖拽导入，`pillow-heif` 提供 HEIC / HEIF 支持；缺少可选依赖时，对应功能不可用，其余功能仍可使用。

Pillow and ttkbootstrap are required. `tkinterdnd2` adds drag-and-drop and `pillow-heif` adds HEIC / HEIF support; without them those features are switched off and everything else still works.

当前构建使用 Python 3.11.6。复现发布环境请使用 [固定构建依赖](requirements-build.txt)，具体步骤见 [发布与重建](发布与重建.md)。

The released builds use Python 3.11.6. To reproduce the release environment, use the [pinned build dependencies](requirements-build.txt) and follow the [rebuild guide](发布与重建.md) (in Chinese).

## 命令行使用 · Command line

命令行适合重复处理或接入脚本。下面的示例将图片按比例缩小至长边不超过 1920 像素，保留文件名，并另存到导出目录。

The command line is handy for repeated jobs or for plugging into scripts. The example below scales images down so the long edge is at most 1920 px, keeps the file names, and saves the results to an export folder.

先预览处理计划，不写入图片：

Preview the plan first — nothing is written:

```powershell
python -m imgwb plan --source "D:\照片\旅行" --output new --dest "D:\导出" --name keep --max-edge 1920
```

确认计划后执行：

Happy with the plan? Run it:

```powershell
python -m imgwb run --source "D:\照片\旅行" --output new --dest "D:\导出" --name keep --max-edge 1920
```

程序会在导出位置下按来源文件夹建立输出目录，以上示例为 `D:\导出\旅行_已处理`。

An output folder named after the source is created under the export location. On the command line the default suffix is `_已处理`, so the example above writes to `D:\导出\旅行_已处理`; add `--dir-suffix _processed` if you'd like an English name.

查看格式支持和完整参数：

To see the supported formats and every option:

```powershell
python -m imgwb capabilities
python -m imgwb run --help
```

**命令行默认使用原地处理和顺序编号。** 需要另存并保留文件名时，请像示例一样显式指定 `--output new`、`--dest` 和 `--name keep`。

**By default the command line works in place and renames files to sequential numbers.** To save copies and keep the names, spell it out as in the example: `--output new`, `--dest` and `--name keep`.

发布版 EXE 也支持命令行。它没有控制台窗口，请用 `--cli-out` 将结果写入文件：

The released EXE has the same command line. It has no console window, so write the result to a file with `--cli-out`:

```powershell
.\ImageWorkbenchV4ce.exe --cli plan --source "D:\照片\旅行" --output new --dest "D:\导出" --name keep --max-edge 1920 --json --cli-out "处理计划.json"
```

## 开发与反馈 · Development and feedback

- [代码导读](代码导读.md)：模块分工、任务处理流程及扩展入口。<br>[Code tour](代码导读.md): what each module does, how a task flows through the program, and where to extend it.
- [更新记录](CHANGELOG.md)：功能调整与问题修复。<br>[Changelog](CHANGELOG.md): feature changes and fixes.
- [发布与重建](发布与重建.md)：构建步骤、源码材料和替换库方法。<br>[Release and rebuild](发布与重建.md): build steps, source materials and how to swap in a modified library.
- [Python API](imgwb/api.py)：无界面的 `plan`、`run` 和 `capabilities` 接口。<br>[Python API](imgwb/api.py): the headless `plan`, `run` and `capabilities` functions.

运行回归测试：

Run the regression tests with:

```powershell
python tests/run_all.py
```

报告问题时，请说明所用版本、图片格式、处理选项、复现步骤和实际结果，并附上相关错误提示。能提供最小示例图片时，会更容易定位问题。

When reporting a problem, please include the version, the image format, the options you used, the steps to reproduce it and what actually happened, along with any error message. A minimal sample image makes things much easier to track down.

## 许可 · License

本项目**自有源码采用 [MIT 许可证](LICENSE)**，允许在保留许可及版权声明的条件下使用、修改和分发。

The project's **own source code is under the [MIT license](LICENSE)**: you may use, modify and redistribute it as long as the license and copyright notice are kept.

**当前包含 HEIC 支持的组合程序按 GPLv3 条件分发**，第三方组件保留各自的许可与版权声明。应用源码的 MIT 许可不代表发布包内所有组件都采用 MIT。

**The current combined program, which includes HEIC support, is distributed under the terms of GPLv3**, and third-party components keep their own licenses and copyright notices. The MIT license on the application source does not mean every component in the release package is MIT.

发布附件同时提供应用源码、第三方对应源码和许可材料。详细许可范围、组件来源及分发要求见 [第三方许可声明](第三方许可声明.md)、[发布与重建](发布与重建.md) 和 [licenses](licenses/)。

Each release provides the application source, the corresponding third-party sources and the license materials. For the exact scope, where each component comes from and the redistribution requirements, see the [third-party license notice](第三方许可声明.md), the [rebuild guide](发布与重建.md) and the [licenses](licenses/) folder.
