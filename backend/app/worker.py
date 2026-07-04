"""Single-thread FIFO worker that runs queued tasks one at a time."""

from __future__ import annotations

import queue
import threading
from typing import Callable

from . import database


_queue: "queue.Queue[str]" = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()


def enqueue(task_id: str) -> None:
    _queue.put(task_id)


def _loop(runner: Callable[[str], None]) -> None:
    while True:
        task_id = _queue.get()
        task = database.get_task(task_id)
        if task and task["status"] == "queued":
            runner(task_id)
        _queue.task_done()


def start(runner: Callable[[str], None]) -> None:
    global _thread
    with _lock:
        if _thread is not None:
            return
        _thread = threading.Thread(target=_loop, args=(runner,), daemon=True)
        _thread.start()
    pending = [t for t in database.list_tasks() if t["status"] == "queued"]
    for task in reversed(pending):
        _queue.put(task["id"])
