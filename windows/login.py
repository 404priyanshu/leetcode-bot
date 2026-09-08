"""Manual Windows login in installed Chrome, without an automation connection."""
import os
from pathlib import Path
import shutil
import subprocess


def find_chrome():
    candidates = []
    for variable in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe')
    on_path = shutil.which('chrome.exe')
    if on_path:
        candidates.append(Path(on_path))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError('Google Chrome was not found. Install Chrome, then run --setup again.')


def manual_login(profile, announce):
    chrome = find_chrome()
    profile.mkdir(parents=True, exist_ok=True)
    announce('Opening ordinary Chrome for manual login. Complete verification and login,')
    announce('then close every window belonging to this bot profile to finish setup.')
    announce('The bot will check your login on its next run; setup does not submit anything.')
    result = subprocess.run([
        str(chrome), f'--user-data-dir={profile}', '--new-window',
        '--window-size=1365,900', '--window-position=80,80',
        '--disable-background-mode', 'https://leetcode.com/accounts/login/',
    ], check=False)
    if result.returncode:
        raise RuntimeError(f'Chrome exited with code {result.returncode}; login was not verified.')
    announce(f'Chrome closed. Profile retained at {profile}. Login has not been verified.')
