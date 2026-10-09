"""Shared sequential course runner for the desktop and terminal interfaces."""
from datetime import datetime
from uuid import uuid4

from cxapi.network import AuthenticationError, error_text
from runtime import Cancelled
from searcher import create_searcher
from settings import ROOT, save_json


def chapter_snapshot(chapters):
    items = []
    for c in chapters.chapters:
        total, finished = c.point_total, c.point_finish
        directory = getattr(c, 'is_directory', False)
        unfinished = getattr(c, 'point_unfinished', -1)
        if directory:
            children = [v for v in chapters.chapters if v.label.startswith(c.label + '.')
                        and not getattr(v, 'is_directory', False)]
            total = sum(v.point_total for v in children) if all(v.point_total >= 0 for v in children) else -1
            finished = sum(v.point_finish for v in children)
            state = '目录 · 已完成' if total > 0 and finished >= total else '目录'
        elif total > 0 and finished >= total and unfinished <= 0:
            state = '已完成'
        elif getattr(c, 'runtime_state', ''):
            state = c.runtime_state
        elif total == 0 and c.jobs == 0:
            state = '无任务点'
        else:
            state = '待核验' if total < 0 else '待处理'
        items.append({'id': c.chapter_id, 'label': c.label, 'name': c.name,
            'total': total, 'finished': finished, 'jobs': c.jobs,
            'layer': getattr(c, 'layer', 1), 'parent_id': getattr(c, 'parent_id', None),
            'is_directory': directory, 'state': state,
            'unsupported': list(getattr(c, 'unsupported', [])),
            'pending': not directory and (c.point_total < 0 or c.point_finish < c.point_total
                       or unfinished > 0 or (c.point_total == 0 and c.jobs > 0))})
    return items


def load_course_chapters(classes, index, context):
    context.checkpoint()
    chapters = classes.fetch_chapters_by_index(index)
    context.checkpoint()
    try:
        chapters.fetch_point_status()
    except AuthenticationError:
        raise
    except Exception as error:
        if isinstance(error, Cancelled):
            raise
        context.log('章节已加载，平台状态暂不可用：' + error_text(error), 'warning')
    return chapters


def first_pending_course(classes, context, start=0, excluded_indices=()):
    """Inspect courses in display order, without starting any study tasks."""
    first, uncertain = None, None
    for index in range(start, len(classes.classes)):
        context.checkpoint()
        course = classes.classes[index]
        if course.state or index in excluded_indices:
            continue
        context.event('course_scan', index=index, text=f'正在核验课程：{course.name}')
        try:
            chapters = load_course_chapters(classes, index, context)
        except (Cancelled, AuthenticationError):
            raise
        except Exception as error:
            context.log(f'{course.name}：' + error_text(error), 'warning')
            if uncertain is None:
                uncertain = (index, None)
            continue
        if first is None:
            first = (index, chapters)
        snapshot = chapter_snapshot(chapters)
        pending = not snapshot or any(c['pending'] for c in snapshot)
        context.event('course_status', index=index, complete=not pending)
        if pending:
            return index, chapters, False
    if uncertain is not None:
        return *uncertain, False
    return (*(first or (None, None)), True)


def run_course(chapters, config, context, only_chapters=None, report_dir=None, start_chapter=None):
    with context.bound():
        return _run_course(chapters, config, context, only_chapters, report_dir, start_chapter)


def _run_course(chapters, config, context, only_chapters, report_dir, start_chapter):
    enabled = {kind for kind in ('video', 'exam', 'document') if config[kind]['enable']}
    records, searcher, search_error = [], None, None
    cancelled, blocked = False, False
    start_index = 0
    if start_chapter is not None:
        matches = [i for i, c in enumerate(chapters.chapters) if str(c.chapter_id) == str(start_chapter)]
        if not matches:
            raise ValueError('所选起始章节已不存在，请重新加载课程')
        start_index = matches[0]
    scope = {c.chapter_id for c in chapters.chapters[start_index:]
             if only_chapters is None or c.chapter_id in only_chapters}
    current = None

    def record(chapter, title, status, detail):
        item = {'chapter_id': chapter.chapter_id, 'chapter': chapter.label + ' ' + chapter.name,
                'title': title, 'status': status, 'detail': detail}
        records.append(item)
        context.event('result', **item)
        context.log(f'{title}：{detail}', 'error' if status == 'failed' else 'info')

    def refresh():
        context.checkpoint()
        try:
            chapters.fetch_point_status()
        except (AuthenticationError, Cancelled):
            raise
        except Exception as error:
            for chapter in chapters.chapters:
                chapter.point_total = -1
                chapter.point_unfinished = -1
            context.log('无法核验平台状态：' + error_text(error), 'warning')
        context.event('chapters', items=chapter_snapshot(chapters))

    try:
        if not enabled:
            raise ValueError('请至少启用一种任务类型')
        if 'exam' in enabled:
            try:
                searcher = create_searcher(config['searcher'])
            except Exception as error:
                search_error = error_text(error)
                context.log('测验暂不可执行：' + search_error, 'warning')
        refresh()
        for index, chapter in enumerate(chapters.chapters):
            context.checkpoint()
            if chapter.chapter_id not in scope or getattr(chapter, 'is_directory', False):
                continue
            current = chapter
            chapter.runtime_state = ''
            context.event('chapter', id=chapter.chapter_id, text=chapter.label + ' ' + chapter.name)
            if chapters.is_finished(index):
                record(chapter, chapter.name, 'skipped', '平台显示本章已完成')
                continue
            chapter.runtime_state = '处理中'
            context.event('chapters', items=chapter_snapshot(chapters))
            try:
                tasks = chapters.fetch_points_by_index(index, enabled=enabled)
                for status, detail in chapters.point_issues:
                    record(chapter, chapter.name, status, detail)
                if not tasks and chapter.jobs > 0 and not chapters.point_issues:
                    record(chapter, chapter.name, 'failed', '本章有任务点，但平台未返回可执行内容，请刷新或在平台检查开放状态')
            except Cancelled:
                raise
            except AuthenticationError as error:
                record(chapter, chapter.name, 'failed', error_text(error))
                raise
            except Exception as error:
                chapter.runtime_state = '失败 · 可重试'
                record(chapter, chapter.name, 'failed', error_text(error))
                context.event('chapters', items=chapter_snapshot(chapters))
                continue
            for task in tasks:
                context.checkpoint()
                if task.kind not in enabled:
                    continue
                context.event('task', text=task.title)
                try:
                    if not task.pre_fetch():
                        record(chapter, task.title, 'skipped', task.skip_reason)
                        continue
                    context.checkpoint()
                    if not task.fetch():
                        record(chapter, task.title, 'skipped', task.skip_reason)
                        continue
                    context.event('task', text=task.title)
                    if task.kind == 'exam':
                        if search_error:
                            raise ValueError(search_error)
                        task.mount_searcher(searcher)
                    task.run(context, config[task.kind])
                    record(chapter, task.title, 'success', '平台已接受任务记录')
                except Cancelled:
                    raise
                except AuthenticationError as error:
                    record(chapter, task.title, 'failed', error_text(error))
                    raise
                except Exception as error:
                    record(chapter, task.title, 'failed', error_text(error))
                finally:
                    if not context.control.cancelled:
                        refresh()
                wait = config[task.kind]['wait']
                if wait:
                    context.event('progress', value=0, total=0, text=f'任务间隔，等待 {wait} 秒')
                    context.wait(wait)
            statuses = {r['status'] for r in records if r['chapter_id'] == chapter.chapter_id}
            chapter.runtime_state = ('失败 · 可重试' if 'failed' in statuses else
                                     '需在平台处理' if 'manual' in statuses and chapter.jobs > 0 else
                                     '等待平台确认' if 'success' in statuses else '')
            context.event('chapters', items=chapter_snapshot(chapters))
            current = None
        refresh()
    except Cancelled:
        cancelled = True
        if current is not None:
            current.runtime_state = '已停止 · 可继续'
        context.log('已停止；再次运行会重新核验平台完成状态')
    except AuthenticationError as error:
        blocked = True
        if current is not None:
            current.runtime_state = '登录已失效'
        context.log(error_text(error), 'error')
    finally:
        if searcher:
            searcher.close()

    snapshot = chapter_snapshot(chapters)
    context.event('chapters', items=snapshot)
    pending = [c['id'] for c in snapshot if c['pending']]
    retry = {c for c in pending if c in scope} | {r['chapter_id'] for r in records if r['status'] == 'failed'}
    counts = {key: sum(item['status'] == key for item in records) for key in ('success', 'failed', 'skipped', 'manual')}
    verified = bool(snapshot) and not pending and not cancelled and not blocked and counts['failed'] == 0
    scope_complete = not (set(pending) & scope) and not cancelled and not blocked and counts['failed'] == 0
    status = '已停止' if cancelled else '已确认登录失效，需要重新登录' if blocked else '平台已确认所有章节任务完成' if verified else '本次处理结束，仍有待核验或未完成任务'
    if scope_complete and not verified:
        status = '所选范围已完成，范围外仍有未完成章节'
    result = {'status': status, 'counts': counts, 'records': records, 'chapters': snapshot,
              'pending_chapters': pending, 'cancelled': cancelled, 'blocked': blocked,
              'retry_chapters': [c.chapter_id for c in chapters.chapters if c.chapter_id in retry],
              'start_chapter': start_chapter, 'scope_chapters': [c.chapter_id for c in chapters.chapters if c.chapter_id in scope],
              'verified_complete': verified}
    result['scope_complete'] = scope_complete
    try:
        path = (report_dir or ROOT / 'logs') / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:6] + '.json')
        save_json(path, result)
        result['report_path'] = str(path)
    except OSError:
        context.log('运行记录无法保存，请使用界面导出结果', 'warning')
    context.event('summary', **result)
    return result
