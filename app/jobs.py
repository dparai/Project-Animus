"""Persistent reflection jobs and the in-app scheduler.

Journal safety: logs and `last_error` hold only kinds, period keys, statuses, short fixed
phrases and exception class names. Never entry text, prompts or model output.
"""
import asyncio
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import chat_memory, config, periods, reflect, tz
from app.db import connect
from app.llm import LLMError

RETRY_DELAY_SECONDS = 60
# Retryable failures never go terminal early: 60 s retries for the first attempts, then hourly,
# and only after MAX_ATTEMPTS attempts (about a day of outage) does the job become `failed`.
FAST_RETRY_ATTEMPTS = 4
SLOW_RETRY_DELAY_SECONDS = 3600
MAX_ATTEMPTS = 24
NO_KEY_MESSAGE = "no API key configured"
NO_KEY_DELAY_SECONDS = 3600
STARTUP_DELAY_SECONDS = 5
KINDS = ("weekly", "monthly")
_FMT = "%Y-%m-%dT%H:%M:%SZ"

log = logging.getLogger("animus.jobs")
_run_lock = threading.Lock()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_FMT)


def _period_for(kind: str, key: str) -> periods.Period:
    if kind == "weekly":
        year, _, week = key.partition("-W")
        return periods.week_of(date.fromisocalendar(int(year), int(week), 1))
    if kind == "monthly":
        year, _, month = key.partition("-")
        return periods._month_of(date(int(year), int(month), 1))
    raise ValueError(f"unknown kind: {kind!r}")


def _insert(conn, kind: str, period_key: str, now: datetime) -> int | None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO reflection_jobs(kind, period_key, status, created_at, queued_at)"
        " VALUES (?, ?, 'pending', ?, ?)",
        (kind, period_key, _stamp(now), _stamp(now)),
    )
    return cur.lastrowid if cur.rowcount == 1 else None


def _requeue(conn, job_id: int, now: datetime) -> None:
    conn.execute(
        "UPDATE reflection_jobs SET status = 'pending', attempts = 0, next_attempt_at = NULL,"
        " last_error = NULL, queued_at = ? WHERE id = ?",
        (_stamp(now), job_id),
    )


def ensure_due_jobs(conn: sqlite3.Connection, now: datetime) -> list[int]:
    """Create a pending job for the latest completed week and month if due and missing."""
    zone = tz.get_timezone(conn)
    candidates = (
        ("weekly", periods.last_completed_week(now, zone)),
        ("monthly", periods.last_completed_month(now, zone)),
    )
    created = []
    with conn:
        for kind, period in candidates:
            if not periods.is_due(kind, period, now, zone):
                continue
            job_id = _insert(conn, kind, period.key, now)
            if job_id is None:
                # A job queued before the period closed (Generate now mid-period) covered only
                # part of it: requeue once so the full-period reflection is still generated.
                row = conn.execute(
                    "SELECT id, status, queued_at FROM reflection_jobs WHERE kind = ? AND period_key = ?",
                    (kind, period.key),
                ).fetchone()
                due = _stamp(periods.due_instant(kind, period, zone))
                if row["status"] in ("done", "skipped", "failed") and row["queued_at"] < due:
                    _requeue(conn, row["id"], now)
                    job_id = row["id"]
            if job_id is not None:
                created.append(job_id)
                log.info("job queued kind=%s period=%s", kind, period.key)
    return created


def recover_running(conn: sqlite3.Connection) -> int:
    """After a crash mid-call: running jobs go back to pending."""
    with conn:
        cur = conn.execute("UPDATE reflection_jobs SET status = 'pending' WHERE status = 'running'")
    return cur.rowcount


def request_now(conn: sqlite3.Connection, kind: str, now: datetime) -> int:
    """Create or reset the job for the most relevant period; the caller runs it."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    zone = tz.get_timezone(conn)
    today = now.astimezone(ZoneInfo(zone)).date()
    if kind == "weekly":
        period = periods.week_of(today)
        if not periods.entries_for(conn, period):
            period = periods.last_completed_week(now, zone)
    else:
        period = periods.last_completed_month(now, zone)
        if not reflect._weeks_in_month(conn, period):
            period = periods._month_of(today)
    with conn:
        _insert(conn, kind, period.key, now)
        row = conn.execute(
            "SELECT id, status FROM reflection_jobs WHERE kind = ? AND period_key = ?",
            (kind, period.key),
        ).fetchone()
        if row["status"] != "running":  # a running job is already being generated
            _requeue(conn, row["id"], now)
    return row["id"]


def _set(conn, job_id: int, status: str, *, attempts=None, next_attempt_at=None,
         last_error=None, reflection_id=None) -> None:
    with conn:
        conn.execute(
            "UPDATE reflection_jobs SET status = ?, attempts = COALESCE(?, attempts),"
            " next_attempt_at = ?, last_error = ?, reflection_id = COALESCE(?, reflection_id)"
            " WHERE id = ? AND status = 'running'",
            (status, attempts, next_attempt_at, last_error, reflection_id, job_id),
        )


def _claim(conn, job_id: int) -> bool:
    # Committed before any network call so no transaction or lock is held during the LLM call.
    with conn:
        cur = conn.execute(
            "UPDATE reflection_jobs SET status = 'running' WHERE id = ? AND status = 'pending'",
            (job_id,),
        )
    return cur.rowcount == 1


def _process(conn, llm, job_id: int, now: datetime, retry_delay: int) -> bool:
    """Claim and run one job. False when someone else already claimed it."""
    if not _claim(conn, job_id):
        return False
    job = conn.execute("SELECT * FROM reflection_jobs WHERE id = ?", (job_id,)).fetchone()
    kind, key, attempts = job["kind"], job["period_key"], job["attempts"]
    reason = ""
    try:
        period = _period_for(kind, key)
        generate = reflect.generate_weekly if kind == "weekly" else reflect.generate_monthly
        rid = generate(conn, llm, period, now)
    except LLMError as exc:
        conn.rollback()
        reason = exc.reason or type(exc).__name__
        if exc.reason == "no_key" or str(exc) == NO_KEY_MESSAGE:
            # Not a failure: adding the key later just works. Attempts are not consumed.
            _set(conn, job_id, "pending",
                 next_attempt_at=_stamp(now + timedelta(seconds=NO_KEY_DELAY_SECONDS)),
                 last_error=NO_KEY_MESSAGE)
            status = "pending"
            reason = "no_key"
        elif exc.retryable and attempts + 1 < MAX_ATTEMPTS:
            delay = retry_delay if attempts + 1 <= FAST_RETRY_ATTEMPTS else SLOW_RETRY_DELAY_SECONDS
            _set(conn, job_id, "pending", attempts=attempts + 1,
                 next_attempt_at=_stamp(now + timedelta(seconds=delay)), last_error=reason)
            status = "pending"
        else:
            # Reason or class name only; the message text is never stored.
            _set(conn, job_id, "failed", attempts=attempts + 1, last_error=reason)
            status = "failed"
    except Exception as exc:  # anything else: class name only, never the message
        conn.rollback()
        reason = type(exc).__name__
        _set(conn, job_id, "failed", attempts=attempts + 1, last_error=reason)
        status = "failed"
    else:
        if rid is None:
            _set(conn, job_id, "skipped")
            status = "skipped"
        else:
            _set(conn, job_id, "done", reflection_id=rid)
            status = "done"
    # Ids, kinds, period keys, statuses and reason codes only; never entry text or model output.
    level = logging.INFO if status in ("done", "skipped") else logging.WARNING
    log.log(level, "job id=%s kind=%s period=%s status=%s reason=%s", job_id, kind, key, status, reason or "-")
    return True


def _weekly_in_flight(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM reflection_jobs WHERE kind = 'weekly' AND status IN ('pending', 'running') LIMIT 1"
    ).fetchone() is not None


def run_pending(conn: sqlite3.Connection, llm, now: datetime, *,
                retry_delay: int = RETRY_DELAY_SECONDS) -> list[int]:
    """Run every pending job that is due, oldest first. Returns the ids this call processed."""
    ids = [
        (r["id"], r["kind"]) for r in conn.execute(
            "SELECT id, kind FROM reflection_jobs WHERE status = 'pending'"
            " AND (next_attempt_at IS NULL OR next_attempt_at = '' OR next_attempt_at <= ?)"
            " ORDER BY created_at, id",
            (_stamp(now),),
        ).fetchall()
    ]
    ids.sort(key=lambda item: item[1] != "weekly")  # stable: weeklies first, so monthlies see their output
    processed = []
    for job_id, kind in ids:
        if kind == "monthly" and _weekly_in_flight(conn):
            continue  # its weekly input is not finished yet: leave it pending, no attempt counted
        if _process(conn, llm, job_id, now, retry_delay):
            processed.append(job_id)
    return processed


def resolve_llm(llm):
    if llm is not None:
        return llm
    from app.llm import LlmClient  # built per run so a key added to .env is picked up

    return LlmClient(config.get_llm_settings())


def run_job(db_path: str, llm, job_id: int, now: datetime | None = None) -> bool:
    """Background-task helper: run one pending job on its own connection."""
    now = now or datetime.now(timezone.utc)
    with _run_lock:
        conn = connect(db_path)
        try:
            recover_running(conn)  # the lock serialises runners, so any running row is stale
            return _process(conn, resolve_llm(llm), job_id, now, RETRY_DELAY_SECONDS)
        finally:
            conn.close()


def scheduler_tick(db_path: str, llm, now: datetime | None = None) -> list[int]:
    """One scheduler pass: create due jobs, run pending ones, then turn finished conversations
    into memory notes. Returns the reflection job ids processed."""
    now = now or datetime.now(timezone.utc)
    processed = _reflection_tick(db_path, llm, now)
    # After the reflection jobs and outside their lock, so "Generate now" never waits on it.
    try:
        chat_memory.run_step(db_path, llm, now)
    except Exception as exc:  # class name only; a failure here never affects reflection jobs
        log.warning("conversation memory step failed reason=%s", type(exc).__name__)
    return processed


def _reflection_tick(db_path: str, llm, now: datetime) -> list[int]:
    """Create due jobs and run pending ones. Skipped if another run is active."""
    if not _run_lock.acquire(blocking=False):
        return []
    try:
        conn = connect(db_path)
        try:
            recover_running(conn)  # the lock serialises runners, so any running row is stale
            ensure_due_jobs(conn, now)
            return run_pending(conn, resolve_llm(llm), now)
        finally:
            conn.close()
    finally:
        _run_lock.release()


async def scheduler_loop(db_path: str, llm, interval_seconds: float = 300,
                         stop_event: asyncio.Event | None = None,
                         startup_delay: float = STARTUP_DELAY_SECONDS) -> None:
    stop = stop_event or asyncio.Event()

    async def stopped_within(seconds: float) -> bool:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    if await stopped_within(startup_delay):
        return
    while True:
        try:
            await asyncio.to_thread(scheduler_tick, db_path, llm)
        except Exception as exc:
            log.warning("scheduler tick failed: %s", type(exc).__name__)
        if await stopped_within(interval_seconds):
            return
