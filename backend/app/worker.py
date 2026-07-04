"""Single-thread FIFO worker that runs queued tasks one at a time."""

from __future__ import annotations

import queue
import threading
from typing import Callable

from . import database


_queue: "queue.Queue[str]" = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()
_resume_event = threading.Event()
_resume_event.set()
_current_task_id: str | None = None


def enqueue(task_id: str) -> None:
    _queue.put(task_id)


def _loop(runner: Callable[[str], None]) -> None:
    global _current_task_id
    while True:
        _resume_event.wait()
        task_id = _queue.get()
        try:
            _resume_event.wait()
            task = database.get_task(task_id)
            if task and task["status"] == "queued":
                _current_task_id = task_id
                runner(task_id)
        finally:
            _current_task_id = None
            _queue.task_done()


def start(runner: Callable[[str], None]) -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            _resume_event.set()
            return
        _resume_event.set()
        _thread = threading.Thread(target=_loop, args=(runner,), daemon=True)
        _thread.start()
    pending = [t for t in database.list_tasks() if t["status"] == "queued"]
    for task in reversed(pending):
        _queue.put(task["id"])


def pause() -> None:
    _resume_event.clear()


def resume() -> None:
    _resume_event.set()


def status() -> dict:
    return {
        "running": _resume_event.is_set(),
        "thread_alive": bool(_thread and _thread.is_alive()),
        "queue_size": _queue.qsize(),
        "current_task_id": _current_task_id,
    }
