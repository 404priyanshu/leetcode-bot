"""Account paths and identity binding; never share cookies or progress."""
import json
import os
from pathlib import Path
import re

BASE = Path(__file__).resolve().parent


def storage_root():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'leetcode-bot'
    return BASE


def validate_name(name):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name) or name in {'con', 'prn', 'aux', 'nul'} or re.fullmatch(r'(com|lpt)[1-9]', name):
        raise ValueError('Account names must be safe lowercase names, 1–32 characters (letters, digits, _ or -).')
    return name


def paths(name):
    validate_name(name)
    root = storage_root()
    default = name == 'default'
    data = BASE if default else root / 'accounts' / name
    profile = (root / 'profile' if os.name == 'nt' else BASE / 'leetcode_profile') if default else data / 'profile'
    return dict(data=data, profile=profile,
                identity=root / 'default-account.json' if default else data / 'account.json',
                lock=(root / '.leetcode-bot.lock' if os.name == 'nt' else BASE / '.bot.lock') if default else data / '.bot.lock')


def list_accounts():
    """Known accounts: the original one first, then registered named accounts."""
    root = storage_root() / 'accounts'
    named = []
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not (entry / 'account.json').is_file():
                continue
            try:
                named.append(validate_name(entry.name))
            except ValueError:
                continue  # a stray directory is not an account
    return ['default'] + named


def expected_username(name):
    path = paths(name)['identity']
    if not path.exists():
        if name == 'default':
            return None
        raise ValueError(f'Unknown account {name}; run --account {name} --setup first.')
    identity = json.loads(path.read_text(encoding='utf-8'))
    value = identity.get('username') if isinstance(identity, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Invalid identity for {name}; run --account {name} --setup.')
    return value


def register(name, username):
    username = username.strip()
    if not username or '@' in username or any(c.isspace() for c in username):
        raise ValueError('Enter the LeetCode username, not an email address or display name.')
    path = paths(name)['identity']
    if path.exists() and expected_username(name).casefold() != username.casefold():
        raise ValueError('This account name is bound to another username. Use a new account name to keep history separate.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'username': username}, indent=2), encoding='utf-8')
    temporary.replace(path)


def check_identity(name, username):
    if not isinstance(username, str) or not username:
        raise RuntimeError('LeetCode did not return an authenticated username; stopping before submission.')
    expected = expected_username(name)
    if expected is None:
        register(name, username)
    elif expected.casefold() != username.casefold():
        raise RuntimeError(f'Account {name} expects {expected}, but Chrome is logged in as {username}. Run --account {name} --setup.')
