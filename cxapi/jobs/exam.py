import difflib
import re

import lxml.html

from searcher import normalize
from runtime import Cancelled
from .. import APIError
from ..schema import QuestionEnum, QuestionModel
from .common import attachment_settings, find_attachment

API_EXAM_COMMIT = 'https://mooc1-api.chaoxing.com/work/addStudentWorkNew'
PAGE_MOBILE_EXAM = 'https://mooc1-api.chaoxing.com/android/mworkspecial'


def declared_question_ids(root, form):
    """Read literal submission lists without evaluating the platform's JavaScript."""
    lists = [value for value in form.xpath(".//input[@name='answerwqbid']/@value") if value.strip()]
    for script in root.xpath('//script[not(@src)]/text()'):
        # Tokenize literals and simple assignments, never execute page scripts.
        # Comments, unrelated variables and semicolon-free code are harmless.
        tokens = re.findall(r'''/\*.*?\*/|//[^\r\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[\w$]+|[^\s]''', script, re.S)
        tokens = [token for token in tokens if not token.startswith(('//', '/*'))]
        scopes = [{}]
        def literal(token):
            return token[1:-1] if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0] else None
        def lookup(token):
            value = literal(token)
            if value is not None:
                return value
            return next((scope[token] for scope in reversed(scopes) if token in scope), None)
        for index, token in enumerate(tokens):
            if token == '{':
                scopes.append({})
            elif token == '}' and len(scopes) > 1:
                scopes.pop()
            elif re.fullmatch(r'[A-Za-z_$][\w$]*', token) and tokens[index + 1:index + 2] == ['=']:
                scopes[-1][token] = lookup(tokens[index + 2]) if index + 2 < len(tokens) else None
            elif (token in ('$', 'jQuery') and tokens[index + 1:index + 2] == ['(']
                  and index + 8 < len(tokens) and literal(tokens[index + 2]) == '#answerwqbid'
                  and tokens[index + 3:index + 7] == [')', '.', 'val', '(']
                  and tokens[index + 8] == ')'):
                value = lookup(tokens[index + 7])
                if value is not None:
                    lists.append(value)
    expected = None
    for value in lists:
        if not re.fullmatch(r'\s*\d+(?:\s*,\s*\d+)*\s*,?\s*', value):
            raise APIError('测验题目清单格式无效，未提交')
        ids = [item.strip() for item in value.strip().rstrip(',').split(',')]
        if len(ids) != len(set(ids)):
            raise APIError('测验题目清单包含重复编号，未提交')
        if expected is not None and set(ids) != set(expected):
            raise APIError('测验页面中的题目清单不一致，未提交')
        if expected is None:
            expected = ids
    return expected


def answer_parts(searcher, response):
    if not isinstance(response, dict) or response.get('code') not in (1, '1'):
        return []
    values = searcher.rsp_query.parse(response)
    parts = []
    for value in values:
        values_list = value if isinstance(value, list) else [value]
        for item in values_list:
            if isinstance(item, bool):
                item = '正确' if item else '错误'
            if not isinstance(item, (str, int, float)):
                return []
            parts.extend(part.strip() for part in re.split(r'[#;；]', str(item)) if part.strip())
    return parts


def match_option(answers, text):
    text = normalize(text)
    exact = [key for key, value in answers.items() if normalize(value) == text]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    candidates = sorted(((difflib.SequenceMatcher(a=normalize(value), b=text).ratio(), key)
                         for key, value in answers.items()), reverse=True)
    if not candidates or candidates[0][0] < 0.9:
        return None
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.03:
        return None
    return candidates[0][1]


def fill_answer(question, parts):
    question.option = ''
    if not parts:
        return False
    if question.question_type == QuestionEnum.判断题:
        if len(parts) != 1:
            return False
        value = normalize(parts[0]).lower()
        if value in ('对', '正确', '√', 'true', '1'):
            question.option = 'true'
        elif value in ('错', '错误', '×', 'false', '0'):
            question.option = 'false'
        return bool(question.option)
    if question.question_type == QuestionEnum.单选题 and len(parts) != 1:
        return False
    options = [match_option(question.answers, part) for part in parts]
    if any(option is None for option in options) or len(set(options)) != len(options):
        return False
    question.option = ''.join(sorted(options))
    return bool(question.option)


class ChapterExam:
    kind = 'exam'

    def __init__(self, session, acc, card_index, courseid, workid, jobid, knowledgeid, clazzid, cpi, point_index):
        self.session, self.acc = session, acc
        self.card_index, self.courseid, self.workid, self.jobid = card_index, courseid, workid, jobid
        self.knowledgeid, self.clazzid, self.cpi, self.point_index = knowledgeid, clazzid, cpi, point_index
        self.title, self.skip_reason = '章节测验', '测验已提交或已完成'
        self.answerwqbid = None

    def pre_fetch(self):
        data = attachment_settings(self)
        item = find_attachment(data, self)
        if item.get('isPassed') is True or item.get('job') is False:
            return False
        self.ktoken, self.enc = data['defaults']['ktoken'], item['enc']
        self.need_jobid = item.get('job') is not None
        return True

    def fetch(self):
        response = self.session.get(PAGE_MOBILE_EXAM, params={
            'courseid': self.courseid, 'workid': self.workid,
            'jobid': self.jobid if self.need_jobid else '', 'needRedirect': 'true',
            'knowledgeid': self.knowledgeid, 'userid': self.acc.puid, 'ut': 's',
            'clazzId': self.clazzid, 'cpi': self.cpi, 'ktoken': self.ktoken, 'enc': self.enc})
        response.raise_for_status()
        root = lxml.html.fromstring(response.text)
        title = ''.join(root.xpath('//title/text()'))
        if re.search('已批阅|已提交', title):
            return False
        if any('无效的权限' in text for text in root.xpath("//p[contains(@class,'blankTips')]/text()")):
            raise APIError('当前测验访问权限无效')
        titles = root.xpath("//h3[contains(@class,'chapter-title') or contains(@class,'py-Title')]")
        self.title = titles[0].text_content().strip() if titles else '章节测验'
        forms = root.xpath("//form[.//input[@name='workAnswerId'] and .//input[@name='totalQuestionNum']]")
        if len(forms) > 1:
            raise APIError('页面包含多份测验表单，无法确定提交内容，未提交')
        form = forms[0] if forms else root
        def field(name):
            values = form.xpath('.//input[@name=$name]/@value', name=name)
            if not values or not values[0].strip():
                raise APIError(f'测验缺少必要表单字段 {name}，未提交')
            if len(set(values)) != 1:
                raise APIError(f'测验表单字段 {name} 存在冲突，未提交')
            return values[0]
        # Platform identifiers are opaque: newer pages can use hashes, and numeric
        # identifiers may contain leading zeros. Submit exactly what the page sent.
        self.workAnswerId = field('workAnswerId')
        self.workRelationId = field('workRelationId')
        self.enc_work, self.totalQuestionNum, self.fullScore = field('enc_work'), field('totalQuestionNum'), field('fullScore')
        # totalQuestionNum is an opaque 32-character value on current mobile
        # pages. Keep the wire value; use the independent question list below.
        raw_count = self.totalQuestionNum.strip()
        question_count = None
        if re.fullmatch(r'[a-fA-F0-9]{32}', raw_count):
            pass
        elif re.fullmatch(r'[0-9]+', raw_count):
            question_count = int(raw_count)
            if question_count <= 0:
                raise APIError('测验题数字段 totalQuestionNum 必须大于零，未提交')
        else:
            raise APIError('测验题数字段 totalQuestionNum 无效，未提交') from None
        expected_ids = declared_question_ids(root, form)
        extras = form.xpath(".//input[@name='workTimesEnc']/@value")
        self.workTimesEnc = extras[0] if extras else ''
        self.questions = []
        nodes = form.xpath(".//div[contains(concat(' ',normalize-space(@class),' '),' zquestions ')]/div[contains(concat(' ',normalize-space(@class),' '),' Py-mian1 ')]")
        for node in nodes:
            title_nodes = node.xpath("div/div[contains(@class,'Py-m1-title')]")
            ids = node.xpath("div/input[contains(@class,'answerInput')]/@id")
            types = node.xpath("div/input[starts-with(@id,'answertype')]/@value")
            if not title_nodes or not ids or not types:
                raise APIError('测验题目结构无法识别，未提交')
            match = re.fullmatch(r'answers?(\d+)', ids[0])
            if not match:
                raise APIError('测验题目编号无效，未提交')
            try:
                kind = QuestionEnum(int(types[0]))
            except ValueError:
                raise APIError('测验包含不支持的题型，未提交') from None
            # Only known metadata is removable; <i>/<em> can contain the question.
            title_node = title_nodes[0]
            for metadata in title_node.xpath(".//span[contains(concat(' ',normalize-space(@class),' '),' quesType ')]"):
                metadata.drop_tree()
            value = re.sub(r'^\s*\d+\s*[.、．]\s*', '', title_node.text_content()).strip()
            value = re.sub(r'^\s*[（(](?:单选题|多选题|判断题)[^）)]*[）)]\s*', '', value)
            answers = {}
            for answer in node.xpath("div/ul[contains(@class,'answerList')]/li"):
                if kind == QuestionEnum.判断题:
                    keys = answer.xpath('@val-param')
                    texts = answer.xpath('p')
                else:
                    keys = answer.xpath('em/@id-param')
                    texts = answer.xpath('.//cc')
                if not keys or not texts:
                    raise APIError('测验选项结构无法识别，未提交')
                content = ''.join(text.text_content() for text in texts).strip()
                if not content:
                    raise APIError('测验含图片或无法读取的选项，未提交')
                answers[keys[0].strip()] = content
            if not value or not answers:
                raise APIError('题干或选项为空，未提交')
            self.questions.append(QuestionModel(match.group(1), value, kind, answers, ''))
        if not self.questions or (question_count is not None and len(self.questions) != question_count):
            raise APIError('解析题数与试卷题数不一致，未提交')
        parsed_ids = [question.question_id for question in self.questions]
        if len(set(parsed_ids)) != len(parsed_ids):
            raise APIError('题目编号重复，未提交')
        # Check all type fields in the form, including any outside recognized
        # question containers, so a changed layout cannot silently omit a task.
        type_ids = form.xpath(".//input[starts-with(@id,'answertype')]/@id")
        if len(type_ids) != len(parsed_ids) or set(type_ids) != {'answertype' + qid for qid in parsed_ids}:
            raise APIError('解析题目与表单题目清单不一致，未提交')
        if expected_ids is not None and set(expected_ids) != set(parsed_ids):
            raise APIError('解析题目与平台提交清单不一致，未提交')
        if question_count is None and expected_ids is None:
            raise APIError('测验题数已编码，但页面缺少完整题目清单，未提交')
        self.answerwqbid = ','.join(expected_ids or parsed_ids)
        return True

    def mount_searcher(self, searcher):
        self.searcher = searcher

    def run(self, context, config):
        failed = []
        for index, question in enumerate(self.questions):
            context.checkpoint()
            try:
                answered = self.searcher.answer(question, context)
            except Cancelled:
                raise
            except Exception as error:
                from ..network import error_text
                answered = False
                context.log(f'题目 {question.question_id} 获取答案失败：{error_text(error)}', 'warning')
            if not answered:
                failed.append(str(question.question_id))
                context.log(f'题目 {question.question_id} 尚无可提交的答案', 'warning')
            context.event('progress', value=index + 1, total=len(self.questions),
                          text=f'已处理 {index + 1}/{len(self.questions)} 道题')
        if failed:
            raise APIError('以下题目未匹配，整份测验未提交：' + '、'.join(failed))
        context.checkpoint()
        data = {'answerwqbid': self.answerwqbid or ','.join(str(q.question_id) for q in self.questions)}
        for question in self.questions:
            data[f'answer{question.question_id}'] = question.option
            data[f'answertype{question.question_id}'] = question.question_type.value
        response = self.session.post(API_EXAM_COMMIT, params={
            'keyboardDisplayRequiresUserAction': 1, '_classId': self.clazzid,
            'courseid': self.courseid, 'token': self.enc_work, 'workAnswerId': self.workAnswerId,
            'workid': self.workRelationId, 'cpi': self.cpi, 'jobid': self.jobid,
            'knowledgeid': self.knowledgeid, 'ua': 'app'}, data={
            'pyFlag': '', 'courseId': self.courseid, 'classId': self.clazzid, 'api': 1, 'mooc': 0,
            'workAnswerId': self.workAnswerId, 'totalQuestionNum': self.totalQuestionNum,
            'fullScore': self.fullScore, 'knowledgeid': self.knowledgeid, 'oldSchoolId': '',
            'oldWorkId': self.workid, 'jobid': self.jobid, 'workRelationId': self.workRelationId,
            'enc_work': self.enc_work, 'isphone': 'true', 'userId': self.acc.puid,
            'workTimesEnc': self.workTimesEnc, **data})
        response.raise_for_status()
        if response.json().get('status') not in (True, 1, 'true'):
            raise APIError('测验提交未获确认，请到平台核验后再重试')
        context.log('测验提交已获平台确认')
