"""Locate the user's installed ENet library; never download or vendor game files."""
import os
from pathlib import Path
import re


def find_enet():
    explicit = os.environ.get('ROBRUN_ENET_DLL')
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError('ROBRUN_ENET_DLL does not point to an installed DLL')
        return path.resolve()
    roots = []
    if os.name == 'nt':
        import winreg
        for hive, key, name in [(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath')]:
            try:
                with winreg.OpenKey(hive, key) as handle:
                    roots.append(Path(winreg.QueryValueEx(handle, name)[0]))
            except OSError:
                pass
    roots.append(Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Steam')
    libraries = list(roots)
    for root in roots:
        folders = root / 'steamapps/libraryfolders.vdf'
        if folders.is_file():
            text = folders.read_text(encoding='utf-8', errors='replace')
            libraries.extend(Path(value.replace('\\\\', '\\')) for value in re.findall(r'"path"\s+"([^"]+)"', text))
    for library in libraries:
        candidate = library / 'steamapps/common/Rob Run/ROB RUN_Data/Plugins/x86_64/enet.dll'
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError('ROB RUN ENet library not found. Install the game or set ROBRUN_ENET_DLL.')
