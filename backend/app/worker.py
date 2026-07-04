"""Stage scheduler with one IO lane and one compute lane."""

from __future__ import annotations

import queue
import threading
from itertools import count
from typing import Callable

from . import database
from .stages import STAGE_RESOURCES


RESOURCE_TYPES = ("io", "compute")

_queues: dict[str, "queue.PriorityQueue[tuple[int, int, str]]"] = {
    resource: queue.PriorityQueue() for resource in RESOURCE_TYPES
}
_threads: dict[str, threading.Thread | None] = {resource: None for resource in RESOURCE_TYPES}
_lock = threading.Lock()
_resume_event = threading.Event()
_resume_event.set()
_current_task_ids: dict[str, str | None] = {resource: None for resource in RESOURCE_TYPES}
_sequence = count()


def _next_stage_name(task: dict) -> str | None:
    for stage in task.get("stages") or []:
        if stage.get("status") != "succeeded":
            return str(stage.get("name") or "")
    return None


def _resource_for_task(task_id: str) -> str | None:
    task = database.get_task(task_id)
    if not task or task["status"] != "queued":
        return None
    stage_name = _next_stage_name(task)
    if not stage_name:
        return None
    return STAGE_RESOURCES.get(stage_name, "compute")


def _task_priority(task_id: str) -> int:
    task = database.get_task(task_id)
    if not task:
        return 0
    succeeded = sum(1 for stage in task.get("stages") or [] if stage.get("status") == "succeeded")
    return -succeeded


def enqueue(task_id: str) -> None:
    resource = _resource_for_task(task_id) or "compute"
    _queues[resource].put((_task_priority(task_id), next(_sequence), task_id))


def _loop(resource: str, runner: Callable[[str], None]) -> None:
    while True:
        _resume_event.wait()
        resource_queue = _queues[resource]
        _, _, task_id = resource_queue.get()
        try:
            _resume_event.wait()
            actual_resource = _resource_for_task(task_id)
            if actual_resource is None:
                continue
            if actual_resource != resource:
                enqueue(task_id)
                continue
            _current_task_ids[resource] = task_id
            runner(task_id)
            task = database.get_task(task_id)
            if task and task["status"] == "queued":
                enqueue(task_id)
        finally:
            _current_task_ids[resource] = None
            resource_queue.task_done()


def start(runner: Callable[[str], None]) -> None:
    started_any = False
    with _lock:
        _resume_event.set()
        for resource in RESOURCE_TYPES:
            thread = _threads.get(resource)
            if thread is not None and thread.is_alive():
                continue
            _threads[resource] = threading.Thread(
                target=_loop,
                args=(resource, runner),
                daemon=True,
                name=f"youdub-{resource}-worker",
            )
            _threads[resource].start()
            started_any = True
    if started_any:
        pending = [t for t in database.list_tasks() if t["status"] == "queued"]
        for task in reversed(pending):
            enqueue(task["id"])


def pause() -> None:
    _resume_event.clear()


def resume() -> None:
    _resume_event.set()


def status() -> dict:
    current_task_id = next((task_id for task_id in _current_task_ids.values() if task_id), None)
    return {
        "running": _resume_event.is_set(),
        "thread_alive": all(bool(thread and thread.is_alive()) for thread in _threads.values()),
        "queue_size": sum(resource_queue.qsize() for resource_queue in _queues.values()),
        "current_task_id": current_task_id,
        "current_task_ids": dict(_current_task_ids),
        "queues": {resource: resource_queue.qsize() for resource, resource_queue in _queues.items()},
    }
