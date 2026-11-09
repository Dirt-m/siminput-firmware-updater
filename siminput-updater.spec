import customtkinter
from pathlib import Path

ctk_path = Path(customtkinter.__path__[0])

a = Analysis(
    ["entry.py"],
    pathex=["src"],
    datas=[
        (str(ctk_path), "customtkinter"),
        # flash_nuke.uf2, CircuitPython and their notices, for switching back
        # to the stock firmware (switch.ASSETS).
        ("src/siminput_updater/assets", "siminput_updater/assets"),
    ],
    hiddenimports=["customtkinter"],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="siminput-updater",
    console=False,
    onefile=True,
)
