import json
import re
import time
from typing import TypeVar

import lxml.html
import requests
from rich.layout import Layout
from rich.panel import Panel
from wcwidth import wcswidth

from . import APIError, calc_infenc
from .jobs.document import ChapterDocument
from .jobs.exam import ChapterExam
from .jobs.video import ChapterVideo
from .schema import AccountInfo, ChapterModel

TaskPointType = TypeVar('TaskPointType', ChapterExam, ChapterVideo, ChapterDocument)

API_CHAPTER_POINT = 'https://mooc1-api.chaoxing.com/job/myjobsnodesmap'          # 接口-课程章节任务点状态
API_CHAPTER_CARDS = 'https://mooc1-api.chaoxing.com/gas/knowledge'               # 接口-课程章节卡片


class ClassChapters:
    '课程章节'
    session: requests.Session
    acc: AccountInfo
    chapters: list[ChapterModel]
    # 课程参数
    courseid: int  # 课程 id
    clazzid: int  # 班级 id
    cpi: int
    
    def __init__(self, session: requests.Session, acc: AccountInfo, courseid: int, clazzid: int, cpi: int, chapter_lst: list[dict]) -> None:
        self.session = session
        self.acc = acc
        self.courseid = courseid
        self.clazzid = clazzid
        self.cpi = cpi
        
        self.chapters = [
            ChapterModel(
                chapter_id=c['id'],
                jobs=int(c.get('jobcount') or 0),
                index=int(c.get('indexorder') or c.get('indexOrder') or 0),
                name=str(c.get('name') or '未命名章节').strip(),
                label=str(c.get('label') or ''),
                layer=int(c.get('layer') or 1),
                status=c.get('status', ''),
                point_total=-1, point_finish=0, parent_id=c.get('parentnodeid')
            ) for c
            in chapter_lst
        ]
        self.chapters.sort(key=lambda x: tuple(int(v) for v in re.findall(r'\d+', x.label)) or (x.index,))
        for chapter in self.chapters:
            chapter.is_directory = chapter.jobs == 0 and any(
                (other.parent_id is not None and str(other.parent_id) == str(chapter.chapter_id))
                or (other.layer > chapter.layer and other.label.startswith(chapter.label + '.'))
                for other in self.chapters if other is not chapter)
        self.point_issues = []
    
    def is_finished(self, index: int) -> bool:
        '判断当前章节的任务点是否全部完成'
        c = self.chapters[index]
        return c.point_total > 0 and c.point_finish >= c.point_total and c.point_unfinished <= 0
    
    def render_lst2tui(self, tui_ctx: Layout, index: int, lst_length: int=16):
        '渲染章节列表到TUI'
        lines = []
        total = len(self.chapters)
        half_length = lst_length // 2
        if index - half_length < 0:
            _min = 0
            _max = min(total, lst_length)
        elif index + half_length > total:
            _min = max(0, total - lst_length)
            _max = total
        else:
            _min = index - half_length
            _max = index + half_length
        for ptr in range(_min, _max):
            chapter = self.chapters[ptr]
            # 判断是否已完成章节任务
            is_finished = self.is_finished(ptr)
            lines.append(
                ('[bold red]❱[/]' if ptr == index else ' ') + 
                ('  ' * (chapter.layer - 1)) +
                f'[bold green]{chapter.label}[/]:' +
                '[' + ('bold ' if ptr == index else '')  + ('green' if is_finished else 'white') + ']' +
                (chapter.name[:22] + '...' if wcswidth(chapter.name) > 25 else chapter.name) + '[/]' +
                f'    ...任务点{chapter.point_finish}/{chapter.point_total}'
            )
        tui_ctx.update(Panel('\n'.join(lines), title='课程列表', border_style='blue'))
    
    def fetch_point_status(self):
        '拉取章节任务点状态'
        resp = self.session.post(API_CHAPTER_POINT, data={
            'view': 'json',
            'nodes': ','.join(str(c.chapter_id) for c in self.chapters),
            'clazzid': self.clazzid,
            'time': int(time.time()*1000),
            'userid': self.acc.puid,
            'cpi': self.cpi,
            'courseid': self.courseid
        })
        resp.raise_for_status()
        json_content = resp.json()
        if not isinstance(json_content, dict):
            raise APIError('章节状态接口返回格式不正确')
        for c in self.chapters:
            point_data = json_content.get(str(c.chapter_id))
            if not isinstance(point_data, dict):
                c.point_total, c.point_finish = -1, 0
                c.point_unfinished = -1
                continue
            try:
                def count(name, default=-1):
                    value = point_data.get(name, default)
                    if isinstance(value, bool):
                        raise ValueError()
                    number = int(value)
                    if number < -1 or (isinstance(value, float) and value != number):
                        raise ValueError()
                    return number
                finished, unfinished, total = count('finishcount'), count('unfinishcount'), count('totalcount')
                if finished < 0 or (total < 0 and unfinished < 0):
                    raise ValueError()
                c.point_finish = finished
                c.point_unfinished = unfinished
                c.point_total = max(total, finished + max(0, unfinished), c.jobs)
            except (ValueError, TypeError, OverflowError):
                c.point_total, c.point_finish, c.point_unfinished = -1, 0, -1
    
    def fetch_points_by_index(self, num: int, enabled=None) -> list[TaskPointType]:
        '以课程序号拉取对应“章节”的任务节点卡片资源'
        self.point_issues = []
        self.chapters[num].unsupported = []
        kinds = {'insertvideo': 'video', 'work': 'exam', 'insertdoc': 'document'}
        params = {
            'id': self.chapters[num].chapter_id,
            'courseid': self.courseid,
            'fields': 'id,parentnodeid,indexorder,label,layer,name,begintime,createtime,lastmodifytime,status,jobUnfinishedCount,clickcount,openlock,card.fields(id,knowledgeid,title,knowledgeTitile,description,cardorder).contentcard(all)',
            'view': 'json',
            'token': '4faa8662c59590c6f43ae9fe5b002b42',
            '_time': int(time.time()*1000)
        }
        resp = self.session.get(API_CHAPTER_CARDS, params={**params, 'inf_enc': calc_infenc(params)})
        resp.raise_for_status()
        content_json = resp.json()
        if not isinstance(content_json, dict) or 'data' not in content_json:
            raise APIError('服务器没有返回章节任务数据，请检查登录状态或课程权限')
        if len(content_json['data']) == 0:
            return []
        cards = content_json['data'][0].get('card', {}).get('data', [])
        point_objs = []  # 任务点实例化列表
        for card_index, card in enumerate(cards):  # 遍历章节卡片
            if not isinstance(card, dict):
                self.point_issues.append(('failed', '章节卡片格式无效'))
                continue
            description = card.get('description')
            if not description:
                continue
            if not isinstance(description, str):
                self.point_issues.append(('failed', '章节卡片内容格式无效'))
                continue
            if not description.strip():
                continue
            try:
                inline_html = lxml.html.fromstring(description)
            except (ValueError, lxml.etree.ParserError):
                self.point_issues.append(('failed', '章节卡片内容无法解析'))
                continue
            points = inline_html.xpath("//iframe")
            for point_index, point in enumerate(points):  # 遍历任务点列表
                # 获取任务点类型 跳过不存在 Type 的任务点
                if point_type := point.xpath("@module"):
                    point_type = point_type[0]
                else:
                    continue
                kind = kinds.get(point_type)
                if kind is None:
                    label = {'insertreadV2': '阅读', 'insertvote': '投票'}.get(point_type, point_type)
                    self.chapters[num].unsupported.append(label)
                    self.point_issues.append(('manual', f'{label}组件需要在平台查看或处理'))
                    continue
                if enabled is not None and kind not in enabled:
                    self.point_issues.append(('skipped', {'video': '视频', 'exam': '测验', 'document': '文档'}[kind] + '已在设置中关闭'))
                    continue
                try:
                    json_data = json.loads(point.xpath('@data')[0])
                    required = ('workid', '_jobid') if kind == 'exam' else ('objectid',)
                    if not isinstance(json_data, dict) or any(key not in json_data for key in required):
                        raise ValueError
                except (ValueError, IndexError):
                    self.point_issues.append(('failed', '任务卡片参数不完整，无法读取该资源'))
                    continue
                # 进行分类讨论任务点类型并做 ORM
                match point_type:
                    case 'insertvideo':
                        # 视频任务点
                        point_objs.append(ChapterVideo(
                            session=self.session,
                            acc=self.acc,
                            card_index=card_index,
                            point_index=point_index,
                            courseid=self.courseid,
                            knowledgeid=self.chapters[num].chapter_id,
                            objectid=json_data['objectid'],
                            clazzid=self.clazzid,
                            cpi=self.cpi
                        ))
                    case 'work':
                        # 测验任务点
                        point_objs.append(ChapterExam(
                            session=self.session,
                            acc=self.acc,
                            card_index=card_index,
                            point_index=point_index,
                            courseid=self.courseid,
                            workid=json_data['workid'],
                            jobid=json_data['_jobid'],
                            knowledgeid=self.chapters[num].chapter_id,
                            clazzid=self.clazzid,
                            cpi=self.cpi
                        ))
                    case 'insertdoc':
                        # 文档查看任务点
                        point_objs.append(ChapterDocument(
                            session=self.session,
                            acc=self.acc,
                            card_index=card_index,
                            point_index=point_index,
                            courseid=self.courseid,
                            knowledgeid=self.chapters[num].chapter_id,
                            clazzid=self.clazzid,
                            cpi=self.cpi,
                            objectid=json_data['objectid']
                        ))
                if kind in ('video', 'document') and json_data.get('_jobid'):
                    point_objs[-1].jobid = json_data['_jobid']
        return point_objs
