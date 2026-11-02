# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Church Translate standalone Windows executable.

This build is intended to be self-contained for deployment:
  - bundles Python runtime + dependencies into the executable
  - includes app assets, scripts, and config files
  - produces a single-file .exe for end users

Build with:
    pyinstaller church-translate.spec --distpath dist --noconfirm
"""

block_cipher = None

# NOTE: PyInstaller will bundle the Python runtime and all imported packages into
# the resulting executable, so the app can run on a target Windows machine
# without requiring a separate Python install.
a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('app', 'app'),
        ('scripts', 'scripts'),
        ('config', 'config'),
        ('.env.example', '.'),
        ('version.txt', '.'),
        ('QUICKSTART.pdf', '.'),
    ],
    hiddenimports=[
        'flask',
        'flask_sock',
        'requests',
        'numpy',
        'soundfile',
        'anthropic',
        'httpx',
        'websockets',
        'websockets.sync.client',
        'websockets.sync',
        'websockets.client',
        'ssl',
        'charset_normalizer',
        'idna',
        'certifi',
        # Imported only by files under scripts/ (shipped as data, never analysed):
        # witness_align.py lines an extra ear's text up with a caption.
        'difflib',
        'unicodedata',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludedimports=[
        # Exclude test/dev dependencies to keep size down
        'pytest',
        'black',
        'isort',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='church-translate',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# Uncomment to also create a windowed version (no console):
# exe_windowed = EXE(
#     pyz,
#     a.scripts,
#     a.binaries,
#     a.zipfiles,
#     a.datas,
#     [],
#     name='church-translate-windowed',
#     debug=False,
#     console=False,
# )
