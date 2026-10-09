import json
from dataclasses import dataclass
from typing import Optional

from settings import load_config, resolve_path, save_json, storage_lock

CONF = load_config()
CONF_SESSPATH = resolve_path(CONF['sessionPath'])
CONF_MULTI_SESS = CONF['multiSession']
CONF_TUI_MAX_HEIGHT = CONF['tUIMaxHeight']
CONF_MASKACC = CONF['maskAcc']
CONF_EXAM, CONF_VIDEO, CONF_DOCUMENT = CONF['exam'], CONF['video'], CONF['document']
CONF_SEARCHER = CONF['searcher']
CONF_EN_EXAM, CONF_EN_VIDEO, CONF_EN_DOCUMENT = (CONF[k]['enable'] for k in ('exam', 'video', 'document'))
CONF_WAIT_EXAM, CONF_WAIT_VIDEO, CONF_WAIT_DOCUMENT = (CONF[k]['wait'] for k in ('exam', 'video', 'document'))


@dataclass
class SessionModule:
    phone: str
    puid: int
    passwd: Optional[str]
    name: str
    ck: str


def dict2ck(cookies):
    return ''.join(f'{key}={value};' for key, value in cookies.items())


def ck2dict(cookies):
    if isinstance(cookies, dict):
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in cookies.items()):
            raise ValueError('会话中的 Cookie 格式无效，请重新登录')
        return dict(cookies)
    if not isinstance(cookies, str):
        raise ValueError('会话中的 Cookie 格式无效，请重新登录')
    result = {}
    for field in cookies.split(';'):
        if not field.strip():
            continue
        if '=' not in field:
            raise ValueError('会话中的 Cookie 格式无效，请重新登录')
        key, value = field.strip().split('=', 1)
        result[key.strip()] = value
    return result


def save_session(api, passwd=None, session_path=None):
    folder = resolve_path(session_path or CONF['sessionPath'])
    phone = str(api.acc.phone)
    if not phone.isdigit():
        raise ValueError('账号没有有效手机号，无法保存会话')
    path = folder / f'{phone}.json'
    with storage_lock(path):
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding='utf-8-sig'))
                if not isinstance(existing, dict):
                    existing = {}
            except (ValueError, OSError):
                pass
        existing.update(phone=phone, puid=api.acc.puid, passwd=passwd,
                        name=api.acc.name, ck=dict2ck(api.ck_dump()))
        save_json(path, existing)


def sessions_load(session_path=None, on_error=None):
    folder = resolve_path(session_path or CONF['sessionPath'])
    if not folder.is_dir():
        return []
    sessions = []
    for path in sorted(folder.glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            cookies = data['ck']
            ck2dict(cookies)
            sessions.append(SessionModule(str(data['phone']), int(data['puid']),
                                          data.get('passwd'), str(data.get('name') or ''),
                                          dict2ck(cookies) if isinstance(cookies, dict) else cookies))
        except (OSError, ValueError, TypeError, KeyError) as error:
            if on_error:
                on_error(f'一个会话文件无法读取，已跳过（{type(error).__name__}）')
    return sessions


def mask_name(name):
    return name[0] + '*' + (name[-1] if len(name) > 2 else '') if name else '未命名'


def mask_phone(phone):
    return phone[:3] + '****' + phone[-4:] if len(phone) >= 7 else '****'
