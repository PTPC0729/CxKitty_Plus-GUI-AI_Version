import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import lxml.html

from cxapi import APIError
from cxapi.api import ChaoXingAPI
from cxapi.chapters import ClassChapters
from cxapi.jobs.common import attachment_settings, find_attachment
from cxapi.jobs.exam import ChapterExam, answer_parts, fill_answer
from cxapi.jobs.video import ChapterVideo
from cxapi.network import AuthenticationError, TimeoutSession
from cxapi.schema import AccountInfo, ChapterModel, QuestionEnum, QuestionModel
from engine import run_course
from runtime import Cancelled, TaskContext, TaskControl
from searcher import JsonFileSearcher, SearcherBase, SqliteSearcher
from settings import DEFAULTS, load_config, save_config
from utils import ck2dict, save_session, sessions_load

ACCOUNT = AccountInfo(123, '测试', '未知', '13000000000', '测试学校', 'test')


def response(data=None, text=''):
    result = Mock()
    result.json.return_value = data
    result.text = text
    return result


def chapters(session=None):
    return ClassChapters(session or Mock(), ACCOUNT, 1, 2, 3, [
        {'id': 1, 'jobcount': 1, 'indexorder': 1, 'name': '章节', 'label': '1.1', 'layer': 1, 'status': ''}])


class StorageTests(unittest.TestCase):
    def test_cookie_with_equals(self):
        self.assertEqual(ck2dict(' a=abc==; b=2;'), {'a': 'abc==', 'b': '2'})
        with self.assertRaises(ValueError):
            ck2dict(None)

    def test_old_session_round_trip_and_bad_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / '13000000000.json'
            path.write_text(json.dumps({'phone': '13000000000', 'puid': 123, 'passwd': 'old-password',
                                       'name': '测试', 'ck': 'a=abc==;', 'extension': 7}), encoding='utf8')
            (folder / 'broken.json').write_text('{', encoding='utf8')
            warnings = []
            sessions = sessions_load(folder, warnings.append)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].passwd, 'old-password')
            self.assertEqual(len(warnings), 1)
            api = SimpleNamespace(acc=ACCOUNT, ck_dump=lambda: {'a': 'new=='})
            save_session(api, 'old-password', folder)
            data = json.loads(path.read_text(encoding='utf8'))
            self.assertEqual(data['extension'], 7)
            self.assertEqual(data['ck'], 'a=new==;')
            self.assertEqual(data['passwd'], 'old-password')
            self.assertEqual(list(folder.glob('*.tmp')), [])

    def test_config_preserves_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yml'
            path.write_text('video:\n  speed: 1.25\ncustom: preserved\n', encoding='utf8')
            config = load_config(path)
            self.assertEqual(config['video']['speed'], 1.25)
            self.assertEqual(config['video']['wait'], 15)
            save_config(config, path)
            self.assertEqual(load_config(path)['custom'], 'preserved')

    def test_invalid_config_rejected_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yml'
            path.write_text('video:\n  speed: 0\n', encoding='utf8')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                load_config(path)
            self.assertEqual(path.read_bytes(), before)


class NetworkTests(unittest.TestCase):
    def test_default_timeout_and_no_submission_replay(self):
        session = TimeoutSession()
        result = Mock(status_code=200, url='https://example.test/')
        with patch('requests.Session.request', return_value=result) as request:
            session.post('https://example.test/', data={'answer': 'A'})
            self.assertEqual(request.call_args.kwargs['timeout'], (8, 20))
        retry = session.get_adapter('https://').max_retries
        self.assertEqual(retry.read, 0)
        self.assertEqual(retry.status, 0)

    def test_authentication_failure(self):
        with patch('requests.Session.request', return_value=Mock(status_code=403)), \
                patch.object(TimeoutSession, 'account_status', return_value=False):
            with self.assertRaises(AuthenticationError):
                TimeoutSession().get('https://mooc1-api.chaoxing.com/knowledge/cards')

    def test_old_cookie_initializes_account(self):
        api = ChaoXingAPI()
        api.ck_load({'session': 'abc=='})
        with patch.object(api.session, 'get', return_value=response({'result': 1, 'msg': {
                'puid': 123, 'name': '测试', 'phone': '13000000000', 'sex': 5}})):
            self.assertTrue(api.accinfo())
            self.assertEqual(api.acc.sex, '未知')
        api.session.close()


class ChapterTests(unittest.TestCase):
    def test_empty_response_is_iterable(self):
        value = chapters()
        value.session.get.return_value = response({'data': []})
        self.assertEqual(value.fetch_points_by_index(0), [])

    def test_missing_response_is_not_silent_success(self):
        value = chapters()
        value.session.get.return_value = response({'error': 'failed'})
        with self.assertRaises(APIError):
            value.fetch_points_by_index(0)

    def test_disabled_exam_does_not_parse_broken_data(self):
        value = chapters()
        value.session.get.return_value = response({'data': [{'card': {'data': [
            {'description': '<iframe module="work" data="broken"></iframe>'}]}}]})
        self.assertEqual(value.fetch_points_by_index(0, enabled={'video'}), [])
        self.assertEqual(value.point_issues[0][0], 'skipped')

    def test_one_bad_card_does_not_discard_valid_task(self):
        value = chapters()
        html = '<iframe module="insertvideo" data="broken"></iframe><iframe module="insertdoc" data=\'{"objectid":"doc"}\'></iframe>'
        value.session.get.return_value = response({'data': [{'card': {'data': [{'description': html}]}}]})
        tasks = value.fetch_points_by_index(0)
        self.assertEqual([task.kind for task in tasks], ['document'])
        self.assertEqual(value.point_issues[0][0], 'failed')

    def test_attachment_parser_handles_multiline_and_order_changes(self):
        data = {'defaults': {}, 'attachments': [{'property': {'objectid': 'other'}}, {'property': {'objectid': 'wanted'}}]}
        task = SimpleNamespace(session=Mock(), clazzid=1, courseid=2, knowledgeid=3, card_index=0, cpi=4, objectid='wanted')
        task.session.get.return_value = response(text='window.AttachmentSetting = ' + json.dumps(data, indent=2) + ';')
        parsed = attachment_settings(task)
        self.assertEqual(find_attachment(parsed, task)['property']['objectid'], 'wanted')


class AnswerTests(unittest.TestCase):
    def test_json_exact_match_wins_over_earlier_similar_question(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bank.json'
            path.write_text(json.dumps({'Which number is 1234567891?': 'wrong', 'Which number is 1234567890?': 'right'}))
            self.assertEqual(JsonFileSearcher(path).invoke('Which number is 1234567890?')['data'], 'right')

    def test_missing_sqlite_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.db'
            with self.assertRaises(ValueError):
                SqliteSearcher(path, 'question', 'answer')
            self.assertFalse(path.exists())

    def test_partial_multiple_choice_rejected(self):
        question = QuestionModel(1, 'test', QuestionEnum.多选题, {'A': '甲', 'B': '乙'}, '')
        self.assertFalse(fill_answer(question, ['甲', '丙']))
        self.assertEqual(question.option, '')
        self.assertTrue(fill_answer(question, ['乙', '甲']))
        self.assertEqual(question.option, 'AB')
        self.assertFalse(fill_answer(question, ['甲', '甲']))

    def test_jsonpath_all_answers_and_null(self):
        searcher = SearcherBase(rsp_field='$.data[*]')
        self.assertEqual(answer_parts(searcher, {'code': 1, 'data': ['甲', '乙']}), ['甲', '乙'])
        self.assertEqual(answer_parts(SearcherBase(), {'code': 1, 'data': None}), [])

    def test_judgement_uses_exact_tokens(self):
        question = QuestionModel(1, 'test', QuestionEnum.判断题, {}, '')
        self.assertFalse(fill_answer(question, ['不正确']))
        self.assertTrue(fill_answer(question, ['正确']))

    def test_empty_or_partial_exam_never_accepted(self):
        task = ChapterExam(Mock(), ACCOUNT, 0, 1, 'work', 'job', 2, 3, 4, 0)
        task.need_jobid, task.ktoken, task.enc = True, 'token', 'enc'
        fields = {'workAnswerId': '1', 'workRelationId': '2', 'enc_work': 'enc', 'totalQuestionNum': '1', 'fullScore': '100'}
        html = '<html><title>测验</title>' + ''.join(f'<input name="{k}" value="{v}">' for k, v in fields.items()) + '</html>'
        task.session.get.return_value = response(text=html)
        with self.assertRaisesRegex(APIError, '题数'):
            task.fetch()
        task.session.post.assert_not_called()
        self.assertEqual(task.workRelationId, '2')

    def make_exam(self, **overrides):
        task = ChapterExam(Mock(), ACCOUNT, 0, 1, 'work', 'job', 2, 3, 4, 0)
        task.need_jobid, task.ktoken, task.enc = True, 'token', 'enc'
        fields = {'workAnswerId': '1', 'workRelationId': '99', 'enc_work': 'enc', 'totalQuestionNum': '1', 'fullScore': '100'}
        fields.update(overrides)
        html = '<html><title>测验</title>' + ''.join(
            f'<input name="{key}" value="{value}">' for key, value in fields.items() if value is not None)
        html += '''<div class="zquestions"><div class="Py-mian1 extra"><div>
            <div class="Py-m1-title"><i>1.</i><span>（单选题，100分）</span>中国首都是？</div>
            <input class="answerInput" id="answer42"><input id="answertype42" value="0">
            <ul class="answerList"><li><em id-param="A"></em><p><cc>北京</cc></p></li>
            <li><em id-param="B"></em><p><cc>上海</cc></p></li></ul>
            </div></div></div></html>'''
        task.session.get.return_value = response(text=html)
        task.session.post.return_value = response({'status': True})
        return task

    def test_complete_exam_preserves_platform_ids_in_submission(self):
        opaque = 'ab12cd34ef56ab78cd90ef12ab34cd56'
        for answer_id, relation_id in (('1', '99'), (opaque, '99'), ('1', opaque),
                                       (opaque, opaque), ('000001', '000099')):
            with self.subTest(answer_id=answer_id, relation_id=relation_id):
                task = self.make_exam(workAnswerId=answer_id, workRelationId=relation_id)
                self.assertTrue(task.fetch())
                self.assertEqual(task.questions[0].value, '中国首都是？')
                searcher = SearcherBase()
                searcher.invoke = Mock(return_value={'code': 1, 'data': '北京'})
                task.mount_searcher(searcher)
                task.run(TaskContext(), {})
                task.session.post.assert_called_once()
                request = task.session.post.call_args.kwargs
                self.assertEqual(request['params']['workAnswerId'], answer_id)
                self.assertEqual(request['params']['workid'], relation_id)
                self.assertEqual(request['params']['cpi'], 4)
                self.assertEqual(request['data']['answer42'], 'A')
                self.assertEqual(request['data']['workAnswerId'], answer_id)
                self.assertEqual(request['data']['workRelationId'], relation_id)
                self.assertEqual(request['data']['oldWorkId'], 'work')

    def test_missing_exam_ids_still_rejected(self):
        for field in ('workAnswerId', 'workRelationId'):
            for value in (None, '', '   '):
                with self.subTest(field=field, value=value):
                    task = self.make_exam(**{field: value})
                    with self.assertRaisesRegex(APIError, field):
                        task.fetch()
                    task.session.post.assert_not_called()

    def test_invalid_exam_count_has_field_specific_error(self):
        for value in ('not-a-count', '1.5', '-1', '0'):
            with self.subTest(value=value):
                task = self.make_exam(totalQuestionNum=value)
                with self.assertRaisesRegex(APIError, 'totalQuestionNum'):
                    task.fetch()
                task.session.post.assert_not_called()

    def make_mobile_exam(self, transform=lambda html: html):
        task = self.make_exam()
        html = (Path(__file__).parent / 'fixtures' / 'mobile_exam_encoded_count.html').read_text(encoding='utf-8')
        task.session.get.return_value = response(text=transform(html))
        return task

    def test_encoded_count_uses_complete_manifest_and_keeps_original_submission_value(self):
        task = self.make_mobile_exam()
        self.assertTrue(task.fetch())
        self.assertEqual([q.value for q in task.questions], ['单选测试', '判断测试一', '判断测试二', '多选测试'])
        searcher = SearcherBase()
        answers = {'单选测试': '乙', '判断测试一': '正确', '判断测试二': '错误', '多选测试': '甲#丙'}
        searcher.invoke = lambda question: {'code': 1, 'data': answers[question]}
        task.mount_searcher(searcher)
        task.run(TaskContext(), {})
        data = task.session.post.call_args.kwargs['data']
        self.assertEqual(data['totalQuestionNum'], 'abcdef0123456789abcdef0123456789')
        self.assertEqual(data['answerwqbid'], '101,104,103,102')
        self.assertEqual([data['answer' + str(i)] for i in (101, 102, 103, 104)], ['B', 'true', 'false', 'AC'])

    def test_encoded_count_cannot_hide_missing_question(self):
        def omit_question(html):
            root = lxml.html.fromstring(html)
            root.xpath('//div[@class="Py-mian1"]')[-1].drop_tree()
            return lxml.html.tostring(root, encoding='unicode')
        task = self.make_mobile_exam(omit_question)
        with self.assertRaisesRegex(APIError, '提交清单不一致'):
            task.fetch()
        task.session.post.assert_not_called()

    def test_unrecognized_question_container_cannot_be_silently_skipped(self):
        task = self.make_mobile_exam(lambda html: html.replace('class="Py-mian1"', 'class="new-layout"', 1))
        with self.assertRaisesRegex(APIError, '表单题目清单不一致'):
            task.fetch()

    def test_encoded_count_requires_independent_question_list(self):
        task = self.make_mobile_exam(lambda html: html[:html.index('<script>')] + '</body></html>')
        with self.assertRaisesRegex(APIError, '缺少完整题目清单'):
            task.fetch()

    def test_duplicate_and_conflicting_platform_lists_rejected(self):
        for manifest in ('101,104,103,102,101', '101,104,103,999'):
            with self.subTest(manifest=manifest):
                task = self.make_mobile_exam(lambda html: html.replace('101,104,103,102', manifest, 1))
                with self.assertRaisesRegex(APIError, '题目清单'):
                    task.fetch()

    def test_hidden_question_list_and_zero_prefixed_question_ids(self):
        def hidden_list(html):
            html = html[:html.index('<script>')] + '</body></html>'
            return html.replace('id="answerwqbid">', 'id="answerwqbid" value="101,104,103,102,">').replace('101', '00101')
        task = self.make_mobile_exam(hidden_list)
        self.assertTrue(task.fetch())
        self.assertEqual(task.questions[0].question_id, '00101')
        self.assertEqual(task.answerwqbid, '00101,104,103,102')

    def test_fields_in_unrelated_form_do_not_override_exam(self):
        task = self.make_mobile_exam(lambda html: html.replace('<form name="form1"',
            '<form><input name="totalQuestionNum" value="invalid"><input name="workRelationId" value="other"></form><form name="form1"'))
        self.assertTrue(task.fetch())
        self.assertEqual(task.workRelationId, '67890')

    def test_conflicting_fields_inside_exam_rejected(self):
        task = self.make_mobile_exam(lambda html: html.replace('</form>', '<input name="workAnswerId" value="different"></form>'))
        with self.assertRaisesRegex(APIError, 'workAnswerId.*冲突'):
            task.fetch()


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch('video_progress.ROOT', Path(self.directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_video(self):
        task = ChapterVideo(Mock(), ACCOUNT, 1, 2, 3, 0, 'object', 4, 0)
        task.jobid = 'offline-job'
        task.duration = 2
        return task

    def test_fractional_speed_reaches_exact_end(self):
        task = self.make_video()
        reports = []
        def report(played):
            reports.append(played)
            return {'isPassed': played == task.duration}
        task._report = report
        context = TaskContext()
        context.wait = Mock(return_value=0.25)
        task.run(context, {'speed': 0.5, 'report_rate': 58})
        self.assertEqual(reports, [0, 2])
        self.assertEqual(context.wait.call_count, 16)

    def test_unconfirmed_end_is_bounded(self):
        task = self.make_video()
        task._report = Mock(return_value={'isPassed': False})
        context = TaskContext()
        context.wait = Mock(return_value=1)
        with self.assertRaises(APIError):
            task.run(context, {'speed': 1, 'report_rate': 1})
        self.assertLessEqual(task._report.call_count, 6)
        self.assertLessEqual(max(call.args[0] for call in task._report.call_args_list), task.duration)

    def test_auth_failure_is_not_retried(self):
        task = self.make_video()
        task._report = Mock(side_effect=AuthenticationError('expired'))
        with self.assertRaises(AuthenticationError):
            task.run(TaskContext(), {'speed': 1, 'report_rate': 1})
        self.assertEqual(task._report.call_count, 1)


class RunnerTests(unittest.TestCase):
    def test_task_failure_continues_and_summary_is_honest(self):
        data = chapters()
        data.chapters[0].point_total = 2
        bad = SimpleNamespace(kind='video', title='bad', pre_fetch=Mock(side_effect=ValueError('bad data')))
        good = SimpleNamespace(kind='document', title='good', pre_fetch=lambda: True,
                               fetch=lambda: True, run=Mock())
        data.fetch_point_status = Mock()
        data.fetch_points_by_index = Mock(return_value=[bad, good])
        config = copy.deepcopy(DEFAULTS)
        for kind in ('video', 'document', 'exam'):
            config[kind]['wait'] = 0
        config['exam']['enable'] = False
        with tempfile.TemporaryDirectory() as directory:
            result = run_course(data, config, TaskContext(), report_dir=Path(directory))
            self.assertTrue(Path(result['report_path']).is_file())
        good.run.assert_called_once()
        self.assertEqual(result['counts']['failed'], 1)
        self.assertEqual(result['counts']['success'], 1)
        self.assertFalse(result['verified_complete'])
        self.assertEqual(result['retry_chapters'], [1])

    def test_zero_counts_with_known_jobs_are_not_complete(self):
        data = chapters()
        data.chapters[0].point_total = 0
        data.fetch_point_status = Mock()
        data.fetch_points_by_index = Mock(return_value=[])
        config = copy.deepcopy(DEFAULTS)
        config['exam']['enable'] = False
        with tempfile.TemporaryDirectory() as directory:
            result = run_course(data, config, TaskContext(), report_dir=Path(directory))
        self.assertFalse(result['verified_complete'])

    def test_cancel_wakes_paused_worker(self):
        control = TaskControl()
        control.set_paused(True)
        stopped = threading.Event()
        def run():
            try:
                control.checkpoint()
            except Cancelled:
                stopped.set()
        worker = threading.Thread(target=run)
        worker.start()
        control.cancel()
        worker.join(1)
        self.assertTrue(stopped.is_set())
        self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
