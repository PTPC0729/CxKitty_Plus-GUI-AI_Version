import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from ai_credentials import completion_url, load_api_key, read_credentials, save_api_key
from ai_searcher import AIError, AISearcher, parse_answer
from ai_selfcheck import run_ai_check
from cxapi import APIError
from cxapi.jobs.exam import ChapterExam
from cxapi.schema import AccountInfo, QuestionEnum, QuestionModel
from runtime import Cancelled, TaskContext
from searcher import create_searcher
from settings import DEFAULTS, load_config, save_config


def question(kind=QuestionEnum.单选题):
    return QuestionModel(42, '应当选择哪个选项？', kind, {'A': '甲', 'B': '乙', 'C': '丙'}, '')


class Response:
    def __init__(self, answer='', status=200, data=None):
        self.status_code = status
        self.headers = {}
        self.data = data if data is not None else {'choices': [{'message': {'content': answer}}]}

    def json(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class AIParserTests(unittest.TestCase):
    def test_json_fence_and_reasoning_are_not_submitted(self):
        text = '<think>A and B may work</think>\n```json\n{"answers":["B"]}\n```'
        self.assertEqual(parse_answer(text, question()), 'B')

    def test_multiple_choice_deduplicates_and_orders_by_platform_keys(self):
        self.assertEqual(parse_answer('{"answers":["C","A","C"]}', question(QuestionEnum.多选题)), 'AC')
        self.assertEqual(parse_answer('答案：AC', question(QuestionEnum.多选题)), 'AC')

    def test_correctness_and_confidence_are_not_gated(self):
        self.assertEqual(parse_answer('{"answers":["B"],"confidence":0.01}', question()), 'B')

    def test_judgement_bool_and_text(self):
        for content in ('{"answers":[false]}', '错误', '{"answer":"false"}'):
            self.assertEqual(parse_answer(content, question(QuestionEnum.判断题)), 'false')

    def test_plain_letter_and_exact_option_text(self):
        self.assertEqual(parse_answer('答案：b', question()), 'B')
        self.assertEqual(parse_answer('{"answers":["乙"]}', question()), 'B')

    def test_invalid_empty_and_out_of_range_options_are_rejected(self):
        for content in ('', '{"answers":[]}', '{"answers":["D"]}', '{"answers":["A","B"]}', '{"answers":[null]}', 'A is wrong, choose something else'):
            with self.subTest(content=content), self.assertRaises(AIError):
                parse_answer(content, question())


class AIClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.config = copy.deepcopy(DEFAULTS['searcher']['aiSearcher'])
        self.config.update(url='https://example.test/v1', model='test-model', api_key='private-test-key',
                           cache_path=str(self.folder / 'cache.json'))
        self.client = AISearcher(self.config)
        self.events = []
        self.context = TaskContext(self.events.append)
        self.context.wait = Mock()

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def test_payload_has_full_question_and_no_account_fields(self):
        self.client.session.post = Mock(return_value=Response('{"answers":["A"]}'))
        q = question()
        self.assertTrue(self.client.answer(q, self.context))
        call = self.client.session.post.call_args
        self.assertEqual(call.args[0], 'https://example.test/v1/chat/completions')
        body = call.kwargs['json']
        data = json.loads(body['messages'][1]['content'])
        self.assertEqual(data['options'][1], {'id': 'B', 'text': '乙'})
        self.assertEqual(set(data), {'type', 'question', 'options'})
        self.assertNotIn('private-test-key', json.dumps(body))
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(call.kwargs['timeout'], (8, 90))
        self.assertEqual(q.option, 'A')

    def test_format_failure_and_rate_limit_retry_automatically(self):
        self.client.session.post = Mock(side_effect=[Response(status=429), Response('invalid'), Response('{"answers":["C"]}')])
        q = question()
        self.assertTrue(self.client.answer(q, self.context))
        self.assertEqual(q.option, 'C')
        self.assertEqual(self.client.session.post.call_count, 3)
        self.assertEqual(self.context.wait.call_count, 2)

    def test_auth_failure_not_repeated_for_each_question_or_leaked(self):
        self.client.session.post = Mock(return_value=Response(status=401, data={'error': 'private-test-key'}))
        for _ in range(2):
            with self.assertRaisesRegex(AIError, '401'):
                self.client.answer(question(), self.context)
        self.assertEqual(self.client.session.post.call_count, 1)
        self.assertNotIn('private-test-key', json.dumps(self.events))

    def test_network_errors_are_bounded_and_do_not_leak_url_or_key(self):
        self.client.session.post = Mock(side_effect=requests.Timeout('secret private-test-key URL'))
        with self.assertRaisesRegex(AIError, '超时') as error:
            self.client.answer(question(), self.context)
        self.assertEqual(self.client.session.post.call_count, 3)
        self.assertNotIn('private-test-key', str(error.exception) + json.dumps(self.events))

    def test_cancellation_after_network_return_does_not_set_answer_or_cache(self):
        def response(*args, **kwargs):
            self.context.control.cancel()
            return Response('{"answers":["A"]}')
        self.client.session.post = Mock(side_effect=response)
        q = question()
        with self.assertRaises(Cancelled):
            self.client.answer(q, self.context)
        self.assertEqual(q.option, '')
        self.assertFalse(self.client.cache_path.exists())

    def test_cache_persists_but_changed_model_or_options_needs_new_request(self):
        self.client.session.post = Mock(return_value=Response('{"answers":["B"]}'))
        self.client.answer(question(), self.context)
        another = AISearcher(self.config)
        try:
            another.session.post = Mock(return_value=Response('{"answers":["A"]}'))
            self.assertTrue(another.answer(question(), self.context))
            another.session.post.assert_not_called()
            reordered = question()
            reordered.answers = {'A': '乙', 'B': '甲', 'C': '丙'}
            self.assertTrue(another.answer(reordered, self.context))
            self.assertEqual(another.session.post.call_count, 1)
            another.model = 'different-model'
            another.answer(question(), self.context)
            self.assertEqual(another.session.post.call_count, 2)
        finally:
            another.close()

    def test_broken_cache_does_not_block_answers_or_overwrite_file(self):
        self.client.cache_path.write_text('{broken', encoding='utf-8')
        another = AISearcher(self.config)
        try:
            another.session.post = Mock(return_value=Response('B'))
            self.assertTrue(another.answer(question(), self.context))
            self.assertEqual(another.cache_path.read_text(encoding='utf-8'), '{broken')
        finally:
            another.close()

    def test_bank_first_uses_existing_bank_then_ai_on_missing_match(self):
        bank = self.folder / 'bank.json'
        bank.write_text(json.dumps({'应当选择哪个选项？': '乙'}), encoding='utf-8')
        configs = copy.deepcopy(DEFAULTS['searcher'])
        configs.update(use='aiSearcher', aiSearcher={**self.config, 'strategy': 'bank_first'})
        configs['jsonFileSearcher']['file_path'] = str(bank)
        another = create_searcher(configs)
        try:
            another.session.post = Mock(return_value=Response('A'))
            q = question()
            self.assertTrue(another.answer(q, self.context))
            self.assertEqual(q.option, 'B')
            another.session.post.assert_not_called()
            q.value = '完全不同的问题'
            self.assertTrue(another.answer(q, self.context))
            self.assertEqual(q.option, 'A')
            another.session.post.assert_called_once()
        finally:
            another.close()

    def test_missing_local_bank_still_allows_ai(self):
        configs = copy.deepcopy(DEFAULTS['searcher'])
        configs.update(use='aiSearcher', aiSearcher={**self.config, 'strategy': 'bank_first'})
        configs['jsonFileSearcher']['file_path'] = str(self.folder / 'missing.json')
        another = create_searcher(configs)
        try:
            another.session.post = Mock(return_value=Response('A'))
            self.assertTrue(another.answer(question(), self.context))
        finally:
            another.close()

    def test_reasoning_token_parameter_and_content_blocks(self):
        self.client.config['token_parameter'] = 'max_completion_tokens'
        self.client.session.post = Mock(return_value=Response(data={'choices': [{'message': {'content': [{'type': 'text', 'text': '{"answers":["A"]}'}]}}]}))
        self.client.answer(question(), self.context)
        payload = self.client.session.post.call_args.kwargs['json']
        self.assertEqual(payload['max_completion_tokens'], 4096)
        self.assertNotIn('max_tokens', payload)

    def test_failed_ai_question_prevents_partial_exam_submission(self):
        account = AccountInfo(1, 'test', '未知', '13000000000', 'test', 'test')
        task = ChapterExam(Mock(), account, 0, 1, 'work', 'job', 2, 3, 4, 0)
        task.questions = [question()]
        task.mount_searcher(self.client)
        self.client.session.post = Mock(return_value=Response(status=401))
        with self.assertRaises(APIError):
            task.run(self.context, {})
        task.session.post.assert_not_called()


class AICredentialTests(unittest.TestCase):
    def test_url_normalization(self):
        self.assertEqual(completion_url('https://example.test'), 'https://example.test/v1/chat/completions')
        self.assertEqual(completion_url('https://example.test/custom/'), 'https://example.test/custom/chat/completions')
        self.assertEqual(completion_url('https://example.test/v1/chat/completions'), 'https://example.test/v1/chat/completions')
        for url in ('', 'file:///tmp/test', 'https://user:pass@example.test/v1'):
            with self.assertRaises(ValueError):
                completion_url(url)

    def test_keys_are_external_and_scoped_to_endpoint(self):
        with tempfile.TemporaryDirectory() as directory, patch('ai_credentials.ROOT', Path(directory)), patch.dict(os.environ, {}, clear=True):
            save_api_key('https://example.test/v1', 'private-key')
            self.assertEqual(load_api_key('https://example.test/v1/chat/completions'), 'private-key')
            self.assertEqual(load_api_key('https://another.test/v1'), '')
            self.assertFalse((Path(directory) / 'config.yml').exists())
            save_api_key('https://example.test/v1', '')
            self.assertEqual(load_api_key('https://example.test/v1'), '')

    def test_corrupt_credential_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory, patch('ai_credentials.ROOT', Path(directory)):
            file = Path(directory) / 'ai_credentials.json'
            file.write_text('broken', encoding='utf-8')
            with self.assertRaises(ValueError):
                save_api_key('https://example.test', 'key')
            self.assertEqual(file.read_text(), 'broken')

    def test_config_roundtrip_keeps_ai_settings_but_not_transient_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yml'
            config = copy.deepcopy(DEFAULTS)
            config['searcher']['aiSearcher'].update(url='https://example.test/v1', model='model', api_key='never-in-yaml')
            save_config(config, path)
            self.assertNotIn('never-in-yaml', path.read_text())
            self.assertEqual(load_config(path)['searcher']['aiSearcher']['model'], 'model')
            self.assertEqual(config['searcher']['aiSearcher']['api_key'], 'never-in-yaml')

    def test_real_http_model_answers_flow_into_automatic_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_ai_check(Path(directory))
            self.assertEqual(result['exam_page_requests'], 1)
            self.assertTrue(result['opaque_identifiers_preserved'])
            self.assertTrue(result['encoded_question_count_preserved'])
            self.assertTrue(result['question_manifest_verified'])
            self.assertEqual(result['ai_requests'], 3)
            self.assertEqual(result['automatic_submissions'], 1)
            self.assertTrue(result['cache_survives_restart'])


if __name__ == '__main__':
    unittest.main()
