"""External, per-endpoint API credentials; never part of a frozen executable."""
import hashlib
import json
import os
from urllib.parse import urlsplit, urlunsplit

from settings import ROOT, save_json, storage_lock

CREDENTIAL_FILE = 'ai_credentials.json'


def completion_url(value):
    if not isinstance(value, str):
        raise ValueError('请填写 AI 接口 URL')
    parsed = urlsplit(value.strip())
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('AI 接口 URL 必须是有效的 HTTP / HTTPS 地址，不能包含用户名、密码或片段')
    path = parsed.path.rstrip('/')
    if not path:
        path = '/v1/chat/completions'
    elif not path.endswith('/chat/completions'):
        path += '/chat/completions'
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ''))


def credential_id(url):
    return hashlib.sha256(completion_url(url).encode('utf-8')).hexdigest()


def read_credentials(path=None):
    path = path or ROOT / CREDENTIAL_FILE
    if not path.exists():
        return {'version': 1, 'services': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if (not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('services'), dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in data['services'].items())):
            raise ValueError()
        return data
    except (ValueError, OSError):
        raise ValueError('ai_credentials.json 无法读取或格式损坏，请检查该文件；原文件未被覆盖') from None


def load_api_key(url):
    environment = os.environ.get('CXKITTY_AI_API_KEY')
    if environment is not None:
        return environment.strip()
    if not url.strip():
        return ''
    return read_credentials()['services'].get(credential_id(url), '')


def save_api_key(url, api_key):
    if not url.strip() and not api_key.strip():
        return
    key = credential_id(url)
    with storage_lock(ROOT / CREDENTIAL_FILE):
        data = read_credentials()
        if api_key.strip():
            data['services'][key] = api_key.strip()
        else:
            data['services'].pop(key, None)
        save_json(ROOT / CREDENTIAL_FILE, data)
