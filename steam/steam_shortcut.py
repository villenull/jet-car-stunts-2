#!/usr/bin/env python3
"""Register Jet Car Stunts 2 as a non-Steam shortcut with library artwork.

Offline, fail-closed edits of one Steam account's ``shortcuts.vdf`` and
``grid/`` directory. Refuses while Steam runs, keeps unrelated entries and
fields byte-identical, backs up before writing, replaces atomically, rolls
back on failure and never writes compatibility-tool (Proton) mappings.
It never starts, stops or signals Steam. Python >= 3.9, stdlib only.

Exit codes: 0 ok, 2 usage/invalid path, 3 Steam running, 4 malformed or
unsupported Steam data, 5 ownership conflict, 6 I/O failure (rolled back),
7 install not complete.
"""
import argparse
import binascii
import datetime
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import binary_vdf as vdf  # noqa: E402

TITLE = 'Jet Car Stunts 2'
DEFAULT_TARGET = 'jet-car-stunts-2.sh'
DEFAULT_LAUNCH_OPTIONS = ''
MANIFEST_REL = os.path.join('state', 'steam-registration.json')
BACKUP_REL = os.path.join('state', 'steam-backup')
LOCK_REL = os.path.join('state', 'steam-registration.lock')
INSTALL_STATE = 'install-state.json'
ARTWORK_REL = os.path.join('steam', 'artwork')
STEAMID64_BASE = 76561197960265728
MAX_ART_BYTES = 64 * 1024 * 1024

# slot -> (source file, grid filename pattern, recommended size or None)
ARTWORK_SLOTS = (
    ('cover', 'cover.png', '{id}p.png', (600, 900)),
    ('landscape', 'landscape.png', '{id}.png', (920, 430)),
    ('hero', 'hero.png', '{id}_hero.png', (3840, 1240)),
    ('logo', 'logo.png', '{id}_logo.png', None),
    ('icon', 'icon.png', '{id}_icon.png', None),
)
GRID_EXTS = ('.png', '.jpg', '.jpeg', '.webp')
STEAM_PROCESS_NAMES = {'steam', 'steamwebhelper', 'steam.sh', 'steam-runtime-l'}


class RegistrationError(Exception):
    code = 6


class UsageError(RegistrationError):
    code = 2


class SteamRunning(RegistrationError):
    code = 3


class MalformedData(RegistrationError):
    code = 4


class Conflict(RegistrationError):
    code = 5


class InstallIncomplete(RegistrationError):
    code = 7


# ---------------------------------------------------------------- discovery

def find_steam_roots(home=None):
    """Existing Steam roots; flatpak installs are reported but unsupported."""
    home = home or os.path.expanduser('~')
    seen, roots = set(), []
    candidates = (
        (os.path.join(home, '.local', 'share', 'Steam'), 'native'),
        (os.path.join(home, '.steam', 'steam'), 'native'),
        (os.path.join(home, '.steam', 'root'), 'native'),
        (os.path.join(home, '.var', 'app', 'com.valvesoftware.Steam', '.local', 'share', 'Steam'),
         'flatpak-unsupported'),
    )
    for path, kind in candidates:
        if not os.path.isdir(os.path.join(path, 'userdata')):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        roots.append({'path': real, 'kind': kind})
    return roots


def default_steam_root(home=None):
    for root in find_steam_roots(home):
        if root['kind'] == 'native':
            return root['path']
    raise UsageError('no native Steam installation with userdata/ found')


def _read_text_vdf_users(path):
    """Minimal loginusers.vdf reader: {steamid64: {key: value}}; tolerant."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            text = handle.read()
    except OSError:
        return {}
    users, current, depth = {}, None, 0
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        token, brace = match.group(1), match.group(2)
        if brace == '{':
            depth += 1
            continue
        if brace == '}':
            depth -= 1
            if depth <= 1:
                current = None
            continue
        if depth == 1 and token.isdigit():
            current = users.setdefault(token, {})
            pending = None
            continue
        if depth == 2 and current is not None:
            if pending is None:
                pending = token
            else:
                current[pending.lower()] = token
                pending = None
    return users


def list_accounts(steam_root=None):
    steam_root = steam_root or default_steam_root()
    userdata = os.path.join(steam_root, 'userdata')
    users = _read_text_vdf_users(os.path.join(steam_root, 'config', 'loginusers.vdf'))
    accounts = []
    try:
        names = sorted(os.listdir(userdata))
    except OSError as exc:
        raise MalformedData('cannot read %s: %s' % (userdata, exc))
    for name in names:
        if not name.isdigit() or name == '0' or not os.path.isdir(os.path.join(userdata, name)):
            continue
        steamid64 = str(int(name) + STEAMID64_BASE)
        info = users.get(steamid64, {})
        accounts.append({
            'account_id': name,
            'steamid64': steamid64,
            'persona_name': info.get('personaname'),
            'most_recent': info.get('mostrecent') == '1',
            'shortcuts_exists': os.path.isfile(
                os.path.join(userdata, name, 'config', 'shortcuts.vdf')),
        })
    return accounts


def steam_running(steam_root=None, proc_root='/proc'):
    """Processes that look like a Steam client; empty means closed."""
    found = []
    real_root = os.path.realpath(steam_root) if steam_root else None
    own = os.getpid()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit() or int(entry) == own:
            continue
        base = os.path.join(proc_root, entry)
        try:
            with open(os.path.join(base, 'comm'), 'r', errors='replace') as handle:
                comm = handle.read().strip()
        except OSError:
            continue
        exe = ''
        try:
            exe = os.readlink(os.path.join(base, 'exe'))
        except OSError:
            pass
        in_root = bool(real_root and exe and (
            exe.startswith(os.path.join(real_root, 'ubuntu12_32') + os.sep)
            or exe.startswith(os.path.join(real_root, 'ubuntu12_64') + os.sep)))
        if comm in STEAM_PROCESS_NAMES or in_root:
            found.append({'pid': int(entry), 'name': comm})
    return found


# ------------------------------------------------------------ path helpers

def _check_text(value, what):
    if not isinstance(value, str):
        raise UsageError('%s must be text' % what)
    if any(ch in value for ch in ('\x00', '\n', '\r', '"')):
        raise UsageError('%s must not contain NUL, newline or double quote: %r' % (what, value))
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise UsageError('%s is not valid UTF-8' % what)


def _resolve_install(install_root, launch_target):
    _check_text(install_root, 'install root')
    if not os.path.isabs(install_root):
        raise UsageError('install root must be absolute')
    root = os.path.normpath(install_root)
    if not os.path.isdir(root):
        raise UsageError('install root is not a directory: %s' % root)
    _check_text(launch_target, 'launch target')
    if os.path.isabs(launch_target) or '..' in launch_target.split('/'):
        raise UsageError('launch target must be relative to the install root')
    target = os.path.normpath(os.path.join(root, launch_target))
    real_root = os.path.realpath(root)
    real_target = os.path.realpath(target)
    if os.path.commonpath([real_root, real_target]) != real_root:
        raise UsageError('launch target resolves outside the install root')
    try:
        mode = os.stat(real_target).st_mode
    except OSError:
        raise UsageError('launch target does not exist: %s' % target)
    if not stat.S_ISREG(mode):
        raise UsageError('launch target is not a regular file: %s' % target)
    if not os.access(real_target, os.X_OK):
        raise UsageError('launch target is not executable: %s' % target)
    return root, target


def _check_launch_options(value):
    if value is None:
        return
    if any(ch in value for ch in ('\x00', '\n', '\r')):
        raise UsageError('launch options must not contain NUL or newlines')


def shortcut_appid(exe_quoted, app_name):
    """Deterministic non-Steam shortcut id used for newly created entries."""
    return (binascii.crc32((exe_quoted + app_name).encode('utf-8')) & 0xFFFFFFFF) | 0x80000000


def shortcut_id64(appid):
    return str((appid << 32) | 0x02000000)


def _unquote(value):
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _same_path(stored, target):
    path = _unquote(stored)
    if not path:
        return False
    if os.path.normpath(path) == os.path.normpath(target):
        return True
    try:
        return os.path.realpath(path) == os.path.realpath(target)
    except (OSError, ValueError):
        return False


def _decode(value):
    return value.decode('utf-8', 'surrogateescape') if value is not None else None


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path, data, mode=0o644):
    directory = os.path.dirname(path)
    tmp = os.path.join(directory, '.%s.jcs2-tmp-%d' % (os.path.basename(path), os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def _replace(src, dst):
    # Indirection point for fault-injection tests.
    os.replace(src, dst)


def png_size(path):
    with open(path, 'rb') as handle:
        head = handle.read(33)
    if len(head) < 33 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        return None
    return struct.unpack('>II', head[16:24])


# -------------------------------------------------------------- Steam data

class Account(object):
    def __init__(self, steam_root, account_id):
        _check_text(steam_root, 'Steam root')
        if not isinstance(account_id, str) or not account_id.isdigit() or account_id == '0':
            raise UsageError('account id must be a positive decimal Steam account id')
        self.steam_root = os.path.realpath(steam_root)
        self.account_id = account_id
        self.user_dir = os.path.join(self.steam_root, 'userdata', account_id)
        self.config_dir = os.path.join(self.user_dir, 'config')
        self.shortcuts = os.path.join(self.config_dir, 'shortcuts.vdf')
        self.grid = os.path.join(self.config_dir, 'grid')
        if not os.path.isdir(self.user_dir):
            raise UsageError('Steam account %s not found under %s' % (account_id, self.steam_root))
        for path in (self.user_dir, self.config_dir):
            if os.path.islink(path):
                raise MalformedData('refusing symlinked Steam directory: %s' % path)
        if not os.path.isdir(self.config_dir):
            raise MalformedData('account has no config directory: %s' % self.config_dir)
        for path in (self.shortcuts, self.grid):
            if os.path.islink(path):
                raise MalformedData('refusing symlinked Steam path: %s' % path)
        if os.path.exists(self.grid) and not os.path.isdir(self.grid):
            raise MalformedData('grid path is not a directory: %s' % self.grid)

    @property
    def key(self):
        return '%s|%s' % (self.steam_root, self.account_id)

    def read_shortcuts(self):
        """Return (raw bytes or None, parsed top nodes, shortcuts map node)."""
        if not os.path.exists(self.shortcuts):
            nodes = [vdf.Node(vdf.T_MAP, b'shortcuts', [])]
            return None, nodes, nodes[0]
        if not os.path.isfile(self.shortcuts):
            raise MalformedData('shortcuts.vdf is not a regular file')
        with open(self.shortcuts, 'rb') as handle:
            raw = handle.read()
        try:
            nodes = vdf.parse(raw)
        except vdf.VdfError as exc:
            raise MalformedData('shortcuts.vdf is malformed: %s' % exc)
        if len(nodes) != 1 or nodes[0].type != vdf.T_MAP or nodes[0].key.lower() != b'shortcuts':
            raise MalformedData('shortcuts.vdf does not contain exactly one "shortcuts" map')
        for index, entry in enumerate(nodes[0].value):
            if entry.type != vdf.T_MAP:
                raise MalformedData('shortcut child %r is not a map' % entry.key)
            if entry.key != str(index).encode():
                raise MalformedData('shortcut keys are not sequential (found %r at %d)'
                                    % (entry.key, index))
            appid = vdf.find(entry.value, 'appid')
            if appid is not None and appid.type != vdf.T_INT32:
                raise MalformedData('shortcut %d has non-int32 appid' % index)
            for name in ('AppName', 'Exe', 'StartDir', 'icon', 'LaunchOptions'):
                field = vdf.find(entry.value, name)
                if field is not None and field.type != vdf.T_STRING:
                    raise MalformedData('shortcut %d field %s is not a string' % (index, name))
        return raw, nodes, nodes[0]

    def proton_override(self, appid):
        """True if config.vdf maps this shortcut to a compatibility tool (read-only)."""
        path = os.path.join(self.steam_root, 'config', 'config.vdf')
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                text = handle.read()
        except OSError:
            return False
        block = re.search(r'"CompatToolMapping"\s*\{', text, re.IGNORECASE)
        if not block:
            return False
        depth, pos = 1, block.end()
        while depth and pos < len(text):
            char = text[pos]
            depth += char == '{'
            depth -= char == '}'
            pos += 1
        section = text[block.end():pos]
        return re.search(r'"%d"\s*\{' % appid, section) is not None


def _entry_appid(entry):
    return vdf.uint32_of(vdf.find(entry.value, 'appid'))


def _entry_str(entry, name):
    return _decode(vdf.string(entry.value, name))


def _set_string(entry, name, value):
    node = vdf.find(entry.value, name)
    encoded = value.encode('utf-8')
    if node is None:
        entry.value.append(vdf.Node(vdf.T_STRING, name.encode(), encoded))
        return True
    if node.value == encoded:
        return False
    node.value = encoded
    return True


def _new_entry(index, appid, exe, start_dir, launch_options):
    s = lambda k, v: vdf.Node(vdf.T_STRING, k.encode(), v.encode('utf-8'))
    i = lambda k, v: vdf.Node(vdf.T_INT32, k.encode(), vdf.int32(v))
    return vdf.Node(vdf.T_MAP, str(index).encode(), [
        i('appid', appid), s('AppName', TITLE), s('Exe', exe), s('StartDir', start_dir),
        s('icon', ''), s('ShortcutPath', ''), s('LaunchOptions', launch_options),
        i('IsHidden', 0), i('AllowDesktopConfig', 1), i('AllowOverlay', 1), i('OpenVR', 0),
        i('Devkit', 0), s('DevkitGameID', ''), i('DevkitOverrideAppID', 0),
        i('LastPlayTime', 0), s('FlatpakAppID', ''), s('sortas', ''),
        vdf.Node(vdf.T_MAP, b'tags', []),
    ])


# ------------------------------------------------------------------ manifest

def _load_manifest(root):
    path = os.path.join(root, MANIFEST_REL)
    if not os.path.exists(path):
        return path, {'schema': 1, 'registrations': {}}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        if not isinstance(data.get('registrations'), dict):
            raise ValueError('registrations missing')
    except (OSError, ValueError) as exc:
        raise MalformedData('registration manifest unreadable (%s): %s' % (path, exc))
    return path, data


def _check_install_complete(root):
    path = os.path.join(root, INSTALL_STATE)
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            state = json.load(handle)
    except (OSError, ValueError) as exc:
        raise InstallIncomplete('install state missing or unreadable (%s): %s' % (path, exc))
    if not isinstance(state, dict) or state.get('status') != 'complete':
        raise InstallIncomplete('install state is not complete: %s' % path)


class _Lock(object):
    def __init__(self, root):
        state = os.path.join(root, 'state')
        if not os.path.isdir(state):
            raise UsageError('install state directory missing: %s' % state)
        self.path = os.path.join(root, LOCK_REL)
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise Conflict('another Steam registration is in progress')
            raise
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


# ------------------------------------------------------------------ planning

def _locate_owned(shortcuts, target, record):
    """Find our entry index by exact target; manifest appid must agree, never alone."""
    matches = [i for i, e in enumerate(shortcuts.value)
               if _entry_str(e, 'Exe') is not None and _same_path(_entry_str(e, 'Exe'), target)]
    if len(matches) > 1:
        raise Conflict('%d shortcuts already target %s; remove duplicates in Steam first'
                       % (len(matches), target))
    if record:
        rec_appid = record.get('appid')
        rec_matches = [i for i, e in enumerate(shortcuts.value) if _entry_appid(e) == rec_appid]
        for i in rec_matches:
            if i not in matches:
                exe = _entry_str(shortcuts.value[i], 'Exe') or ''
                if not _same_path(exe, record.get('target', '')):
                    raise Conflict('recorded appid %d now belongs to a different shortcut; '
                                   'not taking it over' % rec_appid)
                # Our old entry, but the install target moved: still ours.
                if matches:
                    raise Conflict('both a previous and a current Jet Car Stunts 2 entry exist')
                matches = [i]
    return matches[0] if matches else None


def _plan(account, root, target, launch_options, artwork_dir, record):
    raw, nodes, shortcuts = account.read_shortcuts()
    exe = '"%s"' % target
    start_dir = '"%s/"' % root.rstrip('/')
    warnings = []
    index = _locate_owned(shortcuts, target, record)
    if index is None:
        appid = shortcut_appid(exe, TITLE)
        for entry in shortcuts.value:
            if _entry_appid(entry) == appid:
                raise Conflict('shortcut appid %d is already used by another shortcut' % appid)
        if any(_entry_str(e, 'AppName') == TITLE for e in shortcuts.value):
            warnings.append('another shortcut named %r (different target) exists and was kept'
                            % TITLE)
        action = 'create'
        entry = _new_entry(len(shortcuts.value), appid, exe, start_dir,
                           launch_options if launch_options is not None else DEFAULT_LAUNCH_OPTIONS)
        shortcuts.value.append(entry)
        changed = True
    else:
        entry = shortcuts.value[index]
        appid = _entry_appid(entry)
        changed = False
        if appid is None:
            appid = shortcut_appid(_entry_str(entry, 'Exe'), _entry_str(entry, 'AppName') or '')
            entry.value.insert(0, vdf.Node(vdf.T_INT32, b'appid', vdf.int32(appid)))
            changed = True
            warnings.append('existing entry had no appid; stored the legacy computed id')
        if record and record.get('appid') not in (None, appid):
            raise Conflict('entry for this target has appid %d but manifest recorded %d'
                           % (appid, record.get('appid')))
        changed |= _set_string(entry, 'Exe', exe)
        changed |= _set_string(entry, 'StartDir', start_dir)
        name = _entry_str(entry, 'AppName')
        if name is None:
            changed |= _set_string(entry, 'AppName', TITLE)
        elif name != TITLE:
            warnings.append('kept user-chosen shortcut name %r' % name)
        current_opts = _entry_str(entry, 'LaunchOptions')
        if launch_options is not None and current_opts != launch_options:
            if current_opts in (None, '') or (record and current_opts == record.get('launch_options')):
                changed |= _set_string(entry, 'LaunchOptions', launch_options)
            else:
                warnings.append('kept user-edited launch options')
        action = 'update' if changed else 'noop'

    art = _plan_artwork(account, appid, artwork_dir, record, warnings)
    icon_value = None
    icon = next((a for a in art if a['slot'] == 'icon'), None)
    if icon and icon['status'] in ('install', 'unchanged'):
        current_icon = _entry_str(entry, 'icon') or ''
        recorded_icon = (record or {}).get('icon_field')
        if current_icon in ('', icon['dest'], recorded_icon):
            icon_value = icon['dest']
            if current_icon != icon_value:
                _set_string(entry, 'icon', icon_value)
                if action == 'noop':
                    action = 'update'
        else:
            warnings.append('kept user-chosen shortcut icon')
    if action in ('create', 'update'):
        changed = True
    if account.proton_override(appid):
        warnings.append('a compatibility tool (Proton) is mapped to this shortcut in Steam; '
                        'remove it in shortcut Properties > Compatibility')
    return {
        'raw': raw, 'nodes': nodes, 'changed': changed, 'action': action, 'appid': appid,
        'exe': exe, 'start_dir': start_dir, 'launch_options': _entry_str(entry, 'LaunchOptions'),
        'art': art, 'icon_field': icon_value, 'warnings': warnings,
    }


def _existing_grid_files(account, stem):
    return [os.path.join(account.grid, stem + ext) for ext in GRID_EXTS
            if os.path.lexists(os.path.join(account.grid, stem + ext))]


def _plan_artwork(account, appid, artwork_dir, record, warnings):
    owned = (record or {}).get('artwork', {}) if (record or {}).get('appid') == appid else {}
    plans = []
    for slot, source_name, pattern, size in ARTWORK_SLOTS:
        dest = os.path.join(account.grid, pattern.format(id=appid))
        stem = os.path.splitext(os.path.basename(dest))[0]
        source = os.path.join(artwork_dir, source_name)
        item = {'slot': slot, 'source': source, 'dest': dest, 'status': None, 'sha256': None}
        plans.append(item)
        if not os.path.isfile(source):
            item['status'] = 'missing-source'
            warnings.append('artwork %s not supplied (%s); skipped' % (slot, source_name))
            continue
        if os.path.getsize(source) > MAX_ART_BYTES or png_size(source) is None:
            item['status'] = 'invalid-source'
            warnings.append('artwork %s is not a usable PNG; skipped' % slot)
            continue
        dims = png_size(source)
        if size and tuple(dims) != size:
            warnings.append('artwork %s is %dx%d; recommended %dx%d'
                            % ((slot,) + tuple(dims) + size))
        item['sha256'] = _sha256(source)
        existing = _existing_grid_files(account, stem)
        ours = owned.get(slot, {})
        if not existing:
            item['status'] = 'install'
        elif existing == [dest] and not os.path.islink(dest) and os.path.isfile(dest):
            current = _sha256(dest)
            if current == item['sha256']:
                item['status'] = 'unchanged'
            elif ours.get('sha256') == current:
                item['status'] = 'install'
            else:
                item['status'] = 'preserved-user'
        else:
            item['status'] = 'preserved-user'
        if item['status'] == 'preserved-user':
            warnings.append('kept existing %s artwork for this shortcut' % slot)
    return plans


# --------------------------------------------------------------- operations

def _result(action, account, appid, root, **extra):
    out = {'ok': True, 'action': action, 'appid': appid,
           'shortcut_id64': shortcut_id64(appid) if appid is not None else None,
           'account_id': account.account_id, 'shortcuts_vdf': account.shortcuts,
           'backup_dir': None, 'manifest': os.path.join(root, MANIFEST_REL),
           'artwork': {}, 'warnings': []}
    out.update(extra)
    return out


def _art_report(art):
    status_map = {'install': 'installed', 'unchanged': 'unchanged'}
    return {a['slot']: {'status': status_map.get(a['status'], a['status']),
                        'path': a['dest'] if a['status'] in status_map else None}
            for a in art}


def _require_closed(account, proc_root):
    running = steam_running(account.steam_root, proc_root)
    if running:
        raise SteamRunning('Steam is running (%s); exit Steam completely and retry'
                           % ', '.join('%s[%d]' % (p['name'], p['pid']) for p in running))


def plan(install_root, account_id, steam_root=None, launch_target=DEFAULT_TARGET,
         launch_options=DEFAULT_LAUNCH_OPTIONS, artwork_dir=None, proc_root='/proc'):
    """Dry run: report what register() would do. Never writes; Steam may run."""
    root, target = _resolve_install(install_root, launch_target)
    _check_launch_options(launch_options)
    account = Account(steam_root or default_steam_root(), account_id)
    _, manifest = _load_manifest(root)
    record = manifest['registrations'].get(account.key)
    p = _plan(account, root, target, launch_options, artwork_dir or os.path.join(root, ARTWORK_REL),
              record)
    running = steam_running(account.steam_root, proc_root)
    art_status = {'install': 'would-install'}
    return _result(p['action'], account, p['appid'], root, dry_run=True,
                   steam_running=running, would_block=bool(running),
                   artwork={a['slot']: {'status': art_status.get(a['status'], a['status']),
                                        'path': a['dest']} for a in p['art']},
                   warnings=p['warnings'])


def _backup_dir(root, account):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path = os.path.join(root, BACKUP_REL, '%s-%s' % (stamp, account.account_id))
    os.makedirs(path, mode=0o700)
    return path


def _save_manifest(path, manifest):
    _atomic_write(path, (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode(), 0o644)


def _transaction(account, root, new_vdf, raw, art_writes, art_removals, manifest_path, manifest,
                 proc_root, backup):
    """Apply grid/vdf/manifest changes; undo everything already applied on failure."""
    undo = []
    try:
        if art_writes and not os.path.isdir(account.grid):
            os.mkdir(account.grid, 0o755)
            undo.append(('rmdir', account.grid))
        for item in art_writes + art_removals:
            if os.path.exists(item['dest']):
                saved = os.path.join(backup, os.path.basename(item['dest']))
                shutil.copy2(item['dest'], saved)
                undo.append(('restore', item['dest'], saved))
            else:
                undo.append(('remove', item['dest']))
        for item in art_writes:
            with open(item['source'], 'rb') as handle:
                _atomic_write(item['dest'], handle.read(), 0o644)
        for item in art_removals:
            os.unlink(item['dest'])
        if new_vdf is not None:
            _require_closed(account, proc_root)
            mode = 0o644
            if raw is not None:
                mode = stat.S_IMODE(os.stat(account.shortcuts).st_mode)
                with open(account.shortcuts, 'rb') as handle:
                    if handle.read() != raw:
                        raise Conflict('shortcuts.vdf changed during registration')
                undo.append(('restore', account.shortcuts, os.path.join(backup, 'shortcuts.vdf')))
            else:
                if os.path.exists(account.shortcuts):
                    raise Conflict('shortcuts.vdf appeared during registration')
                undo.append(('remove', account.shortcuts))
            vdf.parse(new_vdf)  # never write something we could not read back
            _atomic_write(account.shortcuts, new_vdf, mode)
        _save_manifest(manifest_path, manifest)
    except BaseException as exc:
        failures = []
        for step in reversed(undo):
            try:
                if step[0] == 'restore':
                    with open(step[2], 'rb') as handle:
                        _atomic_write(step[1], handle.read(),
                                      stat.S_IMODE(os.stat(step[2]).st_mode))
                elif step[0] == 'remove':
                    if os.path.lexists(step[1]):
                        os.unlink(step[1])
                elif step[0] == 'rmdir':
                    os.rmdir(step[1])
            except OSError as undo_exc:
                failures.append('%s: %s' % (step[1], undo_exc))
        if isinstance(exc, RegistrationError) and not failures:
            raise
        message = '%s; changes rolled back' % exc
        if failures:
            message += '; ROLLBACK INCOMPLETE, backup kept in %s: %s' % (backup, '; '.join(failures))
        raise RegistrationError(message)


def register(install_root, account_id, steam_root=None, launch_target=DEFAULT_TARGET,
             launch_options=DEFAULT_LAUNCH_OPTIONS, artwork_dir=None,
             steam_closed_confirmed=False, proc_root='/proc'):
    if not steam_closed_confirmed:
        raise UsageError('confirm that Steam has been exited before registering')
    root, target = _resolve_install(install_root, launch_target)
    _check_launch_options(launch_options)
    _check_install_complete(root)
    account = Account(steam_root or default_steam_root(), account_id)
    with _Lock(root):
        _require_closed(account, proc_root)
        manifest_path, manifest = _load_manifest(root)
        record = manifest['registrations'].get(account.key)
        p = _plan(account, root, target, launch_options,
                  artwork_dir or os.path.join(root, ARTWORK_REL), record)
        art_writes = [a for a in p['art'] if a['status'] == 'install']
        new_vdf = vdf.serialize(p['nodes']) if p['changed'] else None
        if new_vdf is not None and new_vdf == p['raw']:
            new_vdf = None
        old_art = (record or {}).get('artwork', {}) if (record or {}).get('appid') == p['appid'] else {}
        art_record = {}
        for a in p['art']:
            if a['status'] in ('install', 'unchanged'):
                art_record[a['slot']] = {'path': a['dest'], 'sha256': a['sha256']}
            elif a['slot'] in old_art and a['status'] in ('missing-source', 'invalid-source'):
                art_record[a['slot']] = old_art[a['slot']]
        manifest['registrations'][account.key] = {
            'steam_root': account.steam_root, 'account_id': account.account_id,
            'appid': p['appid'], 'target': target, 'exe': p['exe'], 'start_dir': p['start_dir'],
            'launch_options': p['launch_options'], 'icon_field': p['icon_field'],
            'artwork': art_record, 'title': TITLE,
        }
        backup = None
        if new_vdf is not None or art_writes:
            backup = _backup_dir(root, account)
            if p['raw'] is not None:
                with open(os.path.join(backup, 'shortcuts.vdf'), 'wb') as handle:
                    handle.write(p['raw'])
        _transaction(account, root, new_vdf, p['raw'], art_writes, [], manifest_path, manifest,
                     proc_root, backup)
        if new_vdf is None and not art_writes:
            action = 'noop'
        elif p['action'] == 'noop':
            action = 'update'
        else:
            action = p['action']
        return _result(action, account, p['appid'], root, backup_dir=backup,
                       artwork=_art_report(p['art']), warnings=p['warnings'])


def unregister(install_root, account_id, steam_root=None, steam_closed_confirmed=False,
               proc_root='/proc'):
    """Remove only the manifest-recorded entry whose target still matches, and our art."""
    if not steam_closed_confirmed:
        raise UsageError('confirm that Steam has been exited before unregistering')
    _check_text(install_root, 'install root')
    if not os.path.isabs(install_root) or not os.path.isdir(install_root):
        raise UsageError('install root must be an existing absolute directory')
    root = os.path.normpath(install_root)
    account = Account(steam_root or default_steam_root(), account_id)
    with _Lock(root):
        _require_closed(account, proc_root)
        manifest_path, manifest = _load_manifest(root)
        record = manifest['registrations'].get(account.key)
        if not record:
            return _result('absent', account, None, root,
                           warnings=['no registration recorded for this account; nothing removed'])
        raw, nodes, shortcuts = account.read_shortcuts()
        warnings = []
        keep, removed = [], 0
        for entry in shortcuts.value:
            if (_entry_appid(entry) == record['appid']
                    and _same_path(_entry_str(entry, 'Exe') or '', record['target'])):
                removed += 1
                continue
            if _entry_appid(entry) == record['appid']:
                warnings.append('shortcut with recorded appid now targets another program; kept')
            keep.append(entry)
        if removed > 1:
            raise Conflict('multiple entries match the recorded registration; not removing')
        new_vdf = None
        if removed:
            for index, entry in enumerate(keep):
                entry.key = str(index).encode()
            shortcuts.value[:] = keep
            new_vdf = vdf.serialize(nodes)
        art_removals, art = [], {}
        for slot, info in sorted(record.get('artwork', {}).items()):
            dest = info.get('path', '')
            if (os.path.dirname(dest) == account.grid and os.path.isfile(dest)
                    and not os.path.islink(dest) and _sha256(dest) == info.get('sha256')):
                art_removals.append({'slot': slot, 'dest': dest})
                art[slot] = {'status': 'removed', 'path': dest}
            elif os.path.lexists(dest):
                art[slot] = {'status': 'preserved-user', 'path': dest}
                warnings.append('kept modified %s artwork' % slot)
            else:
                art[slot] = {'status': 'absent', 'path': None}
        del manifest['registrations'][account.key]
        backup = None
        if new_vdf is not None or art_removals:
            backup = _backup_dir(root, account)
            if raw is not None:
                with open(os.path.join(backup, 'shortcuts.vdf'), 'wb') as handle:
                    handle.write(raw)
        _transaction(account, root, new_vdf, raw, [], art_removals, manifest_path, manifest,
                     proc_root, backup)
        if not removed:
            warnings.append('recorded shortcut was not present; manifest record cleared')
        return _result('remove' if removed else 'absent', account, record['appid'], root,
                       backup_dir=backup, artwork=art, warnings=warnings)


# ---------------------------------------------------------------------- CLI

def _error_json(exc):
    return {'ok': False, 'error': {'code': exc.code, 'type': type(exc).__name__,
                                   'message': str(exc)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Jet Car Stunts 2 Steam shortcut registration')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('roots')
    accounts = sub.add_parser('accounts')
    accounts.add_argument('--steam-root')
    for name in ('plan', 'register', 'unregister'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--install-root', required=True)
        cmd.add_argument('--account', required=True)
        cmd.add_argument('--steam-root')
        cmd.add_argument('--proc-root', default='/proc', help=argparse.SUPPRESS)
        if name != 'unregister':
            cmd.add_argument('--launch-target', default=DEFAULT_TARGET)
            cmd.add_argument('--launch-options', default=DEFAULT_LAUNCH_OPTIONS)
            cmd.add_argument('--artwork-dir')
        if name != 'plan':
            cmd.add_argument('--confirm-steam-closed', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        if args.command == 'roots':
            out = {'ok': True, 'roots': find_steam_roots()}
        elif args.command == 'accounts':
            root = args.steam_root or default_steam_root()
            out = {'ok': True, 'steam_root': root, 'accounts': list_accounts(root)}
        elif args.command == 'plan':
            out = plan(args.install_root, args.account, args.steam_root, args.launch_target,
                       args.launch_options, args.artwork_dir, args.proc_root)
        elif args.command == 'register':
            out = register(args.install_root, args.account, args.steam_root, args.launch_target,
                           args.launch_options, args.artwork_dir, args.confirm_steam_closed,
                           args.proc_root)
        elif args.command == 'unregister':
            out = unregister(args.install_root, args.account, args.steam_root,
                             args.confirm_steam_closed, args.proc_root)
        else:
            parser.print_usage(sys.stderr)
            return 2
    except RegistrationError as exc:
        print(json.dumps(_error_json(exc), indent=2))
        return exc.code
    except OSError as exc:
        err = RegistrationError('I/O error: %s' % exc)
        print(json.dumps(_error_json(err), indent=2))
        return err.code
    print(json.dumps(out, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
