import json
import re

from .. import APIError

CARD_URL = 'https://mooc1-api.chaoxing.com/knowledge/cards'


def attachment_settings(task):
    response = task.session.get(CARD_URL, params={
        'clazzid': task.clazzid, 'courseid': task.courseid,
        'knowledgeid': task.knowledgeid, 'num': task.card_index,
        'isPhone': 1, 'control': 'true', 'cpi': task.cpi,
    })
    response.raise_for_status()
    match = re.search(r'window\.AttachmentSetting\s*=\s*', response.text)
    if not match:
        raise APIError('任务页面缺少附件信息，可能未开放或页面结构已变化')
    try:
        data, _ = json.JSONDecoder().raw_decode(response.text[match.end():])
        if not isinstance(data.get('attachments'), list):
            raise ValueError
        return data
    except (ValueError, AttributeError):
        raise APIError('任务附件信息无法解析') from None


def find_attachment(data, task):
    # The same object can appear in several jobs. Never let its first occurrence
    # hide a matching, unfinished job later in the attachment list.
    candidates = [a for a in data['attachments'] if isinstance(a, dict)]
    for field in ('jobid', 'workid', 'objectid'):
        wanted = getattr(task, field, None)
        if not wanted:
            continue
        def identifier(attachment):
            prop = attachment.get('property')
            prop = prop if isinstance(prop, dict) else {}
            if field == 'objectid':
                return attachment.get('objectId', prop.get('objectid', ''))
            return attachment.get(field, prop.get(field, ''))
        matches = [a for a in candidates if str(identifier(a)) == str(wanted)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            candidates = matches
    if len(candidates) > 1:
        raise APIError('同一资源对应多个任务，无法唯一确认任务身份，已停止处理该资源')
    raise APIError('没有找到与当前资源匹配的任务信息，已停止处理该资源')
