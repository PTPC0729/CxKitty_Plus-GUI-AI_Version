"""Exercise automatic GUI navigation and resumption using an offline platform."""
import copy
import time
from types import SimpleNamespace

from cxapi.chapters import ClassChapters
from cxapi.jobs.video import ChapterVideo
from cxapi.schema import AccountInfo
from gui import MainWindow
from runtime import Cancelled, TaskContext
from settings import DEFAULTS


def run_workflow_check(directory, app):
    account = AccountInfo(123, '自检账号', '未知', '', '离线自检', '')
    counts = [{2: 2, 3: 2}, {2: 2, 3: 0}]
    executed = []

    class Session:
        def post(self, url, **kwargs):
            data = {'1': {'finishcount': 0, 'totalcount': 0, 'unfinishcount': 0}}
            data.update({str(k): {'finishcount': v, 'totalcount': 0, 'unfinishcount': 2 - v}
                         for k, v in counts[self.index].items()})
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

        def close(self):
            pass

    class Classes:
        classes = [SimpleNamespace(name='已完成课程', teacher_name='离线示例', state=0),
                   SimpleNamespace(name='待处理课程', teacher_name='离线示例', state=0)]

        def fetch_chapters_by_index(self, index):
            session = Session()
            session.index = index
            chapters = ClassChapters(session, account, index + 1, 2, 3, [
                {'id': 1, 'label': '1', 'name': '目录汇总', 'layer': 1, 'jobcount': 0},
                {'id': 2, 'label': '1.1', 'name': '已完成的小节', 'layer': 2, 'jobcount': 2},
                {'id': 3, 'label': '1.2', 'name': '继续处理的小节', 'layer': 2, 'jobcount': 2}])
            def points(position, enabled=None):
                chapter = chapters.chapters[position]
                assert index == 1 and chapter.chapter_id == 3
                def finish(context, config):
                    counts[index][chapter.chapter_id] += 1
                    executed.append(chapter.chapter_id)
                return [SimpleNamespace(kind='document', title='离线任务', pre_fetch=lambda: True,
                                        fetch=lambda: True, run=finish) for _ in range(2)]
            chapters.fetch_points_by_index = points
            return chapters

    config = copy.deepcopy(DEFAULTS)
    config['exam']['enable'] = False
    config['document']['wait'] = 0
    window = MainWindow(config)
    def wait_idle():
        deadline = time.monotonic() + 10
        while window.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert window.worker is None, 'GUI workflow did not finish'

    try:
        window.logged_in(SimpleNamespace(acc=account, session=Session(), fetch_classes=Classes))
        wait_idle()
        assert window.course_combo.count() == 2 and window.course_combo.currentIndex() == 1
        assert window.chapter_items[3].text(1) == '0/2'
        assert window.tree.topLevelItemCount() == 1
        window.tree.setCurrentItem(window.chapter_items[3])
        window.use_selected_start()
        assert window.start_chapter.currentData() == 3
        window.grab().save(str(directory / 'workflow.png'))
        window.start_course()
        wait_idle()
        assert window.last_result['verified_complete'] and executed == [3, 3]
        assert window.chapter_items[3].text(1) == '2/2'
        window.close()

        def video():
            task = ChapterVideo(None, account, 1, 2, 3, 0, 'offline-object', 4, 0)
            task.jobid, task.duration = 'offline-job', 100
            return task
        task = video()
        task.platform_offset_ms = 37000
        task._report = lambda played: {'isPassed': False}
        context = TaskContext()
        def stop(seconds):
            raise Cancelled()
        context.wait = stop
        try:
            task.run(context, {'speed': 1, 'report_rate': 58})
        except Cancelled:
            pass
        resumed, positions = video(), []
        resumed.platform_offset_ms = 37000
        def report(played):
            positions.append(played)
            return {'isPassed': True}
        resumed._report = report
        resumed.run(TaskContext(), {'speed': 1, 'report_rate': 58})
        assert positions == [37]
        return {'automatic_course_and_chapter_loading': True, 'first_pending_course_selected': True,
                'manual_start_respected': True, 'zero_total_reconciled': True,
                'directory_summary': True, 'platform_video_resume_after_restart': True}
    finally:
        if window.worker:
            window.stop()
            wait_idle()
        window.close()
        app.processEvents()
