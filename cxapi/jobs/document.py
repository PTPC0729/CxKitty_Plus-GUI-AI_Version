from .. import APIError, get_dc
from .common import attachment_settings, find_attachment

API_DOCUMENT_READINGREPORT = 'https://mooc1.chaoxing.com/ananas/job/document'


class ChapterDocument:
    kind = 'document'

    def __init__(self, session, acc, clazzid, courseid, knowledgeid, card_index, objectid, cpi, point_index):
        self.session, self.acc = session, acc
        self.clazzid, self.courseid, self.knowledgeid = clazzid, courseid, knowledgeid
        self.card_index, self.objectid, self.cpi, self.point_index = card_index, objectid, cpi, point_index
        self.title, self.skip_reason = '文档', '非任务点文档或已完成'

    def pre_fetch(self):
        item = find_attachment(attachment_settings(self), self)
        self.title = item.get('property', {}).get('name') or '文档'
        if item.get('isPassed') is True or item.get('job') is not True:
            return False
        self.jobid, self.jtoken = item['jobid'], item['jtoken']
        return True

    def fetch(self):
        return True

    def run(self, context, config):
        context.checkpoint()
        response = self.session.get(API_DOCUMENT_READINGREPORT, params={
            'jobid': self.jobid, 'knowledgeid': self.knowledgeid, 'courseid': self.courseid,
            'clazzid': self.clazzid, 'jtoken': self.jtoken, '_dc': get_dc()})
        response.raise_for_status()
        if response.json().get('status') not in (True, 1, 'true'):
            raise APIError('平台未确认文档阅读完成')
        context.event('progress', value=1, total=1, text='文档阅读记录已提交')
