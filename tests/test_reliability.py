import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import requests

from cxapi import APIError
from cxapi.api import ChaoXingAPI
from cxapi.jobs.common import find_attachment
from cxapi.jobs.video import ChapterVideo, report_endpoint, report_other_info
from cxapi.network import ACCOUNT_STATUS_URL, AuthenticationError, ResourceAccessError, TimeoutSession
from cxapi.schema import AccountInfo
from engine import run_course
from runtime import Cancelled, TaskContext, TaskControl
from searcher import RestAPISearcher
from settings import DEFAULTS
from test_workflow import course
from video_progress import progress_key, resume_position, save_position

ACCOUNT = AccountInfo(1, 'offline', '', '', '', '')
VIDEO_URL = 'https://mooc1-api.chaoxing.com/multimedia/log/a/1/offline'


def response(status=200, payload=None, url=VIDEO_URL):
    value = requests.Response()
    value.status_code, value.url = status, url
    value._content = json.dumps(payload).encode()
    value._content_consumed = True
    return value


class ReliableVideoTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patcher = patch('video_progress.ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = ChapterVideo(Mock(), ACCOUNT, 1, 2, 3, 0, 'object', 4, 0)
        self.task.jobid, self.task.duration = 'job', 902

    def test_live_millisecond_offset_overrides_poisoned_legacy_end_checkpoint(self):
        # Regression from the actual report: headOffset=58000, duration=902.
        (self.root / 'video_progress.json').write_text(json.dumps({'version': 1, 'entries': {
            progress_key(self.task): {'duration': 902, 'position': 902}}}))
        item = {'jobid': 'job', 'objectId': 'object', 'job': True, 'isPassed': False, 'headOffset': 58000}
        with patch('cxapi.jobs.video.attachment_settings', return_value={'defaults': {'fid': 1}, 'attachments': [item]}):
            self.assertTrue(self.task.pre_fetch())
        self.task._report = Mock(return_value={'isPassed': False})
        context = TaskContext()
        context.wait = Mock(side_effect=Cancelled())
        with self.assertRaises(Cancelled):
            self.task.run(context, {'speed': 1, 'report_rate': 58})
        self.task._report.assert_called_once_with(58)

    def test_offsets_are_always_milliseconds_not_guessed_from_duration(self):
        for raw, expected in ((580, 0.58), ('37000', 37), (841000, 841), (0, 0)):
            with self.subTest(raw=raw):
                self.task.platform_offset_ms = raw
                self.assertEqual(resume_position(self.task, TaskContext()), expected)

    def test_missing_invalid_and_unconfirmed_end_offsets_restart_safely(self):
        save_position(self.task, 902, TaskContext())
        for raw in (None, '', True, -1, float('nan'), float('inf'), 999999999, 902000, 901000):
            with self.subTest(raw=raw):
                self.task.platform_offset_ms = raw
                self.assertEqual(resume_position(self.task, TaskContext()), 0)

    def test_local_report_cannot_override_a_lower_or_reset_platform_position(self):
        save_position(self.task, 600, TaskContext())
        for raw, expected in ((37000, 37), (0, 0), (None, 0)):
            self.task.platform_offset_ms = raw
            self.assertEqual(resume_position(self.task, TaskContext()), expected)

    def test_repeated_runs_cannot_get_trapped_at_unconfirmed_end(self):
        self.task.duration = 2
        self.task.platform_offset_ms = 2000
        context = TaskContext()
        context.wait = Mock(return_value=1)
        for _ in range(2):
            self.task._report = Mock(return_value={'isPassed': False})
            with self.assertRaises(APIError):
                self.task.run(context, {'speed': 1, 'report_rate': 1})
            self.assertEqual(self.task._report.call_args_list[0].args, (0,))

    def test_unknown_report_response_is_not_recorded_as_success(self):
        self.task._report = Mock(return_value={})
        context = TaskContext()
        context.wait = Mock(return_value=1)
        with self.assertRaises(APIError):
            self.task.run(context, {'speed': 1, 'report_rate': 1})
        self.assertFalse((self.root / 'video_progress.json').exists())

    def test_sleep_jump_does_not_advance_video_to_end(self):
        self.task.duration = 3
        self.task._report = Mock(side_effect=lambda played: {'isPassed': played == 3})
        context = TaskContext()
        context.wait = Mock(return_value=3600)
        self.task.run(context, {'speed': 1, 'report_rate': 1})
        self.assertEqual([c.args[0] for c in self.task._report.call_args_list], [0, 1, 2, 3])

    def test_report_query_cannot_split_other_info_into_signed_parameters(self):
        self.task.dtoken, self.task.otherInfo = 'offline', 'node&playingTime=999&jobid=wrong'
        self.task.session.get.return_value = response(payload={'isPassed': False})
        self.task._report(37)
        query = parse_qs(self.task.session.get.call_args.kwargs['params'])
        self.assertEqual(query['playingTime'], ['37'])
        self.assertEqual(query['jobid'], ['job'])
        self.assertEqual(query['otherInfo'], [self.task.otherInfo])

    def test_platform_report_address_and_embedded_course_id_match_player_protocol(self):
        item = {'jobid': 'job', 'objectId': 'object', 'job': True, 'isPassed': False,
                'otherInfo': 'nodeId_3-cpi_4-enc_opaque&courseId=2'}
        endpoint = 'https://mooc1-api.chaoxing.com/mooc-ans/multimedia/log/a/4'
        data = {'defaults': {'fid': 1, 'reportUrl': endpoint}, 'attachments': [item]}
        with patch('cxapi.jobs.video.attachment_settings', return_value=data):
            self.assertTrue(self.task.pre_fetch())
        self.task.dtoken = 'offline'
        self.task.session.get.return_value = response(payload={'isPassed': False})
        self.task._report(58)
        request = self.task.session.get.call_args
        self.assertEqual(request.args[0], endpoint + '/offline')
        query = parse_qs(request.kwargs['params'])
        self.assertEqual(query['courseId'], ['2'])
        self.assertEqual(query['otherInfo'], ['nodeId_3-cpi_4-enc_opaque'])
        self.assertEqual(query['playingTime'], ['58'])
        self.assertEqual(query['clipTime'], ['0_902'])
        # Known signature for these exact identifiers/positions, independently
        # checked against the public player's eight-field template.
        from hashlib import md5
        self.assertEqual(query['enc'], [md5(b'[1][1][job][object][58000][d_yHJ!$pdA~5][902000][0_902]').hexdigest()])
        self.assertIn('/knowledge/cards?', request.kwargs['headers']['Referer'])

    def test_no_embedded_course_field_still_supplies_course_context(self):
        self.task.dtoken, self.task.otherInfo = 'offline', 'nodeId_3'
        self.task.session.get.return_value = response(payload={'isPassed': False})
        self.task._report(0)
        request = self.task.session.get.call_args
        self.assertIn('/mooc-ans/multimedia/log/a/4/', request.args[0])
        self.assertEqual(parse_qs(request.kwargs['params'])['courseId'], ['2'])

    def test_server_can_issue_legacy_or_relative_report_endpoint(self):
        for supplied, expected in (
            ('https://mooc1.chaoxing.com/multimedia/log/a/4', 'https://mooc1.chaoxing.com/multimedia/log/a/4'),
            ('/mooc-ans/multimedia/log/a/4', 'https://mooc1-api.chaoxing.com/mooc-ans/multimedia/log/a/4'),
            ('http://mooc1-api.chaoxing.com/mooc-ans/multimedia/log/a/4/',
             'https://mooc1-api.chaoxing.com/mooc-ans/multimedia/log/a/4')):
            with self.subTest(supplied=supplied):
                self.assertEqual(report_endpoint(supplied, 4), expected)

    def test_untrusted_or_wrong_task_report_endpoint_is_never_called(self):
        for supplied in ('https://example.test/mooc-ans/multimedia/log/a/4',
                         'https://mooc1-api.chaoxing.com.example.test/mooc-ans/multimedia/log/a/4',
                         'https://mooc1-api.chaoxing.com/mooc-ans/multimedia/log/a/99',
                         'https://user:pass@mooc1-api.chaoxing.com/mooc-ans/multimedia/log/a/4',
                         'https://mooc1-api.chaoxing.com/mooc-ans/multimedia/log/a/4?jobid=wrong'):
            with self.subTest(supplied=supplied), self.assertRaises(APIError):
                report_endpoint(supplied, 4)

    def test_embedded_course_cannot_silently_change_report_ownership(self):
        with self.assertRaisesRegex(APIError, '课程参数'):
            report_other_info('nodeId_3&courseId=99', 2)
        with self.assertRaisesRegex(APIError, '课程参数'):
            report_other_info('nodeId_3&courseId=2&courseId=99', 2)

    def test_task_control_does_not_count_system_suspend_time(self):
        control = TaskControl()
        with patch.object(control._condition, 'wait'), patch('runtime.time.monotonic', side_effect=[0, 3600]):
            self.assertEqual(control.wait(0.2), 0.2)

    def test_duplicate_resource_uses_job_identity_and_rejects_ambiguity(self):
        done = {'jobid': 'old-job', 'objectId': 'object', 'isPassed': True}
        pending = {'jobid': 'job', 'objectId': 'object', 'isPassed': False}
        data = {'attachments': [done, None, pending]}
        self.assertIs(find_attachment(data, self.task), pending)
        del self.task.jobid
        with self.assertRaisesRegex(APIError, '多个任务'):
            find_attachment(data, self.task)


class ReliableAuthenticationTests(unittest.TestCase):
    def test_resource_403_with_valid_account_does_not_expire_login_or_replay_report(self):
        for bound in (False, True):
            with self.subTest(bound=bound):
                session = TimeoutSession()
                self.addCleanup(session.close)
                replies = [response(403), response(payload={'result': 1, 'msg': {'puid': 1}}, url=ACCOUNT_STATUS_URL)]
                with patch('requests.Session.request', side_effect=replies) as request:
                    def invoke():
                        with self.assertRaisesRegex(ResourceAccessError, '登录仍有效'):
                            session.get(VIDEO_URL)
                    if bound:
                        with TaskContext().bound():
                            invoke()
                    else:
                        invoke()
                self.assertEqual([c.args[1] for c in request.call_args_list], [VIDEO_URL, ACCOUNT_STATUS_URL])

    def test_expired_login_requires_independent_negative_account_check(self):
        with TimeoutSession() as session, patch('requests.Session.request', side_effect=[
                response(403), response(payload={'result': False}, url=ACCOUNT_STATUS_URL)]):
            with self.assertRaises(AuthenticationError):
                session.get(VIDEO_URL)

    def test_inconclusive_account_check_does_not_claim_expiration(self):
        for probe in (response(503), response(payload={}), requests.Timeout()):
            with self.subTest(probe=type(probe).__name__), TimeoutSession() as session:
                with patch('requests.Session.request', side_effect=[response(401), probe]):
                    with self.assertRaisesRegex(ResourceAccessError, '暂无法确认'):
                        session.get(VIDEO_URL)

    def test_login_redirect_is_verified_separately_and_hostname_is_exact(self):
        with TimeoutSession() as session, patch('requests.Session.request', side_effect=[
                response(url='https://passport2.chaoxing.com/login'),
                response(payload={'result': True, 'msg': {'puid': 1}}, url=ACCOUNT_STATUS_URL)]):
            with self.assertRaises(ResourceAccessError):
                session.get(VIDEO_URL)
        with TimeoutSession() as session, patch('requests.Session.request', return_value=
                response(url='https://passport2.chaoxing.com.example.test/login')) as request:
            self.assertEqual(session.get(VIDEO_URL).status_code, 200)
            self.assertEqual(request.call_count, 1)

    def test_external_bank_403_cannot_expire_platform_account(self):
        with TimeoutSession() as session, patch('requests.Session.request', return_value=response(403)) as request:
            denied = session.get('https://example.test/bank')
            with self.assertRaises(requests.HTTPError):
                denied.raise_for_status()
            self.assertEqual(request.call_count, 1)

    def test_cookie_deletion_in_successful_isolated_request_is_preserved(self):
        session = TimeoutSession()
        self.addCleanup(session.close)
        session.cookies.set('old', 'cookie')
        def send(inner, *args, **kwargs):
            inner.cookies.clear()
            return response(url='https://example.test')
        with patch('requests.Session.request', send), TaskContext().bound():
            session.get('https://example.test')
        self.assertIsNone(session.cookies.get('old'))

    def test_malformed_account_response_does_not_clear_valid_account(self):
        api = ChaoXingAPI()
        self.addCleanup(api.session.close)
        api.acc = ACCOUNT
        with patch.object(api.session, 'get', return_value=response(payload={})):
            with self.assertRaises(APIError):
                api.accinfo()
        self.assertIs(api.acc, ACCOUNT)

    def test_resource_denial_does_not_block_remaining_tasks(self):
        chapters = course(size=1)
        denied = SimpleNamespace(kind='video', title='video', pre_fetch=lambda: True,
            fetch=lambda: True, run=Mock(side_effect=ResourceAccessError('resource denied')))
        good = SimpleNamespace(kind='document', title='doc', pre_fetch=lambda: True, fetch=lambda: True, run=Mock())
        chapters.fetch_points_by_index = Mock(return_value=[denied, good])
        config = copy.deepcopy(DEFAULTS)
        config['exam']['enable'] = False
        for kind in ('video', 'document'):
            config[kind]['wait'] = 0
        with patch('engine.save_json'):
            result = run_course(chapters, config, TaskContext())
        self.assertFalse(result['blocked'])
        self.assertFalse(result['verified_complete'])
        self.assertEqual(result['counts']['failed'], 1)
        good.run.assert_called_once()

    def test_external_bank_does_not_swallow_cancellation(self):
        bank = RestAPISearcher('https://example.test', 'q', '$.data')
        self.addCleanup(bank.close)
        with patch.object(bank.session, 'request', side_effect=Cancelled()):
            with self.assertRaises(Cancelled):
                bank.invoke('question')
