"""UI-independent progress events and cooperative cancellation."""
import threading
import time
from contextlib import contextmanager

_current = threading.local()


def current_context():
    return getattr(_current, 'context', None)


class Cancelled(Exception):
    pass


class TaskControl:
    def __init__(self):
        self._condition = threading.Condition()
        self.cancelled = False
        self.paused = False

    def cancel(self):
        with self._condition:
            self.cancelled = True
            self._condition.notify_all()

    def set_paused(self, value):
        with self._condition:
            self.paused = value
            self._condition.notify_all()

    def checkpoint(self):
        with self._condition:
            while self.paused and not self.cancelled:
                self._condition.wait(0.2)
            if self.cancelled:
                raise Cancelled('操作已停止')

    def wait(self, seconds):
        remaining = max(0.0, seconds)
        elapsed = 0.0
        while remaining > 0:
            self.checkpoint()
            step = min(remaining, 0.2)
            start = time.monotonic()
            with self._condition:
                self._condition.wait(step)
            # Sleep/resume or a stalled process must not credit hours of playback.
            delta = min(step, max(0.0, time.monotonic() - start))
            elapsed += delta
            remaining -= delta
        self.checkpoint()
        return elapsed


class TaskContext:
    def __init__(self, emit=None, control=None):
        self.emit = emit or (lambda event: None)
        self.control = control or TaskControl()

    def event(self, kind, **data):
        self.emit({'kind': kind, **data})

    def log(self, text, level='info'):
        self.event('log', text=text, level=level)

    def checkpoint(self):
        self.control.checkpoint()

    def wait(self, seconds):
        return self.control.wait(seconds)

    @contextmanager
    def bound(self):
        previous = current_context()
        _current.context = self
        try:
            yield self
        finally:
            _current.context = previous

    def call(self, function, *args, **kwargs):
        """Detach cancelled I/O, discarding its result; never replay the request."""
        self.checkpoint()
        done, lock = threading.Event(), threading.Lock()
        outcome = {'abandoned': False}

        def dispose(value):
            close = getattr(value, 'close', None)
            if callable(close):
                close()

        def invoke():
            try:
                self.checkpoint()
                value = function(*args, **kwargs)
                with lock:
                    if outcome['abandoned']:
                        dispose(value)
                    else:
                        outcome['value'] = value
            except BaseException as error:
                with lock:
                    outcome['error'] = error
            finally:
                done.set()

        threading.Thread(target=invoke, daemon=True, name='CxKitty network').start()
        try:
            while not done.wait(0.05):
                self.checkpoint()
            self.checkpoint()
        except BaseException:
            with lock:
                outcome['abandoned'] = True
                dispose(outcome.pop('value', None))
            raise
        if 'error' in outcome:
            raise outcome['error']
        return outcome.get('value')
