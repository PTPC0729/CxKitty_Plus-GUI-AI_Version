"""Terminal compatibility frontend using the same runner as the GUI."""
from rich.console import Console

import dialog
from cxapi.api import ChaoXingAPI
from cxapi.network import error_text
from engine import run_course
from runtime import TaskContext
from settings import load_config
from utils import ck2dict, sessions_load


def run_cli():
    console = Console()
    config = load_config()
    api = ChaoXingAPI()
    try:
        dialog.logo(console)
        sessions = sessions_load(config['sessionPath'], lambda text: console.print(text, markup=False))
        if sessions and config['multiSession']:
            dialog.select_session(console, sessions, api)
        elif sessions:
            api.ck_load(ck2dict(sessions[0].ck))
            if not api.accinfo():
                dialog.login(console, api)
        else:
            dialog.login(console, api)
        dialog.accinfo(console, api)
        classes = api.fetch_classes()
        if not classes.classes:
            console.print('当前账号没有可用课程')
            return 0
        index = int(dialog.select_class(console, classes))
        chapters = classes.fetch_chapters_by_index(index)
        def emit(event):
            if event['kind'] in ('log', 'task', 'chapter'):
                console.print(event['text'], markup=False)
            elif event['kind'] == 'summary':
                console.print(event['status'], markup=False)
                console.print(str(event['counts']), markup=False)
        result = run_course(chapters, config, TaskContext(emit))
        return 0 if result['verified_complete'] else 2
    except KeyboardInterrupt:
        console.print('已停止')
        return 130
    except Exception as error:
        console.print(error_text(error), markup=False)
        return 1
    finally:
        api.session.close()
