"""PyInstaller打包配置文件"""
import sys
import os

sys.setrecursionlimit(5000)

block_cipher = None

ROOT = os.path.dirname(os.path.abspath(sys.argv[0]))
if 'pyinstaller' in ROOT.lower() or 'scripts' in ROOT.lower():
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))

a = Analysis(
    ['main.py'],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, 'app'), 'app'),
        (os.path.join(ROOT, 'data'), 'data'),
    ],
    hiddenimports=[
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebChannel',
        'PySide6.QtNetwork',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='历史版图',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='历史版图',
)
