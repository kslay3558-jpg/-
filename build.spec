# PyInstaller spec file for IRQ Optimizer (Ultimate Safe Edition)
#
# Build command (run in an Administrator PowerShell or CMD in this directory):
#
#   pip install pyinstaller customtkinter
#   pyinstaller build.spec
#
# Output: dist\IRQOptimizer.exe  (single file, no console, UAC admin manifest)
# ──────────────────────────────────────────────────────────────────────────────

import os
import customtkinter

# Locate the customtkinter package directory so its image assets are bundled.
_ctk_dir = os.path.dirname(customtkinter.__file__)

block_cipher = None

a = Analysis(
    ["irq_optimizer_ultimate_safe.py"],
    pathex=[],
    binaries=[],
    datas=[
        # Bundle customtkinter assets (themes, images)
        (_ctk_dir, "customtkinter"),
    ],
    hiddenimports=[
        "customtkinter",
        "winreg",
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="IRQOptimizer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    # ── Single file, no console window ────────────────────────────────────
    onefile=True,
    console=False,
    # ── UAC: request Administrator on launch ──────────────────────────────
    uac_admin=True,
    # ── Optional: set icon (place app.ico next to this file before build) ─
    icon="app.ico" if os.path.exists("app.ico") else None,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version_file=None,
)
