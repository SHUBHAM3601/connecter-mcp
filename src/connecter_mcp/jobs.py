"""Background jobs inside the MCP server process, for work that outlasts one tool call."""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid

from .config import HOME_DIR

LOG = HOME_DIR / "jobs.jsonl"


class Job:
    def __init__(self, name: str, details: dict | None = None):
        self.id = uuid.uuid4().hex[:8]
        self.name = name
        self.details = details or {}
        self.status = "queued"
        self.progress = 0.0
        self.message = ""
        self.result = None
        self.error = None
        self.started = time.strftime("%Y-%m-%d %H:%M:%S")
        self.finished = None
        self._cancel = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def update(self, progress: float | None = None, message: str | None = None) -> None:
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))
        if message is not None:
            self.message = message

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "status": self.status, "progress": round(self.progress, 3),
                "message": self.message, "started": self.started, "finished": self.finished,
                "details": self.details, "error": self.error, "result": self.result}


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def submit(name: str, fn, *args, details: dict | None = None, **kwargs) -> Job:
    """Run fn(job, *args, **kwargs) in a thread. fn may call job.update() and should stop when job.cancelled."""
    job = Job(name, details)
    with _lock:
        _jobs[job.id] = job

    def run():
        job.status = "running"
        try:
            job.result = fn(job, *args, **kwargs)
            job.status = "cancelled" if job.cancelled else "done"
            job.progress = 1.0 if job.status == "done" else job.progress
        except Exception as e:
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
            job.details["traceback"] = traceback.format_exc()[-1500:]
        job.finished = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            HOME_DIR.mkdir(parents=True, exist_ok=True)
            with LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({k: v for k, v in job.to_dict().items() if k != "result"}, default=str) + "\n")
        except OSError:
            pass

    threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def all_jobs() -> list[Job]:
    return sorted(_jobs.values(), key=lambda j: j.started, reverse=True)


def cancel(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if job and job.status in ("queued", "running"):
        job._cancel.set()
        return True
    return False


def history(limit: int = 20) -> list[dict]:
    if not LOG.exists():
        return []
    rows = [json.loads(line) for line in LOG.read_text("utf-8").splitlines() if line.strip()]
    return rows[-limit:][::-1]
