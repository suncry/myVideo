"""Deliver worker results to a stable, main-thread QObject.

A short-lived QRunnable must not own QObject receivers for queued UI callbacks.
The runner belongs to the window/dialog and retains callbacks until delivery.
"""
from __future__ import annotations
import itertools
import traceback
from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Qt, Signal, Slot

class _Worker(QRunnable):
    def __init__(self, runner, token, operation):
        super().__init__()
        self.runner, self.token, self.operation = runner, token, operation

    def run(self):
        try:
            value, ok = self.operation(), True
        except Exception as exc:
            value, ok = str(exc), False
        try:
            self.runner.completed.emit(self.token, ok, value)
        except RuntimeError:
            # Closing the owning window removes the receiver; no UI work is attempted.
            pass

class TaskRunner(QObject):
    completed = Signal(int, bool, object)

    def __init__(self, parent=None, pool=None):
        super().__init__(parent)
        self.pool = pool or QThreadPool.globalInstance()
        self._tokens = itertools.count(1)
        self._callbacks = {}
        self.completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @property
    def pending_count(self):
        return len(self._callbacks)

    def start(self, operation, on_result=None, on_error=None):
        if QThread.currentThread() != self.thread():
            raise RuntimeError('后台任务必须从界面线程提交')
        token = next(self._tokens)
        self._callbacks[token] = (on_result, on_error)
        self.pool.start(_Worker(self, token, operation))
        return token

    @Slot(int, bool, object)
    def _deliver(self, token, ok, value):
        callbacks = self._callbacks.pop(token, None)
        if callbacks is None:
            return
        callback = callbacks[0 if ok else 1]
        if callback:
            try:
                callback(value)
            except Exception:
                traceback.print_exc()
