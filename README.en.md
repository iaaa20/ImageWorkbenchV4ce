# Image Workbench V4ce

[中文](README.md) | **English**

A batch image tool for Windows. Use it to shrink photos, fix their orientation, resize them, give a whole folder tidy sequential names, or turn images into ICO icons. You can drag files or folders straight in, the interface comes in Chinese and English, and there's a command line if you'd rather script it.

**For everyday use, just grab the EXE — no Python needed.** The one-click tasks in Simple mode always save to a new folder and leave your originals alone.

## Download and run

Head to the **Releases** page and pick one of the two builds:

| File | How to use it |
| --- | --- |
| `ImageWorkbenchV4ce.exe` | Single-file build. Download it and double-click. |
| `ImageWorkbenchV4ce_dir.zip` | Folder build. Extract everything and run the EXE inside; keep the `_internal` folder next to it. |

Both are 64-bit Windows programs with the same features, so either one is fine. The source archives attached to each release are there for people who want to read, change or rebuild the program — you don't need them just to run it.

## Quick start

1. Open the program and pick a source folder with **Browse...**, or drag images or folders onto the window.
2. In Simple mode, click the one-click task you want. Processing starts right away.
3. When it's done, check the output folder. If anything failed, the log in the window tells you why.

For example, pick `D:\Photos\Trip` and run a one-click task: the results land in `D:\Photos\Trip_processed` (or `Trip_已处理` if the interface is in Chinese), and the source folder is untouched. To send the output somewhere else, or to combine several options, switch to Advanced mode.

## What it can do

| Task | Good for |
| --- | --- |
| Compress | Making files smaller for sending or uploading; file names stay the same |
| Auto-rotate | Straightening photos using their EXIF orientation. If a photo has no orientation data, pick a rotation angle yourself |
| Rotate left / right | Turning a whole batch 90° either way; Advanced mode also offers 180° |
| Number | Renaming a batch into one clean numbered sequence |
| Limit long edge | Scaling big images down proportionally, handy for web use; smaller images are never enlarged |
| To icon | Exporting ICO files, including multi-size icons from 16 to 256 px |

Advanced mode lets you mix rotation, compression, resizing and naming, and set a name prefix, starting number, number of digits, whether to scan subfolders, and how the output folders are laid out. You can also choose a performance mode or set the number of workers yourself.

## Supported formats

- **Input:** JPEG, PNG, BMP, WebP, TIFF, GIF.
- **HEIC / HEIF:** supported out of the box in the released builds. These are normally converted to JPG, or to ICO when you're making icons.
- **Icon output:** ICO. Note that ICO is not accepted as a batch *input* format.
- **Multi-frame images:** GIF, animated WebP, multi-page TIFF and APNG all work. Rotating or resizing is applied to every frame; when nothing needs to change, the file is usually copied as is. Re-encoding animations can be turned on in Advanced mode.

When running from source, HEIC / HEIF support depends on whether `pillow-heif` is installed. You can always ask the program what it supports with the `capabilities` command.

## Good to know before you start

**How does compression affect quality?**

The one-click Compress task uses the 85% setting. At that setting PNG files are quantized to at most 256 colours, which can cause visible banding — check the result first for photos, gradients or anything where colour detail matters. Re-saving a JPEG is never lossless, even at 100%. Auto-rotate re-saves at the highest quality, but that isn't lossless either.

**Why didn't some images get smaller?**

Images that have already been compressed often can't shrink much more. When you only compress — no change to orientation, size or format — and the new file isn't smaller, the program keeps the original file's contents. Once EXIF straightening, manual rotation, resizing or format conversion is involved, the output size simply depends on the result.

**Will it change my original files?**

The one-click tasks in Simple mode always save copies. Advanced mode can modify files in place, which does change the source files — back up anything important first. "Safe write" is on by default: results are staged first and only committed at the end. With it off, each file is committed as it finishes. Cancelling a task does not undo changes that were already committed.

## Running from source

Python 3.11 is recommended; make sure Tk support is installed. From the project folder:

```powershell
python -m pip install -r requirements.txt
python 融合_v4ce.py
```

Pillow and ttkbootstrap are required. `tkinterdnd2` adds drag-and-drop and `pillow-heif` adds HEIC / HEIF support; without them those features are switched off and everything else still works.

The released builds use Python 3.11.6. To reproduce the release environment, use the [pinned build dependencies](requirements-build.txt) and follow the [rebuild guide](发布与重建.md) (in Chinese).

## Command line

The command line is handy for repeated jobs or for plugging into scripts. The example below scales images down so the long edge is at most 1920 px, keeps the file names, and saves the results to an export folder.

Preview the plan first — nothing is written:

```powershell
python -m imgwb plan --source "D:\Photos\Trip" --output new --dest "D:\Export" --name keep --max-edge 1920
```

Happy with the plan? Run it:

```powershell
python -m imgwb run --source "D:\Photos\Trip" --output new --dest "D:\Export" --name keep --max-edge 1920
```

An output folder named after the source is created under the export location. On the command line the default suffix is `_已处理`, so the example above writes to `D:\Export\Trip_已处理`; add `--dir-suffix _processed` if you'd like an English name.

To see the supported formats and every option:

```powershell
python -m imgwb capabilities
python -m imgwb run --help
```

**By default the command line works in place and renames files to sequential numbers.** To save copies and keep the names, spell it out as in the example: `--output new`, `--dest` and `--name keep`.

The released EXE has the same command line. It has no console window, so write the result to a file with `--cli-out`:

```powershell
.\ImageWorkbenchV4ce.exe --cli plan --source "D:\Photos\Trip" --output new --dest "D:\Export" --name keep --max-edge 1920 --json --cli-out "plan.json"
```

## Development and feedback

These documents are currently in Chinese:

- [Code tour](代码导读.md): what each module does, how a task flows through the program, and where to extend it.
- [Changelog](CHANGELOG.md): feature changes and fixes.
- [Release and rebuild](发布与重建.md): build steps, source materials and how to swap in a modified library.
- [Python API](imgwb/api.py): the headless `plan`, `run` and `capabilities` functions.

Run the regression tests with:

```powershell
python tests/run_all.py
```

When reporting a problem, please include the version, the image format, the options you used, the steps to reproduce it and what actually happened, along with any error message. A minimal sample image makes things much easier to track down.

## License

The project's **own source code is under the [MIT license](LICENSE)**: you may use, modify and redistribute it as long as the license and copyright notice are kept.

**The current combined program, which includes HEIC support, is distributed under the terms of GPLv3**, and third-party components keep their own licenses and copyright notices. The MIT license on the application source does not mean every component in the release package is MIT.

Each release provides the application source, the corresponding third-party sources and the license materials. For the exact scope, where each component comes from and the redistribution requirements, see the [third-party license notice](第三方许可声明.md), the [rebuild guide](发布与重建.md) and the [licenses](licenses/) folder.
