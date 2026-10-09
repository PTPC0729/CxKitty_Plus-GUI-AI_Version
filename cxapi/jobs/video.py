import math
import urllib.parse
from hashlib import md5

from .. import APIError, get_dc
from ..network import AuthenticationError, ResourceAccessError, error_text
from runtime import Cancelled
from video_progress import resume_position, save_position
from .common import CARD_URL, attachment_settings, find_attachment

API_CHAPTER_CARD_RESOURCE = 'https://mooc1-api.chaoxing.com/ananas/status'
API_VIDEO_PLAYREPORT = 'https://mooc1-api.chaoxing.com/mooc-ans/multimedia/log/a'


def report_endpoint(value, cpi):
    """Use the endpoint issued with this task, restricted to platform hosts."""
    if value is None or value == '':
        return f'{API_VIDEO_PLAYREPORT}/{cpi}'
    if not isinstance(value, str):
        raise APIError('视频上报地址格式无效，请刷新章节后重试')
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(CARD_URL, value))
    expected_paths = (f'/mooc-ans/multimedia/log/a/{cpi}', f'/multimedia/log/a/{cpi}')
    if (parsed.scheme not in ('https', 'http')
            or parsed.hostname not in ('mooc1-api.chaoxing.com', 'mooc1.chaoxing.com')
            or parsed.username or parsed.password or parsed.port not in (None, 443, 80)
            or parsed.query or parsed.fragment or parsed.path.rstrip('/') not in expected_paths):
        raise APIError('平台返回的视频上报地址与当前任务不匹配，请刷新章节后重试')
    return urllib.parse.urlunsplit(('https', parsed.hostname, parsed.path.rstrip('/'), '', ''))


def report_other_info(value, courseid):
    """The player's otherInfo string can contain a separate &courseId= field."""
    if not isinstance(value, str):
        raise APIError('视频上报附加参数格式无效')
    parts = value.split('&')
    remaining = [parts[0]]
    for part in parts[1:]:
        key, separator, supplied = part.partition('=')
        if urllib.parse.unquote_plus(key) == 'courseId' and separator:
            if urllib.parse.unquote_plus(supplied) != str(courseid):
                raise APIError('视频上报课程参数与当前课程不一致，请刷新章节后重试')
        else:
            # Unknown fields stay inside the encoded value, never override the
            # signed playingTime/jobid or other top-level request parameters.
            remaining.append(part)
    return '&'.join(remaining)


class ChapterVideo:
    kind = 'video'

    def __init__(self, session, acc, clazzid, courseid, knowledgeid, card_index, objectid, cpi, point_index):
        self.session, self.acc = session, acc
        self.clazzid, self.courseid, self.knowledgeid = clazzid, courseid, knowledgeid
        self.card_index, self.objectid, self.cpi, self.point_index = card_index, objectid, cpi, point_index
        self.title = '视频'
        self.skip_reason = '视频已完成'
        self.report_url = None

    def pre_fetch(self):
        data = attachment_settings(self)
        item = find_attachment(data, self)
        self.title = item.get('property', {}).get('name') or '视频'
        if item.get('isPassed') is True:
            return False
        if item.get('job') is False or not item.get('jobid'):
            self.skip_reason = '非任务点视频'
            return False
        self.fid = data['defaults']['fid']
        self.jobid, self.otherInfo = item['jobid'], item.get('otherInfo', '')
        self.report_url = report_endpoint(data['defaults'].get('reportUrl'), self.cpi)
        self.platform_offset_ms = item.get('headOffset')
        return True

    def fetch(self):
        response = self.session.get(f'{API_CHAPTER_CARD_RESOURCE}/{self.objectid}',
                                    params={'k': self.fid, 'flag': 'normal', '_dc': get_dc()})
        response.raise_for_status()
        data = response.json()
        self.dtoken, self.duration = data['dtoken'], int(data['duration'])
        self.title = data.get('filename') or self.title
        if self.duration <= 0:
            raise APIError('视频时长无效')
        return True

    def _report(self, playing_time):
        endpoint = self.report_url or report_endpoint(None, self.cpi)
        response = self.session.get(f'{endpoint}/{urllib.parse.quote(str(self.dtoken), safe="")}',
            headers={'Referer': CARD_URL + '?' + urllib.parse.urlencode({
                'clazzid': self.clazzid, 'courseid': self.courseid,
                'knowledgeid': self.knowledgeid, 'num': self.card_index, 'cpi': self.cpi})},
            params=urllib.parse.urlencode({
                'otherInfo': report_other_info(self.otherInfo, self.courseid),
                'courseId': self.courseid, 'playingTime': playing_time, 'duration': self.duration,
                'jobid': self.jobid, 'clipTime': f'0_{self.duration}', 'clazzId': self.clazzid,
                'objectId': self.objectid, 'userid': self.acc.puid, 'isdrag': '0',
                'enc': md5(f'[{self.clazzid}][{self.acc.puid}][{self.jobid}][{self.objectid}][{playing_time * 1000}][d_yHJ!$pdA~5][{self.duration * 1000}][0_{self.duration}]'.encode()).hexdigest(),
                'rt': '0.9', 'dtype': 'Video', 'view': 'pc', '_t': get_dc(),
            }))
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get('isPassed'), bool):
            raise APIError('视频上报返回格式无效')
        return data

    def run(self, context, config):
        speed, interval = float(config['speed']), float(config['report_rate'])
        if not math.isfinite(speed) or speed <= 0 or not math.isfinite(interval) or interval <= 0:
            raise ValueError('播放速度和上报间隔必须为正数')
        played = resume_position(self, context)
        if played:
            context.log(f'从平台核验进度 {int(played)//60:02d}:{int(played)%60:02d} 继续视频')
        since_report, active = interval, 0.0
        failures, end_reports = 0, 0
        reached_end = False
        deadline = (self.duration - played) / speed + max(180.0, interval * 4)
        while active <= deadline:
            context.checkpoint()
            at_end = played >= self.duration
            if since_report >= interval or (at_end and not reached_end):
                since_report = 0.0
                reached_end = at_end
                if at_end:
                    end_reports += 1
                try:
                    result = self._report(min(int(played), self.duration))
                    if not isinstance(result, dict) or not isinstance(result.get('isPassed'), bool):
                        raise APIError('视频上报未返回有效确认，不能记录为成功')
                    if result.get('error') or result.get('status') in (False, 0, 'false', '0'):
                        raise APIError('平台未接受视频进度上报')
                    failures = 0
                    save_position(self, min(int(played), self.duration), context, completed=result['isPassed'])
                    context.log('视频进度已上报')
                    if result.get('isPassed') is True:
                        context.event('progress', value=self.duration, total=self.duration, text='平台已确认视频完成')
                        return
                except (AuthenticationError, ResourceAccessError, Cancelled):
                    raise
                except Exception as error:
                    failures += 1
                    context.log(error_text(error), 'warning')
                    if failures >= 3:
                        raise APIError('视频连续三次上报失败，请稍后重试') from error
                if end_reports >= 4:
                    raise APIError('视频已到结尾，但平台尚未确认完成，请稍后核验或重试')
            context.event('progress', value=played, total=self.duration,
                          text=f'视频 {int(played)//60:02d}:{int(played)%60:02d} / {self.duration//60:02d}:{self.duration%60:02d}')
            # Defensive bound even when a custom context or resumed clock jumps.
            delta = min(1.0, max(0.0, context.wait(1.0)))
            active += delta
            played = min(float(self.duration), played + delta * speed)
            since_report += delta
        raise APIError('视频处理超过最长等待时间')
