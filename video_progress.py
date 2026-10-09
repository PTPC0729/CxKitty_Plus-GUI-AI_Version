"""External report history. Fresh platform offsets alone determine resumption."""
import hashlib
import json
import math

from settings import ROOT, save_json, storage_lock


def progress_key(task):
    values = [task.acc.puid, task.courseid, task.clazzid, task.knowledgeid, task.jobid, task.objectid]
    return hashlib.sha256(json.dumps(values, default=str).encode()).hexdigest()


def read_progress():
    path = ROOT / 'video_progress.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('entries'), dict):
        raise ValueError('invalid video checkpoint file')
    return data['entries']


def resume_position(task, context):
    # AttachmentSetting.headOffset is milliseconds, including values below 1000.
    # A successful heartbeat does not prove that the server saved its position.
    raw = getattr(task, 'platform_offset_ms', None)
    try:
        if raw is None:
            position = 0.0
        elif isinstance(raw, bool):
            raise ValueError()
        else:
            position = float(raw) / 1000.0
        if not math.isfinite(position) or not 0 <= position <= task.duration:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        context.log('平台视频续播位置无效，将从头开始', 'warning')
        return 0
    if position > 0 and position >= task.duration - 1:
        context.log('平台未确认视频完成，但续播位置已到结尾，将从头重新播放', 'warning')
        position = 0.0
    try:
        entry = read_progress().get(progress_key(task), {})
        local = entry.get('position', 0) if entry.get('duration') == task.duration else 0
        if float(local) > position + 1:
            context.log('已忽略超出平台进度的本地视频记录，按平台核验的位置播放', 'warning')
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        context.log('本地视频记录无法读取，仍按平台进度播放；原文件保留', 'warning')
    return position


def save_position(task, position, context, completed=False):
    try:
        with storage_lock(ROOT / 'video_progress.json'):
            entries = read_progress()
            # Reports are never proof of a saved playback position or completion.
            entries[progress_key(task)] = {'duration': task.duration, 'position': min(position, task.duration),
                                          'source': 'report', 'completed': completed}
            save_json(ROOT / 'video_progress.json', {'version': 1, 'entries': dict(list(entries.items())[-10000:])})
    except (OSError, ValueError, TypeError):
        context.log('视频进度已上报，但本地续接记录无法保存', 'warning')
