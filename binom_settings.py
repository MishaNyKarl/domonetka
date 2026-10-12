"""Private server configuration; credentials never appear in public responses."""
import json
import os
import secrets
import hashlib
from binom import Client, token_mapping


def settings_path(data_dir, user_id=None):
    if user_id is None: return data_dir / 'binom-settings.json'
    return data_dir / ('binom-user-' + hashlib.sha256(str(user_id).encode()).hexdigest() + '.json')


def read_settings(data_dir, user_id=None):
    path = settings_path(data_dir, user_id)
    if not path.exists(): return dict(origin='', api_key='', tokens={})
    return json.loads(path.read_text(encoding='utf-8'))


def public_settings(settings, admin=False):
    result = dict(origin=settings['origin'], tokens=settings['tokens'],status_policy=settings.get('status_policy',{}))
    if admin: result['has_api_key'] = bool(settings['api_key'])
    return result


def save_settings(data_dir, data, user_id=None):
    previous = read_settings(data_dir, user_id)
    origin = str(data.get('origin', '')).strip().rstrip('/')
    key = str(data.get('api_key', '')).strip() or previous['api_key']
    if data.get('clear_api_key'): key = ''
    if origin != previous['origin'] and previous['api_key'] and not data.get('api_key') and not data.get('clear_api_key'):
        raise ValueError('При смене адреса Binom укажите новый API-ключ или удалите прежний')
    if origin: Client(origin, key or 'validation-only')
    elif key: raise ValueError('Укажите адрес Binom')
    if len(key) > 4096 or any(ord(c) < 32 for c in key): raise ValueError('Некорректный API-ключ')
    settings = dict(origin=origin, api_key=key, tokens=token_mapping(data.get('tokens', {})))
    from binom_sync import status_policy
    settings['status_policy']=status_policy(data.get('status_policy',previous.get('status_policy',{})))
    path = settings_path(data_dir, user_id)
    temp = data_dir / ('binom-settings-' + secrets.token_hex(12) + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(settings, output, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()
    return public_settings(settings, admin=True)


def migrate_legacy(data_dir, admin_id):
    if settings_path(data_dir).exists() and not settings_path(data_dir, admin_id).exists():
        save_settings(data_dir, read_settings(data_dir), admin_id)


def check_connection(data_dir, user_id=None):
    settings = read_settings(data_dir, user_id)
    client = Client(settings['origin'], settings['api_key'])
    # Read-only endpoint from the installed tracker OpenAPI; return no tracker data.
    response = client.get('/public/api/v1/traffic_source/list/filtered', {'limit': 1, 'offset': 0})
    if not isinstance(response, list): raise ValueError('Binom вернул неожиданный ответ API')
    return dict(ok=True, message='API-ключ принят. Статистика обновляется фоновым импортом по выбранному интервалу.')
