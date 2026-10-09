"""Offline regression checks run inside the distributed executable."""
import json
from urllib.parse import parse_qs

import requests

from cxapi.jobs.video import ChapterVideo, report_endpoint
from cxapi.network import ACCOUNT_STATUS_URL, AuthenticationError, ResourceAccessError, TimeoutSession
from cxapi.schema import AccountInfo
from runtime import Cancelled, TaskContext
from video_progress import resume_position, save_position


def run_reliability_check():
    task = ChapterVideo(None, AccountInfo(987, 'offline', '', '', '', ''), 1, 2, 3, 0, 'offline-video', 4, 0)
    task.jobid, task.duration, task.platform_offset_ms = 'offline-job', 902, 58000
    context = TaskContext()
    save_position(task, 902, context)
    assert resume_position(task, context) == 58, 'Milliseconds or poisoned local end checkpoint were misread'
    task.platform_offset_ms = 580
    assert resume_position(task, context) == 0.58, 'Small offsets must also be milliseconds'
    task.platform_offset_ms = 902000
    assert resume_position(task, context) == 0, 'An unconfirmed end must not trap subsequent runs'
    task.platform_offset_ms = None
    assert resume_position(task, context) == 0, 'Missing platform offset must not revive a local end checkpoint'
    task.platform_offset_ms = 58000
    sent = []
    def report(position):
        sent.append(position)
        return {'isPassed': False}
    def stop(_seconds):
        raise Cancelled()
    task._report, context.wait = report, stop
    try:
        task.run(context, {'speed': 1, 'report_rate': 58})
    except Cancelled:
        pass
    assert sent == [58], 'First video report must use the verified platform position'

    class OfflineSession(TimeoutSession):
        def __init__(self, valid):
            super().__init__()
            self.valid, self.calls = valid, []

        def _request_once(self, method, url, **kwargs):
            self.calls.append((method, url))
            response = requests.Response()
            response.url = url
            response._content_consumed = True
            if url == ACCOUNT_STATUS_URL:
                response.status_code = 200
                response._content = json.dumps({'result': self.valid, 'msg': {'puid': 987}}).encode()
            else:
                response.status_code = 403
                response._content = b'{}'
            return response

    endpoint = 'https://mooc1-api.chaoxing.com/multimedia/log/a/offline'
    for valid, expected in ((True, ResourceAccessError), (False, AuthenticationError)):
        with OfflineSession(valid) as session:
            caught = False
            try:
                session.get(endpoint)
            except expected:
                caught = True
            assert caught, 'Resource permission was confused with account expiration'
            assert session.calls == [('GET', endpoint), ('GET', ACCOUNT_STATUS_URL)], 'The denied request was replayed'
    return {'milliseconds_converted': True, 'local_checkpoint_cannot_override_platform': True,
            'unconfirmed_end_restarts': True, 'video_first_report_verified': True,
            'resource_denial_preserves_valid_login': True, 'expiration_independently_confirmed': True,
            'denied_submission_not_replayed': True}


def run_video_protocol_check():
    """Replay the public player protocol against a local in-memory transport."""
    endpoint = 'https://mooc1-api.chaoxing.com/mooc-ans/multimedia/log/a/4'
    calls = []
    class Transport:
        def get(self, url, **kwargs):
            calls.append(url)
            response = requests.Response()
            response.status_code, response.url, response._content_consumed = 200, url, True
            if '/knowledge/cards' in url:
                payload = {'defaults': {'fid': 1, 'reportUrl': endpoint}, 'attachments': [{
                    'objectId': 'offline-object', 'jobid': 'offline-job', 'job': True, 'isPassed': False,
                    'headOffset': 58000, 'otherInfo': 'nodeId_3-cpi_4-enc_opaque&courseId=2'}]}
                response._content = ('<script>window.AttachmentSetting = ' + json.dumps(payload) + ';</script>').encode()
            elif '/ananas/status/' in url:
                response._content = b'{"dtoken":"offline-token","duration":902}'
            else:
                assert url == endpoint + '/offline-token', 'Platform reportUrl was not honored'
                query = parse_qs(kwargs['params'])
                assert query['courseId'] == ['2'], 'courseId was swallowed into otherInfo'
                assert query['otherInfo'] == ['nodeId_3-cpi_4-enc_opaque']
                assert query['playingTime'] == ['58'] and query['jobid'] == ['offline-job']
                response._content = b'{"isPassed":false}'
            return response
    with TaskContext().bound():
        task = ChapterVideo(Transport(), AccountInfo(987, 'offline', '', '', '', ''),
                            1, 2, 3, 0, 'offline-object', 4, 0)
        assert task.pre_fetch() and task.fetch()
        result = task._report(int(resume_position(task, TaskContext())))
        assert result == {'isPassed': False} and len(calls) == 3
        assert report_endpoint('https://mooc1.chaoxing.com/multimedia/log/a/4', 4).endswith('/multimedia/log/a/4')
    return {'platform_report_url_used': True, 'course_id_is_separate': True,
            'legacy_report_url_supported': True, 'prefetch_fetch_and_report_connected': True}
