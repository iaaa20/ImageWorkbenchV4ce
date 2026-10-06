"""生成仅用于本地交付的应用源码快照；第三方大体积源码另行交付。

采用明确目录/文件清单，避免带入日志、开发过程材料、缓存或真实图片。
不联网、不上传、不覆盖已有同名归档。
"""
import hashlib
import io
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent


def main():
    names = {
        '融合_v4ce.py', '完整自检.py', '检查打包许可.py', '检查翻译覆盖.py',
        '检查图片格式.py', '生成源码包.py', 'ImageWorkbenchV4ce.spec',
        'README.md', 'README.en.md', 'CHANGELOG.md', '代码导读.md', '第三方许可声明.md',
        '发布与重建.md', 'LICENSE', 'requirements.txt', 'requirements-build.txt',
        'logo.ico', '发布附件/依赖源码/清单.json',
    }
    for directory in ['imgwb', 'tests']:
        names.update(p.relative_to(ROOT).as_posix()
                     for p in (ROOT / directory).rglob('*.py')
                     if '__pycache__' not in p.parts)
    names.update(p.relative_to(ROOT).as_posix()
                 for p in (ROOT / 'licenses').iterdir() if p.is_file())
    contents = {}
    for name in sorted(names):
        p = ROOT / name
        if p.is_symlink() or not p.resolve().is_relative_to(ROOT):
            raise ValueError(f'拒绝打包链接或项目外文件: {name}')
        contents[name] = p.read_bytes()
    manifest = {name: hashlib.sha256(data).hexdigest()
                for name, data in contents.items()}
    raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()[:16]
    contents['源码清单.json'] = raw
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    data = buffer.getvalue()
    dest = ROOT / '发布附件' / f'应用源码-{digest}.zip'
    dest.parent.mkdir(exist_ok=True)
    if dest.exists():
        if dest.read_bytes() != data:
            raise ValueError(f'已有归档内容不同，保留原文件: {dest}')
    else:
        with dest.open('xb') as stream:
            stream.write(data)
    checksum = hashlib.sha256(data).hexdigest()
    check = dest.with_suffix('.zip.sha256')
    line = f'{checksum}  {dest.name}\n'.encode('utf-8')
    if check.exists():
        if check.read_bytes() != line:
            raise ValueError(f'已有校验文件内容不同，保留原文件: {check}')
    else:
        with check.open('xb') as stream:
            stream.write(line)
    print(f'已生成/核对: {dest.name}，{len(manifest)} 个文件')
    print(f'SHA-256: {checksum}')
    print('仅含应用源码材料；第三方源码完整性与发布授权须按《发布与重建.md》另行核对。')


if __name__ == '__main__':
    main()
