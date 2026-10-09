"""Local HTTP integration check, used in source tests and in the actual EXE."""
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import requests

from ai_searcher import AISearcher
from cxapi.jobs.exam import API_EXAM_COMMIT, PAGE_MOBILE_EXAM, ChapterExam
from cxapi.schema import AccountInfo
from runtime import TaskContext
from settings import DEFAULTS


def run_ai_check(directory):
    calls = {'page': 0, 'ai': 0, 'submit': 0, 'errors': []}
    # Exercise the real page parser with a hash and a zero-prefixed ID. Bypassing
    # fetch() here previously hid failures that occurred before the first AI call.
    answer_id, relation_id = 'ab12cd34ef56ab78cd90ef12ab34cd56', '000099'
    count_token = 'abcdef0123456789abcdef0123456789'
    exam_html = f'''<html><head><meta charset="utf-8"><title>自检测验</title></head><body>
        <h3 class="chapter-title">离线自检</h3>
        <form id="form1" name="form1">
        <input name="workAnswerId" value="{answer_id}">
        <input name="workRelationId" value="{relation_id}">
        <input name="enc_work" value="offline"><input name="totalQuestionNum" value="{count_token}">
        <input name="fullScore" value="100"><input name="workTimesEnc" value="offline-times">
        <div class="zquestions">
        <div class="Py-mian1"><div>
          <div class="Py-m1-title"><span class="quesType">1. [单选题]</span><div class="workTextWrap">单选测试</div></div>
          <input class="answerInput" id="answer1"><input id="answertype1" value="0">
          <ul class="answerList">
            <li><em id-param="A"></em><p><cc>甲</cc></p></li>
            <li><em id-param="B"></em><p><cc>乙</cc></p></li>
          </ul>
        </div></div>
        <div class="Py-mian1"><div>
          <div class="Py-m1-title"><span class="quesType">2. [多选题]</span><div class="workTextWrap">多选测试</div></div>
          <input class="answerInput" id="answers2"><input id="answertype2" value="1">
          <ul class="answerList">
            <li><em id-param="A"></em><p><cc>甲</cc></p></li>
            <li><em id-param="B"></em><p><cc>乙</cc></p></li>
            <li><em id-param="C"></em><p><cc>丙</cc></p></li>
          </ul>
        </div></div>
        <div class="Py-mian1"><div>
          <div class="Py-m1-title"><span class="quesType">3. [判断题]</span><div class="workTextWrap">判断测试</div></div>
          <input class="answerInput" id="answer3"><input id="answertype3" value="3">
          <ul class="answerList">
            <li val-param="true"><p>正确</p></li>
            <li val-param="false"><p>错误</p></li>
          </ul>
        </div></div>
        </div></form>
        <script>function toadd() {{ var a = "2,3,1"; $("#answerwqbid").val(a); }}</script>
        </body></html>'''

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if urlsplit(self.path).path != '/platform-exam':
                calls['errors'].append('unexpected GET URL')
                self.send_error(404)
                return
            calls['page'] += 1
            encoded = exam_html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            try:
                raw = self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/v1/chat/completions':
                    request = json.loads(raw)
                    assert self.headers.get('Authorization') == 'Bearer offline-test-key'
                    assert not self.headers.get('Cookie')
                    assert request['model'] == 'offline-model'
                    question = json.loads(request['messages'][1]['content'])
                    assert set(question) == {'type', 'question', 'options'}
                    assert question['question'] == {
                        '单选题': '单选测试', '多选题': '多选测试', '判断题': '判断测试'}[question['type']]
                    answers = {'单选题': ['B'], '多选题': ['A', 'C'], '判断题': ['true']}[question['type']]
                    calls['ai'] += 1
                    body = {'choices': [{'message': {'content': json.dumps({'answers': answers})}}]}
                elif urlsplit(self.path).path == '/platform-submit':
                    params = parse_qs(urlsplit(self.path).query)
                    data = parse_qs(raw.decode())
                    assert params['workAnswerId'] == [answer_id]
                    assert params['workid'] == [relation_id]
                    assert data['workAnswerId'] == [answer_id]
                    assert data['workRelationId'] == [relation_id]
                    assert data['oldWorkId'] == ['work']
                    assert data['workTimesEnc'] == ['offline-times']
                    assert data['totalQuestionNum'] == [count_token]
                    assert data['answer1'] == ['B']
                    assert data['answer2'] == ['AC']
                    assert data['answer3'] == ['true']
                    assert data['answerwqbid'] == ['2,3,1']
                    calls['submit'] += 1
                    body = {'status': True}
                else:
                    raise AssertionError('unexpected URL')
                code = 200
            except Exception as error:
                calls['errors'].append(type(error).__name__)
                body, code = {'error': 'offline check failed'}, 500
            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    searcher = None

    class PlatformSession(requests.Session):
        def get(self, url, **kwargs):
            assert url == PAGE_MOBILE_EXAM
            return super().get(base + '/platform-exam', timeout=(3, 5), **kwargs)

        def post(self, url, **kwargs):
            assert url == API_EXAM_COMMIT
            return super().post(base + '/platform-submit', timeout=(3, 5), **kwargs)

    platform = PlatformSession()
    platform.trust_env = False
    try:
        config = copy.deepcopy(DEFAULTS['searcher']['aiSearcher'])
        config.update(url=base + '/v1', model='offline-model', api_key='offline-test-key',
                      retries=0, timeout=5, cache_path=str(directory / 'offline-ai-cache.json'))
        searcher = AISearcher(config)
        searcher.session.trust_env = False
        account = AccountInfo(123, '自检账号', '未知', '13000000000', '自检学校', 'offline')
        task = ChapterExam(platform, account, 0, 1, 'work', 'job', 2, 3, 4, 0)
        task.need_jobid, task.ktoken, task.enc = True, 'offline-token', 'offline-enc'
        assert task.fetch()
        task.mount_searcher(searcher)
        task.run(TaskContext(), {})
        assert calls['page'] == 1 and calls['ai'] == 3 and calls['submit'] == 1 and not calls['errors'], calls
        searcher.close()
        searcher = AISearcher(config)
        for question in task.questions:
            assert searcher.answer(question, TaskContext())
        assert calls['ai'] == 3, 'cached answers unexpectedly made a request'
        assert [question.option for question in task.questions] == ['B', 'AC', 'true']
        return {'exam_page_requests': calls['page'], 'opaque_identifiers_preserved': True,
                'encoded_question_count_preserved': True, 'question_manifest_verified': True,
                'ai_requests': calls['ai'], 'automatic_submissions': calls['submit'],
                'cache_survives_restart': True, 'platform_credentials_not_sent': True}
    finally:
        if searcher:
            searcher.close()
        platform.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
