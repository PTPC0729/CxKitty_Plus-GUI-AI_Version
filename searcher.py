import difflib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

import jsonpath

from cxapi.network import TimeoutSession, error_text
from settings import resolve_path
from runtime import Cancelled


def normalize(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text))).strip('().。?？')


def suffix_filter(text):
    return normalize(text)


class SearcherBase:
    def __init__(self, req_field='question', rsp_field='$.data'):
        self.req_field = req_field
        self.rsp_query = jsonpath.compile(rsp_field)

    def invoke(self, question_value):
        raise NotImplementedError

    def answer(self, question, context):
        from cxapi.jobs.exam import answer_parts, fill_answer
        return fill_answer(question, answer_parts(self, self.invoke(question.value)))

    def close(self):
        pass


class RestAPISearcher(SearcherBase):
    def __init__(self, url, req_field, rsp_field, method='POST'):
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            raise ValueError('题库接口地址必须为 HTTP 或 HTTPS 地址')
        self.session, self.url, self.method = TimeoutSession(), url, method.upper()
        if self.method not in ('GET', 'POST'):
            raise ValueError('题库请求方式只支持 GET 和 POST')
        super().__init__(req_field, rsp_field)

    def invoke(self, question_value):
        try:
            kwargs = {'params' if self.method == 'GET' else 'data': {self.req_field: question_value}}
            response = self.session.request(self.method, self.url, **kwargs)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('题库接口必须返回 JSON 对象')
            return data
        except Cancelled:
            raise
        except Exception as error:
            return {'code': -1, 'error': error_text(error)}

    def close(self):
        self.session.close()


class JsonFileSearcher(SearcherBase):
    def __init__(self, file_path):
        path = resolve_path(file_path)
        if not path.is_file():
            raise ValueError('JSON 题库文件不存在，请在设置中选择有效题库')
        self.db = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(self.db, dict) or not self.db:
            raise ValueError('JSON 题库必须为非空的“题目：答案”字典')
        self.entries = [(q, normalize(q), a) for q, a in self.db.items()]
        super().__init__('', '$.data')

    def invoke(self, question_value):
        target = normalize(question_value)
        exact = [(q, a) for q, normalized, a in self.entries if normalized == target]
        if exact:
            if any(a != exact[0][1] for _, a in exact):
                return {'code': -1, 'error': '相同题目存在冲突答案'}
            return {'code': 1, 'question': exact[0][0], 'data': exact[0][1]}
        candidates = sorted(((difflib.SequenceMatcher(a=target, b=n).ratio(), q, a)
                             for q, n, a in self.entries), key=lambda row: row[0], reverse=True)
        if not candidates or candidates[0][0] < 0.9:
            return {'code': -1, 'error': '题目未匹配'}
        if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.03:
            return {'code': -1, 'error': '相似题目存在歧义，请核对题库'}
        _, question, answer = candidates[0]
        return {'code': 1, 'question': question, 'data': answer}


class SqliteSearcher(SearcherBase):
    def __init__(self, file_path, req_field, rsp_field, table='question'):
        path = resolve_path(file_path)
        if not path.is_file():
            raise ValueError('SQLite 题库文件不存在')
        self.db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        def quote(value):
            return '"' + str(value).replace('"', '""') + '"'
        self.query = f'SELECT {quote(req_field)}, {quote(rsp_field)} FROM {quote(table)} WHERE {quote(req_field)}=?'
        try:
            self.db.execute(self.query + ' LIMIT 0', ('',))
        except sqlite3.Error:
            self.db.close()
            raise ValueError('SQLite 题库的表名或字段配置不正确') from None
        super().__init__(req_field, '$.data')

    def invoke(self, question_value):
        try:
            rows = self.db.execute(self.query + ' LIMIT 2', (question_value,)).fetchall()
            if not rows:
                return {'code': -1, 'error': '题目未匹配'}
            if len(rows) > 1 and rows[0][1] != rows[1][1]:
                return {'code': -1, 'error': '相同题目存在冲突答案'}
            return {'code': 1, 'question': rows[0][0], 'data': rows[0][1]}
        except sqlite3.Error:
            return {'code': -1, 'error': '读取 SQLite 题库失败'}

    def close(self):
        self.db.close()


def create_searcher(config):
    selected = config['use']
    if selected == 'aiSearcher':
        from ai_searcher import AISearcher
        return AISearcher(config['aiSearcher'], config)
    constructors = {'restApiSearcher': RestAPISearcher, 'jsonFileSearcher': JsonFileSearcher,
                    'sqliteSearcher': SqliteSearcher}
    if selected not in constructors:
        raise ValueError('不支持的题库类型')
    return constructors[selected](**config[selected])
