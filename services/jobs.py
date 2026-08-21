"""
In-process registry of running import jobs, so an upload can report progress.

Why this exists at all: parsing a long statement takes minutes, and for that
whole time a synchronous upload is one request that says nothing until it ends
— which is both a blank screen for the user and, on a host that caps request
duration, a connection cut before any answer exists. Handing the work to a
background task and letting the browser ask "how far along?" fixes both, and it
is the same shape whether the parse takes two seconds or four minutes.

Deliberately in memory, not Redis or a table. One web process serves this app,
and progress is worth exactly as long as the job it describes: a restart loses
in-flight jobs, which is correct — the parse died with the process, so there is
nothing left to report on. Two things would change that and both are the same
change: running more than one worker, or wanting a job to survive a deploy.
Then this module keeps its interface and stores the same records in Redis.

Thread safety is not optional here. tick() is called from the executor thread
running the parse; every other method is called from the event loop. One lock
guards the lot — it is held for a few field assignments, hundreds of times per
parse, which is nothing against seconds per page.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

QUEUED = "queued"
PARSING = "parsing"
SAVING = "saving"
DONE = "done"
FAILED = "failed"

# How long a finished job stays readable. The browser polls every second or so,
# so this only has to outlive the gap between the last tick and the poll that
# collects the result — but a user who switches tabs mid-import should still
# find it, hence minutes rather than seconds.
RETENTION_SECONDS = 900

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _prune_locked(now: float) -> None:
    """Drop finished jobs past their retention. Called with the lock held.

    On access rather than on a timer: there is no scheduler here, and a registry
    that only grows is the one way an in-memory store like this turns into a
    leak. Running jobs are never pruned, however long they take.
    """
    stale = [
        jid for jid, j in _jobs.items()
        if j["finished_at"] and now - j["finished_at"] > RETENTION_SECONDS
    ]
    for jid in stale:
        _jobs.pop(jid, None)


def create(*, schema: str, username: str, filename: str, total_units: int,
           total_pages: int | None) -> str:
    """Register a job and return its id.

    total_units is the number of ticks the parse is expected to report, not the
    page count — the parser walks the document several times, so a 65-page file
    reports far more than 65 times. Passing it in keeps this module ignorant of
    how the parser is built.
    """
    job_id = uuid.uuid4().hex
    now = time.time()
    with _lock:
        _prune_locked(now)
        _jobs[job_id] = {
            "id": job_id,
            # Who may read it. A job id is a random 32 hex characters, but the
            # progress of another company's import is still not this caller's
            # to read, so ownership is recorded rather than assumed.
            "schema": schema,
            "username": username,
            "filename": filename,
            "state": QUEUED,
            "units_done": 0,
            "total_units": max(1, total_units),
            "total_pages": total_pages,
            # A batched import is several parses, not one long one, and a single
            # bar crawling across all of them says less than a bar per batch: the
            # user cannot tell a stretch that is nearly done from one that has
            # barely started. So progress is reported per step and restarts at
            # zero for each — see start_step. The whole-file figures above are
            # kept alongside it for anyone who wants the total.
            "step_done": 0,
            "step_units": 1,
            "step_index": 0,
            "step_total": 1,
            "step_label": "",
            # One line per finished batch, so the screen can keep saying what is
            # already done while the bar starts over on the next one.
            "steps_done": [],
            "message": "Queued",
            "result": None,
            "error": None,
            "started_at": now,
            "finished_at": None,
            # The asyncio task is parked here purely so it is not garbage
            # collected mid-parse: nothing awaits it, so this is its only
            # reference.
            "task": None,
        }
    return job_id


def attach_task(job_id: str, task) -> None:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["task"] = task


def set_state(job_id: str, state: str, message: str | None = None) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["state"] = state
        if message is not None:
            job["message"] = message


def start_step(job_id: str, *, index: int, total: int, label: str,
               units: int, message: str) -> None:
    """Begin one parse of a batched import, and reset the bar to zero.

    index is 1-based over the batches; 0 marks the one-page probe that runs
    first, which is work the user can see happening but is not a batch and must
    not be numbered as one.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(state=PARSING, step_done=0, step_units=max(1, units),
                   step_index=index, step_total=total, step_label=label,
                   message=message)


def complete_step(job_id: str, *, rows: int) -> None:
    """One batch is finished. Records the line the screen keeps showing.

    Only real batches are recorded — the probe is not one, and reporting it as
    "batch 0" would make a four-batch import look like five.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        if job["step_index"] < 1:
            return
        job["steps_done"].append({
            "index": job["step_index"],
            "total": job["step_total"],
            "label": job["step_label"],
            "rows": rows,
        })
        # Held at full until the next step starts, so the bar reads 100% at the
        # moment the batch completes instead of jumping straight back to zero.
        job["step_done"] = job["step_units"]


def tick(job_id: str) -> None:
    """One page finished. Called from the parsing thread, hundreds of times."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["units_done"] += 1
        job["step_done"] += 1


def finish(job_id: str, result: dict) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(state=DONE, result=result, message="Finished",
                   units_done=job["total_units"],
                   step_done=job["step_units"], finished_at=time.time(),
                   task=None)


def fail(job_id: str, error: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(state=FAILED, error=error, message="Failed",
                   finished_at=time.time(), task=None)


def get(job_id: str) -> dict | None:
    """A snapshot of the job, or None. Never the live record.

    The caller reads this from the event loop while the parse thread is still
    writing to it, so it gets a copy — and the percentage is computed here so
    every reader agrees on it.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        done, total = job["units_done"], job["total_units"]
        step_done, step_units = job["step_done"], job["step_units"]
        if job["state"] == DONE:
            percent = overall = 100
        else:
            # Capped below 100 while work is outstanding: the unit total is an
            # expectation, and a parse that reports more ticks than expected
            # must not show a finished bar over an unfinished import. The one
            # place 100 is allowed early is a step the parse has actually
            # returned from, which complete_step sets deliberately.
            cap = 100 if step_done >= step_units else 99
            percent = min(cap, int(step_done * 100 / step_units))
            overall = min(99, int(done * 100 / total)) if total else 0
        return {
            "job_id": job["id"],
            "state": job["state"],
            # The bar: this batch's own progress, restarting at zero for each.
            "percent": percent,
            # The whole file, for anyone who wants it — the UI shows the batch.
            "overall_percent": overall,
            "batch_index": job["step_index"],
            "batch_total": job["step_total"],
            "batch_label": job["step_label"],
            "batches_done": list(job["steps_done"]),
            "pages_done": done,
            "total_units": total,
            "total_pages": job["total_pages"],
            "filename": job["filename"],
            "message": job["message"],
            "result": job["result"],
            "error": job["error"],
            "elapsed_ms": round(
                ((job["finished_at"] or time.time()) - job["started_at"]) * 1000
            ),
            "_schema": job["schema"],
        }
