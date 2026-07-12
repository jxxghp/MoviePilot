# -*- mode: python ; coding: utf-8 -*-
"""将站点适配采集器构建为不依赖本地 Python 的单文件程序。"""

from pathlib import Path


PROJECT_ROOT = Path(SPECPATH).resolve().parent
ENTRYPOINT = PROJECT_ROOT / "scripts" / "site_adapter_collector.py"
analysis = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="moviepilot-site-collector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
