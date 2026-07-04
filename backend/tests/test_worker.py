from __future__ import annotations

import queue
import threading
from itertools import count

from backend.app import database, worker


def test_worker_prioritizes_tasks_with_more_completed_stages(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "worker-priority.sqlite")
    database.init_db()
    low_progress = database.create_task("https://www.youtube.com/watch?v=lowworker00")
    high_progress = database.create_task("https://www.youtube.com/watch?v=highworker0")
    database.update_stage(high_progress, "download", status="succeeded", progress=100)

    monkeypatch.setattr(
        worker,
        "_queues",
        {resource: queue.PriorityQueue() for resource in worker.RESOURCE_TYPES},
    )
    monkeypatch.setattr(worker, "_threads", {resource: None for resource in worker.RESOURCE_TYPES})
    monkeypatch.setattr(worker, "_sequence", count())
    worker.enqueue(low_progress)
    worker.enqueue(high_progress)

    _, _, first_task_id = worker._queues["compute"].get_nowait()

    assert first_task_id == high_progress


def test_worker_picks_up_pending_and_new_tasks(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "worker.sqlite")
    database.init_db()
    pre_queued = [
        database.create_task(f"https://www.youtube.com/watch?v=v{i:011d}") for i in range(2)
    ]

    executed: list[str] = []
    target = len(pre_queued) + 1
    done = threading.Event()

    def runner(task_id: str) -> None:
        executed.append(task_id)
        database.update_task(task_id, status="succeeded")
        if len(executed) == target:
            done.set()

    monkeypatch.setattr(
        worker,
        "_queues",
        {resource: queue.PriorityQueue() for resource in worker.RESOURCE_TYPES},
    )
    monkeypatch.setattr(worker, "_threads", {resource: None for resource in worker.RESOURCE_TYPES})
    monkeypatch.setattr(worker, "_current_task_ids", {resource: None for resource in worker.RESOURCE_TYPES})
    monkeypatch.setattr(worker, "_sequence", count())
    worker.start(runner)
    late_task = database.create_task("https://www.youtube.com/watch?v=lateworker0")
    worker.enqueue(late_task)
    expected = [*pre_queued, late_task]

    assert done.wait(timeout=2.0)
    assert executed[:3] == expected
