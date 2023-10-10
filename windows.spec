# -*- mode: python ; coding: utf-8 -*-

def collect_pkg_data(package, include_py_files=False, subdir=None):
    """
    Collect all data files from the given package.
    """
    import os
    from PyInstaller.utils.hooks import get_package_paths, remove_prefix, PY_IGNORE_EXTENSIONS

    # Accept only strings as packages.
    if type(package) is not str:
        raise ValueError

    pkg_base, pkg_dir = get_package_paths(package)
    if subdir:
        pkg_dir = os.path.join(pkg_dir, subdir)
    # Walk through all file in the given package, looking for data files.
    data_toc = TOC()
    for dir_path, dir_names, files in os.walk(pkg_dir):
        for f in files:
            extension = os.path.splitext(f)[1]
            if include_py_files or (extension not in PY_IGNORE_EXTENSIONS):
                source_file = os.path.join(dir_path, f)
                dest_folder = remove_prefix(dir_path, os.path.dirname(pkg_base) + os.sep)
                dest_file = os.path.join(dest_folder, f)
                data_toc.append((dest_file, source_file, 'DATA'))
    return data_toc


def collect_local_submodules(package):
    """
    Collect all local submodules from the given package.
    """
    import os
    base_dir = '..'
    package_dir = os.path.join(base_dir, package.replace('.', os.sep))
    submodules = []
    for dir_path, dir_names, files in os.walk(package_dir):
        for f in files:
            if f == '__init__.py':
                submodules.append(f"{package}.{os.path.basename(dir_path)}")
            elif f.endswith('.py'):
                submodules.append(f"{package}.{os.path.basename(dir_path)}.{os.path.splitext(f)[0]}")
        for d in dir_names:
            submodules.append(f"{package}.{os.path.basename(dir_path)}.{d}")
    return submodules


hiddenimports = [
                    'passlib.handlers.bcrypt',
                    'app.modules',
                    'app.plugins',
                ] + collect_local_submodules('app.modules') \
                + collect_local_submodules('app.plugins')

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
    a.datas,
    collect_pkg_data('config'),
    collect_pkg_data('cf_clearance'),
    collect_pkg_data('database', include_py_files=True),
    [],
    name='MoviePilot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="app.ico"
)
