# -*- mode: python ; coding: utf-8 -*-

def collect_pkg_data(package: str, include_py_files: bool = False, subdir: str = None):
    """
    Collect all data files from the given package.
    """
    from pathlib import Path
    from PyInstaller.utils.hooks import get_package_paths, PY_IGNORE_EXTENSIONS
    from PyInstaller.building.datastruct import TOC

    data_toc = TOC()

    # Accept only strings as packages.
    if type(package) is not str:
        raise ValueError
    try:
        pkg_base, pkg_dir = get_package_paths(package)
    except ValueError:
        return data_toc
    if subdir:
        pkg_path = Path(pkg_dir) / subdir
    else:
        pkg_path = Path(pkg_dir)
    # Walk through all file in the given package, looking for data files.
    if not pkg_path.exists():
        return data_toc
    for file in pkg_path.rglob('*'):
        if file.is_file():
            extension = file.suffix
            if not include_py_files and (extension in PY_IGNORE_EXTENSIONS):
                continue
            data_toc.append((str(file.relative_to(pkg_base)), str(file), 'DATA'))
    return data_toc


def collect_local_submodules(package: str):
    """
    Collect all local submodules from the given package.
    """
    import os
    from pathlib import Path
    package_dir = Path(package.replace('.', os.sep))
    submodules = [package]
    # Walk through all file in the given package, looking for data files.
    if not package_dir.exists():
        return []
    for file in package_dir.rglob('*.py'):
        if file.name == '__init__.py':
            module = f"{file.parent}".replace(os.sep, '.')
        else:
            module = f"{file.parent}.{file.stem}".replace(os.sep, '.')
        if module not in submodules:
            submodules.append(module)
    return submodules


hiddenimports = [
                    'passlib.handlers.bcrypt',
                    'app.modules',
                    'app.plugins',
                ] + collect_local_submodules('app.modules') + collect_local_submodules('app.plugins')

block_cipher = None

a = Analysis(
    ['app/main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas + [('./app.ico', './app.ico', 'DATA')],
    collect_pkg_data('config'),
    collect_pkg_data('nginx'),
    collect_pkg_data('cf_clearance'),
    collect_pkg_data('zhconv'),
    collect_pkg_data('cn2an'),
    collect_pkg_data('Pinyin2Hanzi'),
    collect_pkg_data('database', include_py_files=True),
    collect_pkg_data('app.helper'),
    [],
    name='MoviePilot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="app.ico"
)
