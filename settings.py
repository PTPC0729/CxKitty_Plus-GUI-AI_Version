"""Configuration and atomic storage, compatible with the original YAML files."""
import copy
import json
import math
import os
import tempfile
import threading
from pathlib import Path

import yaml

from runtime_paths import data_root

ROOT = data_root()
_path_locks = {}
_path_locks_guard = threading.Lock()


def storage_lock(path):
    """Serialize a complete read/modify/write transaction across task pages."""
    key = os.path.normcase(str(Path(path).expanduser().resolve()))
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.RLock())


DEFAULTS = {
    'multiSession': True, 'sessionPath': 'session/', 'maskAcc': True,
    'tUIMaxHeight': 20,
    'workflow': {'continue_courses': False},
    'video': {'enable': True, 'speed': 1.0, 'wait': 15, 'report_rate': 58},
    'exam': {'enable': True, 'wait': 15},
    'document': {'enable': True, 'wait': 15},
    'searcher': {
        'use': 'jsonFileSearcher',
        'restApiSearcher': {'url': '', 'method': 'POST',
                            'req_field': 'question', 'rsp_field': '$.data'},
        'jsonFileSearcher': {'file_path': 'questions.json'},
        'sqliteSearcher': {'file_path': 'questions.db', 'table': 'question',
                           'req_field': 'question', 'rsp_field': 'answer'},
        'aiSearcher': {
            'url': '', 'model': '', 'strategy': 'ai_only', 'fallback': 'jsonFileSearcher',
            'timeout': 90, 'retries': 2, 'max_tokens': 4096, 'token_parameter': 'max_tokens',
            'cache': True, 'cache_path': 'ai_cache.json',
        },
    },
}


def resolve_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _merge(default, supplied):
    result = copy.deepcopy(default)
    for key, value in supplied.items():
        if key in result and isinstance(result[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f'配置项 {key} 必须是一个配置分组')
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def validate_config(config):
    for key in ('multiSession', 'maskAcc'):
        if not isinstance(config[key], bool):
            raise ValueError(f'{key} 必须为 true 或 false')
    if not isinstance(config.get('workflow', DEFAULTS['workflow'])['continue_courses'], bool):
        raise ValueError('workflow.continue_courses 必须为 true 或 false')
    if not isinstance(config['sessionPath'], str) or not config['sessionPath'].strip():
        raise ValueError('sessionPath 不能为空')
    for kind in ('video', 'exam', 'document'):
        if not isinstance(config[kind]['enable'], bool):
            raise ValueError(f'{kind}.enable 必须为 true 或 false')
        wait = config[kind]['wait']
        if isinstance(wait, bool) or not isinstance(wait, int) or not 0 <= wait <= 3600:
            raise ValueError(f'{kind}.wait 必须为 0～3600 的整数')
    for key, low, high in (('speed', 0.1, 16), ('report_rate', 1, 300)):
        value = config['video'][key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'video.{key} 必须在 {low}～{high} 之间')
    height = config['tUIMaxHeight']
    if isinstance(height, bool) or not isinstance(height, int) or not 8 <= height <= 200:
        raise ValueError('tUIMaxHeight 必须为 8～200 的整数')
    if config['searcher']['use'] not in ('jsonFileSearcher', 'sqliteSearcher', 'restApiSearcher', 'aiSearcher'):
        raise ValueError('不支持的题库类型')
    ai = config['searcher'].get('aiSearcher', DEFAULTS['searcher']['aiSearcher'])
    if ai['strategy'] not in ('ai_only', 'bank_first') or ai['fallback'] not in ('jsonFileSearcher', 'sqliteSearcher', 'restApiSearcher'):
        raise ValueError('AI 答题策略或优先题库无效')
    for name, low, high in (('timeout', 5, 600), ('retries', 0, 5), ('max_tokens', 128, 65536)):
        value = ai[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'AI {name} 必须为 {low}～{high} 的整数')
    if ai['token_parameter'] not in ('max_tokens', 'max_completion_tokens') or not isinstance(ai['cache'], bool):
        raise ValueError('AI 输出上限参数或缓存设置无效')
    for name in ('url', 'model', 'cache_path'):
        if not isinstance(ai[name], str):
            raise ValueError(f'AI {name} 必须为文本')
    if not ai['cache_path'].strip():
        raise ValueError('AI 缓存路径不能为空')
    return config


def load_config(path=None):
    path = Path(path) if path else ROOT / 'config.yml'
    supplied = yaml.safe_load(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise ValueError('配置文件最外层必须是键值映射')
    return validate_config(_merge(DEFAULTS, supplied))


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name, suffix='.tmp', delete=False) as fp:
            name = fp.name
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def save_config(config, path=None):
    validate_config(config)
    stored = copy.deepcopy(config)
    # The UI passes an in-memory key to workers; persistent keys live separately.
    stored.get('searcher', {}).get('aiSearcher', {}).pop('api_key', None)
    atomic_write(path or ROOT / 'config.yml', yaml.safe_dump(stored, allow_unicode=True, sort_keys=False))


def save_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
