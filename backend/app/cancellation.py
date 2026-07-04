from __future__ import annotations

import threading


class TaskCancelled(RuntimeError):
    pass


_cancelled: set[str] = set()
_lock = threading.Lock()


def request_cancel(task_id: str) -> None:
    with _lock:
        _cancelled.add(task_id)


def clear_cancel(task_id: str) -> None:
    with _lock:
        _cancelled.discard(task_id)


def is_cancelled(task_id: str) -> bool:
    with _lock:
        return task_id in _cancelled


def raise_if_cancelled(task_id: str) -> None:
    if is_cancelled(task_id):
        raise TaskCancelled("Task cancelled by user.")
