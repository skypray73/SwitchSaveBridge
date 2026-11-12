"""Save Bridge: directory-save transfers, with verified staging and rollback."""
from pathlib import Path
from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import struct
import unicodedata
import uuid
import configparser


def read_config(path, base):
    base = Path(base)
    cfg = configparser.ConfigParser(interpolation=None)
    if Path(path).exists():
        with Path(path).open(encoding='utf-8-sig') as f:
            cfg.read_file(f)
    defaults = {'switch': 'atmosphere_save', 'emulator': str(Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'Ryujinx/bis/user/save'), 'export': 'export save'}
    paths = {}
    for key, default in defaults.items():
        p = Path(cfg.get('paths', key, fallback=default))
        paths[key] = str(p if p.is_absolute() else base / p)
    for key, default in {'eden': str(Path(os.environ.get('APPDATA', '')) / 'eden/nand'), 'ryubing': paths['emulator']}.items():
        p = Path(cfg.get('paths', key, fallback=default))
        paths[key] = str(p if p.is_absolute() else base / p)
    paths['backend'] = cfg.get('paths', 'backend', fallback='Eden')
    return paths, dict(cfg.items('regions')) if cfg.has_section('regions') else {}


def write_config(path, paths, regions):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg['paths'] = paths
    cfg['regions'] = regions
    temp = Path(path).with_suffix('.ini.tmp')
    try:
        with temp.open('w', encoding='utf-8') as f:
            cfg.write(f)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def normalized(s):
    return ''.join(c for c in unicodedata.normalize('NFKC', s).casefold() if c.isalnum())


def inventory(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise ValueError(f'不是一般資料夾：{root}')
    result = {}
    for p in root.rglob('*'):
        if p.is_symlink() or p.is_junction():
            raise ValueError(f'不支援連結／接合點：{p}')
        if p.is_file():
            h = hashlib.sha256()
            with p.open('rb') as f:
                for block in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(block)
            result[p.relative_to(root).as_posix()] = h.hexdigest()
    return result


def copy_verified(src, dst):
    before = inventory(src)
    shutil.copytree(src, dst)
    if before != inventory(src) or before != inventory(dst):
        raise IOError('複製驗證失敗，或來源在複製期間變動')


def ensure_closed():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes as w
        class Entry(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                        ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                        ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                        ('exe', w.WCHAR * 260)]
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        k.CreateToolhelp32Snapshot.restype = w.HANDLE
        k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError('無法確認模擬器是否關閉，已中止轉移')
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            found = k.Process32FirstW(handle, ctypes.byref(entry))
            if not found:
                raise OSError('無法讀取程序清單，已中止轉移')
            while found:
                if any(name in entry.exe.lower() for name in ('ryujinx', 'ryubing', 'ryutendo', 'eden')):
                    raise RuntimeError('請先完全關閉 Ryujinx／Ryubing／Eden，再執行轉移。')
                found = k.Process32NextW(handle, ctypes.byref(entry))
        finally:
            k.CloseHandle(handle)


@dataclass
class Save:
    path: Path
    title: str
    tid: str
    uid: str
    kind: int
    backend: str = 'Ryubing'
    nand_root: str = ''


def plan_eden(root, tid, uid, kind, title=''):
    root = Path(root).resolve()
    if not re.fullmatch('010[0-9a-fA-F]{13}', tid) or not re.fullmatch('[0-9a-fA-F]{32}', uid) or kind not in (1, 3):
        raise ValueError('Title ID、帳號 UID 或存檔類型無效')
    profiles = eden_account_names(root)
    if kind == 1 and (profiles is None or uid.lower() not in profiles):
        raise ValueError('無法確認此 UID 為 Eden 已登記帳號，請先在 Eden 建立帳號')
    uid = uid.lower() if kind == 1 else '0' * 32
    existing, _, _ = scan_eden(root)
    found = [s for s in existing if s.tid == tid.lower() and s.uid == uid and s.kind == kind]
    if found:
        return found[0]
    path = root / 'user/save/0000000000000000' / uid.upper() / tid.upper()
    if root not in path.resolve().parents:
        raise ValueError('Eden 路徑超出 NAND')
    return Save(path, title or tid, tid.lower(), uid, kind, 'Eden', str(root))


def scan_eden(root, known=None):
    root = Path(root) / 'user/save'
    if not root.is_dir():
        raise ValueError('找不到 Eden nand/user/save，請選擇 nand 根目錄')
    saves, names = [], dict(known or {})
    candidates = []
    for p in root.glob('0000000000000000/*/*'):
        if re.fullmatch('[0-9a-fA-F]{32}', p.parent.name):
            uid = p.parent.name.lower()
            candidates.append((p, uid, 3 if uid == '0' * 32 else 1))
    for p in root.glob('account/*/*/0'):
        if re.fullmatch('[0-9a-fA-F]{32}', p.parent.parent.name):
            candidates.append((p, p.parent.parent.name.lower(), 1))
    candidates.extend((p, '0' * 32, 3) for p in root.glob('device/*/0'))
    for path, uid, kind in candidates:
        tid = path.name.lower() if path.name != '0' else path.parent.name.lower()
        if path.is_dir() and re.fullmatch('[0-9a-f]{16}', tid):
            saves.append(Save(path, names.get(tid, tid), tid, uid, kind, 'Eden'))
    # New layout takes priority, matching Eden's own path resolution.
    unique = {template_key(s): s for s in saves}
    return list(unique.values()), names, []


def eden_account_names(root):
    path = Path(root) / 'system/save/8000000000000010/su/avators/profiles.dat'
    try:
        data = path.read_bytes()
        if len(data) != 0x650:
            return None
        names = {}
        for i in range(8):
            entry = data[0x10 + i * 0xc8:0x10 + (i + 1) * 0xc8]
            uid = entry[:16][::-1].hex()
            name = entry[0x28:0x48].split(b'\0', 1)[0].decode('utf-8', errors='replace')
            if uid != '0' * 32:
                names[uid] = name or '未命名帳號'
        return names
    except OSError:
        return None


def save_content(save, slot='0'):
    return save.path if save.backend == 'Eden' else save.path / slot


def has_save_content(save, slot='0'):
    return any(p.is_file() and p.relative_to(save_content(save, slot)).as_posix() != '.yuzu_save_size' for p in save_content(save, slot).rglob('*'))


def scan_emulator(root, known=None):
    root = Path(root)
    if not root.is_dir():
        raise ValueError('找不到模擬器 save 目錄')
    names = dict(known or {})
    games = root.parent.parent.parent / 'games'
    for p in games.glob('*/gui/metadata.json'):
        try:
            names[p.parent.parent.name.lower()] = json.loads(p.read_text(encoding='utf-8-sig'))['title']
        except (ValueError, KeyError, OSError):
            pass
    saves, warnings = [], []
    for p in sorted(root.iterdir()):
        if not p.is_dir() or not re.fullmatch('[0-9a-fA-F]{16}', p.name):
            continue
        try:
            b = (p / 'ExtraData0').read_bytes()
            if len(b) != 512 or b != (p / 'ExtraData1').read_bytes():
                raise ValueError('ExtraData 不完整或兩份不一致')
            tid = f'{struct.unpack_from("<Q", b)[0]:016x}'
            if b[32] not in (1, 3):
                continue  # account and device saves only; BCAT is not game progress
            if not (p / '0').is_dir() or not (p / '1').is_dir():
                raise ValueError('缺少 0／1 目錄')
            saves.append(Save(p, names.get(tid, tid), tid, b[8:24].hex(), b[32]))
        except (OSError, ValueError) as e:
            warnings.append(f'{p.name}：{e}')
    return saves, names, warnings


def scan_switch(root):
    root = Path(root)
    if not root.is_dir():
        raise ValueError('找不到 Switch 備份目錄')
    rows = []
    for game in sorted(root.iterdir()):
        if not game.is_dir() or game.name.startswith('.') or game.name.casefold() == 'export':
            continue
        for profile in sorted(game.iterdir()):
            if not profile.is_dir() or profile.name == 'BCAT' or profile.name.startswith('SD_Cache'):
                continue
            if any(profile.iterdir()):
                rows.append((game.name, profile.name, profile))
    return rows


def account_scope(switch_root, emulator_root):
    return json.dumps([str(Path(p).resolve()).casefold() for p in (switch_root, emulator_root)])


def load_accounts(path):
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or any(not isinstance(v, dict) or any(not isinstance(k, str) or not isinstance(uid, str) or not re.fullmatch('[0-9a-f]{32}', uid) for k, uid in v.items()) for v in data.values()):
        raise ValueError('帳號對照設定格式無效')
    return data


def save_accounts(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def account_names(root):
    """Profiles.json IDs are printed ulong pairs; ExtraData stores little endian."""
    result = {}
    try:
        data = json.loads((Path(root).parent.parent.parent / 'system/Profiles.json').read_text(encoding='utf-8-sig'))
        for p in data['profiles']:
            uid = p['user_id']
            if isinstance(uid, str) and re.fullmatch('[0-9a-fA-F]{32}', uid):
                key = struct.pack('<QQ', int(uid[:16], 16), int(uid[16:], 16)).hex()
                result[key] = str(p.get('name', '未命名'))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result


def export_profile(save, accounts):
    if save.kind == 3:
        return 'Device'
    aliases = [name for name, uid in accounts.items() if uid == save.uid]
    if len(aliases) == 1 and safe_name(aliases[0]) == aliases[0] and aliases[0] not in ('Device', 'BCAT') and not aliases[0].startswith(('SD_Cache', 'User_')):
        return aliases[0]
    return 'User_' + save.uid


def match_saves(game, profile, saves, names, accounts=None):
    ids = {tid for tid, name in names.items() if normalized(name) == normalized(game)}
    ids.update(x.lower() for x in re.findall(r'(?i)(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])', game))
    kind = 3 if profile == 'Device' else 1
    matches = [s for s in saves if s.tid in ids and s.kind == kind]
    if kind == 1:
        uid = profile[5:].lower() if re.fullmatch('User_[0-9a-fA-F]{32}', profile) else (accounts or {}).get(profile)
        matches = [s for s in matches if s.uid == uid] if uid else []
    return matches


def related(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or a in b.parents or b in a.parents


def replace_directory(stage, target):
    """Commit a sibling stage; roll back if the second rename fails."""
    old = target.with_name('.bridge-old-' + uuid.uuid4().hex)
    target.rename(old)
    try:
        stage.rename(target)
    except BaseException:
        old.rename(target)
        raise
    try:
        shutil.rmtree(old)
    except OSError:
        pass  # verified backup exists; do not report a committed transfer as failed


def import_save(src, save, backup_root):
    ensure_closed()
    src, target, backup_root = Path(src).resolve(), save.path.resolve(), Path(backup_root).resolve()
    if related(src, target) or related(backup_root, target) or related(backup_root, src):
        raise ValueError('來源、目的地與備份路徑不可相互包含')
    if save.backend == 'Eden':
        return import_eden(src, save, backup_root)
    # Revalidate metadata immediately before writing.
    b = (target / 'ExtraData0').read_bytes()
    if len(b) != 512 or b != (target / 'ExtraData1').read_bytes() or f'{struct.unpack_from("<Q", b)[0]:016x}' != save.tid or b[8:24].hex() != save.uid or b[32] != save.kind:
        raise ValueError('目的地識別資料已改變，請重新掃描')
    if not inventory(src):
        raise ValueError('來源沒有存檔檔案')
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / (target.name + '-' + uuid.uuid4().hex[:8])
    copy_verified(target, backup)
    (backup_root / (backup.name + '.json')).write_text(json.dumps({'original': str(target), 'backup': str(backup)}, ensure_ascii=False, indent=2), encoding='utf-8')
    stage = target.with_name('.bridge-stage-' + uuid.uuid4().hex)
    try:
        copy_verified(target, stage)
        for slot in ('0', '1'):
            shutil.rmtree(stage / slot)
            copy_verified(src, stage / slot)
        if inventory(stage / '0') != inventory(stage / '1') or inventory(src) != inventory(stage / '0'):
            raise IOError('來源在分支複製期間變動，已中止')
        ensure_closed()
        if inventory(target) != inventory(backup):
            raise RuntimeError('目的地在作業期間變動，已中止')
        replace_directory(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return backup


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s).rstrip('. ')[:120] or 'Unknown'


def import_eden(src, save, backup_root):
    target = save.path.resolve()
    if save.nand_root:
        validated = plan_eden(save.nand_root, save.tid, save.uid, save.kind, save.title)
        if validated.path.resolve() != target:
            raise ValueError('Eden 目的地已變更，請重新掃描')
        if not target.exists():
            if not inventory(src):
                raise ValueError('來源沒有存檔資料')
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = target.with_name('.bridge-stage-' + uuid.uuid4().hex)
            try:
                copy_verified(src, stage)
                ensure_closed()
                if target.exists():
                    raise ValueError('目的地已出現，請重新掃描')
                stage.rename(target)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            return target
    if not inventory(src):
        raise ValueError('來源沒有存檔檔案')
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / (target.name + '-' + uuid.uuid4().hex[:8])
    copy_verified(target, backup)
    (backup_root / (backup.name + '.json')).write_text(json.dumps({'original': str(target), 'backup': str(backup)}, ensure_ascii=False), encoding='utf-8')
    stage = target.with_name('.bridge-stage-' + uuid.uuid4().hex)
    try:
        copy_verified(src, stage)
        if (target / '.yuzu_save_size').is_file():
            shutil.copy2(target / '.yuzu_save_size', stage / '.yuzu_save_size')
        ensure_closed()
        if inventory(target) != inventory(backup):
            raise ValueError('Eden 存檔在作業期間變動，已中止')
        replace_directory(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return backup


def template_key(save):
    return f'{save.tid}:{save.uid}:{save.kind}'


def resolve_template(save, mapping, switch_root, accounts):
    entry = mapping.get(template_key(save))
    if not entry:
        return None
    game, profile = entry.get('game', ''), entry.get('profile', '')
    if not game or not profile or Path(game).name != game or Path(profile).name != profile or game in ('.', '..') or profile in ('.', '..'):
        return None
    root = Path(switch_root).resolve()
    path = root / game / profile
    if not path.is_dir() or root not in path.resolve().parents or not any(path.iterdir()):
        return None
    if save.kind == 3:
        return path if profile == 'Device' else None
    return path if profile != 'Device' and accounts.get(profile) == save.uid else None


def export_save(save, root, slot='0', profile=None, template=None):
    ensure_closed()
    root = Path(root).resolve()
    if slot not in ('0', '1') or related(save.path, root):
        raise ValueError('匯出路徑／分支無效')
    profile = profile or ('Device' if save.kind == 3 else 'User_' + save.uid)
    if profile != safe_name(profile) or profile in ('.', '..'):
        raise ValueError('匯出使用者名稱無效')
    if template is not None:
        template = Path(template)
        inventory(template)
        game = template.parent.name
        profile = template.name
        if safe_name(game) != game or safe_name(profile) != profile:
            raise ValueError('主機範本名稱無效')
        dest = root / game / profile
    else:
        dest = root / f'{safe_name(save.title)} [{save.tid.upper()}]' / profile
    if dest.exists():
        raise FileExistsError(f'匯出位置已有檔案，請選擇新的輸出目錄：{dest}')
    source = save_content(save, slot)
    if not has_save_content(save, slot):
        raise ValueError('此模擬器存檔為空白')
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = dest.with_name('.bridge-export-' + uuid.uuid4().hex)
    try:
        copy_verified(source, stage)
        if save.backend == 'Eden' and (stage / '.yuzu_save_size').is_file():
            (stage / '.yuzu_save_size').unlink()
        stage.rename(dest)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return dest
