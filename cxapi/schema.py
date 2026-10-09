from dataclasses import dataclass, field
from enum import Enum


@dataclass
class AccountInfo:
    '账号个人信息'
    puid: int    # 用户 puid
    name: str    # 真实姓名
    sex: str     # 性别
    phone: str   # 手机号
    school: str  # 学校
    stu_id: str  # 学号

@dataclass
class ClassModule:
    '课程数据模型'
    courseid: int
    clazzid: int
    cpi: int
    key: int
    name: str
    teacher_name: str
    state: int

@dataclass
class ChapterModel:
    '章节数据模型'
    chapter_id: int
    jobs: int
    index: int
    name: str
    label: str
    layer: int
    status: str
    point_total: int
    point_finish: int
    parent_id: int | str | None = None
    is_directory: bool = False
    point_unfinished: int = -1
    runtime_state: str = ''
    unsupported: list[str] = field(default_factory=list)

class QuestionEnum(Enum):
    '题目类型枚举'
    单选题 = 0
    多选题 = 1
    判断题 = 3

@dataclass
class QuestionModel:
    '题目数据模型'
    question_id: int | str
    value: str
    question_type: QuestionEnum
    answers: dict[str, str]
    option: str
