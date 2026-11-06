import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, jobs, periods, reflections
from app.db import connect
from app.llm import LLMError
from app.main import create_app
from tests.fake_llm import FakeLLM

# Mon 2 Nov 2026 12:00 UTC = 04:00 PST: week 2026-W44 (26 Oct - 1 Nov) and October are complete.
NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
WEEK, MONTH = "2026-W44", "2026-10"
TEXT = "Finished the first draft of the song and it sounds honest."


def add_entry(conn, local_date, text=TEXT):
    cur = conn.execute(
        "INSERT INTO entries(created_at, local_date, text) VALUES (?, ?, ?)",
        (local_date + "T12:00:00Z", local_date, text),
    )
    conn.commit()
    return cur.lastrowid


def good(entry_id):
    return {
        "wins": [{"id": "w1", "text": "You finished a draft.", "quote": "first draft of the song",
                  "entry_id": entry_id}],
        "tough_love": [], "focus": [], "focus_review": [],
    }


def job(conn, kind="weekly", key=None):
    key = key or (WEEK if kind == "weekly" else MONTH)
    return conn.execute(
        "SELECT * FROM reflection_jobs WHERE kind = ? AND period_key = ?", (kind, key)
    ).fetchone()


def reflections_count(conn):
    return conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]


@pytest.fixture
def week(conn):
    """One entry in W44 and the due jobs created."""
    eid = add_entry(conn, "2026-10-27")
    jobs.ensure_due_jobs(conn, NOW)
    return eid


def test_due_jobs_created_once(conn):
    first = jobs.ensure_due_jobs(conn, NOW)
    assert len(first) == 2
    assert jobs.ensure_due_jobs(conn, NOW) == []
    assert conn.execute("SELECT COUNT(*) FROM reflection_jobs").fetchone()[0] == 2


def test_not_due_before_deadline(conn):
    before = datetime(2026, 11, 2, 3, 0, 0, tzinfo=timezone.utc)  # Sun 19:00 PST: W44 not yet due
    created = jobs.ensure_due_jobs(conn, before)
    keys = {(r["kind"], r["period_key"]) for r in conn.execute("SELECT * FROM reflection_jobs")}
    assert len(created) == 2
    assert keys == {("weekly", "2026-W43"), ("monthly", "2026-10")}


def test_only_latest_period_catches_up(conn):
    later = NOW + timedelta(days=30)  # a month offline: older weeks and months are not created
    jobs.ensure_due_jobs(conn, later)
    rows = {(r["kind"], r["period_key"]) for r in conn.execute("SELECT * FROM reflection_jobs")}
    assert rows == {("weekly", "2026-W48"), ("monthly", "2026-11")}


def test_success_marks_done_and_links_reflection(conn, week):
    llm = FakeLLM([good(week)])
    jobs.run_pending(conn, llm, NOW)
    row = job(conn)
    assert row["status"] == "done"
    assert row["reflection_id"] == reflections.latest(conn, "weekly")["id"]
    assert row["last_error"] is None


def test_no_entries_marks_skipped_no_call(conn):
    jobs.ensure_due_jobs(conn, NOW)
    llm = FakeLLM()
    jobs.run_pending(conn, llm, NOW)
    assert job(conn)["status"] == "skipped"
    assert llm.calls == []
    assert reflections_count(conn) == 0


def test_monthly_none_marks_skipped(conn):
    jobs.ensure_due_jobs(conn, NOW)
    llm = FakeLLM()
    jobs.run_pending(conn, llm, NOW)
    assert job(conn, "monthly")["status"] == "skipped"
    assert llm.calls == []


def test_retryable_error_sets_next_attempt_and_stays_pending(conn, week):
    llm = FakeLLM([LLMError("temporary", retryable=True, reason="rate_limited")])
    jobs.run_pending(conn, llm, NOW)
    row = job(conn)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] == (NOW + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert row["last_error"] == "rate_limited"


def test_not_due_before_next_attempt(conn, week):
    llm = FakeLLM([LLMError("temporary", retryable=True), good(week)])
    jobs.run_pending(conn, llm, NOW)
    assert jobs.run_pending(conn, llm, NOW + timedelta(seconds=30)) == []
    assert len(llm.calls) == 1
    assert len(jobs.run_pending(conn, llm, NOW + timedelta(seconds=61))) >= 1
    assert job(conn)["status"] == "done"


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def force_due(conn):
    conn.execute("UPDATE reflection_jobs SET next_attempt_at = NULL")
    conn.commit()


def test_retry_schedule_60s_then_hourly(conn, week):
    llm = FakeLLM([LLMError(f"SECRET {TEXT}", retryable=True, reason="timeout") for _ in range(8)])
    for attempt in range(1, 9):
        jobs.run_pending(conn, llm, NOW)
        row = job(conn)
        assert row["status"] == "pending" and row["attempts"] == attempt
        delay = 60 if attempt <= 4 else 3600
        assert row["next_attempt_at"] == stamp(NOW + timedelta(seconds=delay)), attempt
        assert row["last_error"] == "timeout"
        assert "SECRET" not in row["last_error"] and TEXT not in row["last_error"]
        force_due(conn)


def test_24th_retryable_failure_marks_failed(conn, week):
    llm = FakeLLM([LLMError("x", retryable=True, reason="http_5xx") for _ in range(24)])
    for attempt in range(1, 25):
        jobs.run_pending(conn, llm, NOW)
        assert job(conn)["status"] == ("pending" if attempt < 24 else "failed"), attempt
        force_due(conn)
    row = job(conn)
    assert row["attempts"] == 24 and row["last_error"] == "http_5xx"
    assert jobs.MAX_ATTEMPTS == 24


def test_long_outage_job_still_pending_after_25_minutes(conn, week):
    llm = FakeLLM([LLMError("x", retryable=True, reason="timeout") for _ in range(10)])
    for i in range(10):
        jobs.run_pending(conn, llm, NOW + timedelta(minutes=i * 5), retry_delay=60)
        force_due(conn)
    assert job(conn)["status"] == "pending"
    assert job(conn)["attempts"] == 10


def test_no_key_path_unchanged_and_reason_code(conn, week):
    llm = FakeLLM([LLMError("no API key configured", retryable=False, reason="no_key")])
    jobs.run_pending(conn, llm, NOW)
    row = job(conn)
    assert (row["status"], row["attempts"]) == ("pending", 0)
    assert row["next_attempt_at"] == stamp(NOW + timedelta(seconds=3600))


def test_unexpected_error_reason_is_error_for_default_llmerror(conn, week):
    llm = FakeLLM([LLMError("whatever", retryable=False)])
    jobs.run_pending(conn, llm, NOW)
    assert job(conn)["last_error"] == "error"


def test_non_retryable_marks_failed_immediately(conn, week):
    llm = FakeLLM([LLMError("LLM API request rejected (HTTP 400)", retryable=False)])
    jobs.run_pending(conn, llm, NOW)
    row = job(conn)
    assert row["status"] == "failed"
    assert row["last_error"] == "error"  # reason only, never the message
    assert len(llm.calls) == 1


def test_unexpected_exception_class_name_only(conn, week):
    llm = FakeLLM([ValueError(f"SECRET {TEXT}")])
    jobs.run_pending(conn, llm, NOW)
    row = job(conn)
    assert row["status"] == "failed"
    assert row["last_error"] == "ValueError"


def test_restart_resets_running_to_pending_and_creates_no_duplicate_reflection(conn, week):
    conn.execute("UPDATE reflection_jobs SET status = 'running' WHERE kind = 'weekly'")
    conn.commit()
    assert jobs.recover_running(conn) == 1
    assert job(conn)["status"] == "pending"
    llm = FakeLLM([good(week)])
    jobs.run_pending(conn, llm, NOW)
    jobs.recover_running(conn)
    jobs.ensure_due_jobs(conn, NOW)
    jobs.run_pending(conn, llm, NOW)
    assert reflections_count(conn) == 1
    assert len(llm.calls) == 1


def test_rerun_after_done_does_nothing(conn, week):
    llm = FakeLLM([good(week)])
    jobs.run_pending(conn, llm, NOW)
    assert jobs.run_pending(conn, llm, NOW + timedelta(hours=1)) == []
    assert len(llm.calls) == 1
    assert reflections_count(conn) == 1


def test_request_now_creates_new_version_not_second_job(conn, week):
    llm = FakeLLM([good(week), good(week)])
    jobs.run_pending(conn, llm, NOW)
    jid = jobs.request_now(conn, "weekly", NOW)
    assert jid == job(conn)["id"]
    assert job(conn)["status"] == "pending"
    jobs.run_pending(conn, llm, NOW)
    assert conn.execute(
        "SELECT COUNT(*) FROM reflection_jobs WHERE kind='weekly' AND period_key=?", (WEEK,)
    ).fetchone()[0] == 1
    versions = [r["version"] for r in conn.execute(
        "SELECT version FROM reflections WHERE kind='weekly' AND period_key=? ORDER BY version", (WEEK,))]
    assert versions == [1, 2]


def test_request_now_weekly_prefers_current_week_with_entries_else_last_completed(conn):
    jid = jobs.request_now(conn, "weekly", NOW)
    assert conn.execute("SELECT period_key FROM reflection_jobs WHERE id=?", (jid,)).fetchone()[0] == WEEK
    add_entry(conn, "2026-11-02")  # Monday of W45, the current week
    jid2 = jobs.request_now(conn, "weekly", NOW)
    assert conn.execute("SELECT period_key FROM reflection_jobs WHERE id=?", (jid2,)).fetchone()[0] == "2026-W45"


def test_request_now_unknown_kind_valueerror(conn):
    with pytest.raises(ValueError):
        jobs.request_now(conn, "daily", NOW)


def test_claim_is_atomic_two_runners(conn, week, tmp_path):
    other = connect(str(tmp_path / "test.db"))
    seen = []

    class Racing(FakeLLM):
        def complete_json(self, system, user, *, max_tokens=1500):
            seen.append(jobs.run_pending(other, self, NOW))  # second runner during the first call
            return super().complete_json(system, user, max_tokens=max_tokens)

    llm = Racing([good(week)])
    first = jobs.run_pending(conn, llm, NOW)
    other.close()
    weekly_id = job(conn)["id"]
    assert weekly_id not in seen[0]  # the second runner could not claim the running job
    assert weekly_id in first
    assert len(llm.calls) == 1


def test_run_pending_never_holds_open_transaction_during_llm_call(conn, week):
    states = []

    class Probe(FakeLLM):
        def complete_json(self, system, user, *, max_tokens=1500):
            states.append((conn.in_transaction,
                           conn.execute("SELECT status FROM reflection_jobs WHERE kind='weekly'").fetchone()[0]))
            return super().complete_json(system, user, max_tokens=max_tokens)

    jobs.run_pending(conn, Probe([good(week)]), NOW)
    assert states == [(False, "running")]


def test_app_without_key_starts_and_job_stays_pending_with_no_key_message(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    path = str(tmp_path / "app.db")
    app = create_app(path, llm=None, start_scheduler=False)
    assert app.state.llm is None and app.state.db_path == path
    c = connect(path)
    add_entry(c, "2026-10-27")
    c.close()
    jobs.scheduler_tick(path, app.state.llm, NOW)
    c = connect(path)
    row = job(c)
    c.close()
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["last_error"] == "no API key configured"
    assert row["next_attempt_at"] == (NOW + timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_scheduler_disabled_by_env_off(tmp_path, monkeypatch):
    path = str(tmp_path / "app.db")
    for value in ("off", "0"):
        monkeypatch.setenv("ANIMUS_SCHEDULER", value)
        app = create_app(path, llm=FakeLLM())
        with TestClient(app):
            assert app.state.scheduler_task is None


def test_lifespan_recovers_running_and_stops_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMUS_SCHEDULER", "on")
    path = str(tmp_path / "app.db")
    app = create_app(path, llm=FakeLLM())
    c = connect(path)
    jobs.ensure_due_jobs(c, NOW)
    c.execute("UPDATE reflection_jobs SET status = 'running'")
    c.commit()
    c.close()
    with TestClient(app):
        task = app.state.scheduler_task
        assert task is not None
        c = connect(path)
        assert c.execute("SELECT COUNT(*) FROM reflection_jobs WHERE status='running'").fetchone()[0] == 0
        c.close()
    assert task.done()


def test_scheduler_loop_stops_on_event(tmp_path):
    path = str(tmp_path / "loop.db")
    c = connect(path)
    db.init_db(c)
    c.close()

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(
            jobs.scheduler_loop(path, FakeLLM(), 0.05, stop, startup_delay=0.01)
        )
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=3)
        return task.done()

    assert asyncio.run(go()) is True


# --- Generate now mid-period must not swallow the scheduled run ---
WED = datetime(2026, 11, 4, 20, 0, 0, tzinfo=timezone.utc)   # Wed 12:00 PST, inside W45
SUN = datetime(2026, 11, 9, 5, 0, 0, tzinfo=timezone.utc)    # Sun 21:00 PST: W45 due
DEC1 = datetime(2026, 12, 1, 14, 0, 0, tzinfo=timezone.utc)  # 1 Dec 06:00 PST: November due


def monthly_out():
    return {"summary": "A month of drafts.", "patterns": [], "focus_next": []}


def save_weekly_w44(conn):
    p = periods.week_of(datetime(2026, 10, 28).date())
    content = {"wins": [{"id": "w1", "text": "x", "quote": "q", "entry_id": 1, "date": "2026-10-26"}],
               "tough_love": [], "focus": [], "focus_review": [], "short": False,
               "dropped": [], "unverified": False}
    return reflections.save(conn, kind="weekly", period_key=p.key, period_start=p.start.isoformat(),
                            period_end=p.end.isoformat(), model="m", content=content, now=NOW)


def versions(conn, kind, key):
    return [r["version"] for r in conn.execute(
        "SELECT version FROM reflections WHERE kind=? AND period_key=? ORDER BY version", (kind, key))]


def test_generate_now_midweek_does_not_block_scheduled_run(conn):
    eid = add_entry(conn, "2026-11-02")
    add_entry(conn, "2026-11-03")
    add_entry(conn, "2026-11-04")
    llm = FakeLLM([good(eid), good(eid)])
    jid = jobs.request_now(conn, "weekly", WED)
    jobs.run_pending(conn, llm, WED)
    assert versions(conn, "weekly", "2026-W45") == [1]
    assert jid in jobs.ensure_due_jobs(conn, SUN)
    jobs.run_pending(conn, llm, SUN)
    assert versions(conn, "weekly", "2026-W45") == [1, 2]
    assert conn.execute("SELECT COUNT(*) FROM reflection_jobs WHERE kind='weekly' AND period_key='2026-W45'"
                        ).fetchone()[0] == 1


def test_generate_now_midmonth_does_not_block_scheduled_monthly_run(conn):
    save_weekly_w44(conn)  # the week ending 1 Nov belongs to November
    llm = FakeLLM([monthly_out(), monthly_out()])
    jid = jobs.request_now(conn, "monthly", WED)  # October has no weekly reflections: current month
    assert conn.execute("SELECT period_key FROM reflection_jobs WHERE id=?", (jid,)).fetchone()[0] == "2026-11"
    jobs.run_pending(conn, llm, WED)
    assert versions(conn, "monthly", "2026-11") == [1]
    assert jid in jobs.ensure_due_jobs(conn, DEC1)
    jobs.run_pending(conn, llm, DEC1)
    assert versions(conn, "monthly", "2026-11") == [1, 2]
    assert conn.execute("SELECT COUNT(*) FROM reflection_jobs WHERE kind='monthly' AND period_key='2026-11'"
                        ).fetchone()[0] == 1


def test_scheduled_done_job_not_reset_again(conn):
    eid = add_entry(conn, "2026-11-02")
    llm = FakeLLM([good(eid), good(eid)])
    jobs.request_now(conn, "weekly", WED)
    jobs.run_pending(conn, llm, WED)
    jobs.ensure_due_jobs(conn, SUN)
    jobs.run_pending(conn, llm, SUN)
    later = SUN + timedelta(minutes=5)
    assert jobs.ensure_due_jobs(conn, later) == []
    assert jobs.run_pending(conn, llm, later) == []
    assert len(llm.calls) == 2


def test_skipped_scheduled_job_not_reset_churn(conn):
    jobs.ensure_due_jobs(conn, NOW)  # no entries: both skipped, queued after their due instants
    llm = FakeLLM()
    jobs.run_pending(conn, llm, NOW)
    for minutes in (5, 10):
        assert jobs.ensure_due_jobs(conn, NOW + timedelta(minutes=minutes)) == []
    assert job(conn)["status"] == "skipped"
    assert llm.calls == []


def test_legacy_rows_with_empty_queued_at_are_reset_once(conn):
    eid = add_entry(conn, "2026-10-27")
    conn.execute("INSERT INTO reflection_jobs(kind, period_key, status, created_at, queued_at)"
                 " VALUES ('weekly', ?, 'done', '2026-11-01T00:00:00Z', '')", (WEEK,))
    conn.commit()
    jid = job(conn)["id"]
    assert jid in jobs.ensure_due_jobs(conn, NOW)
    assert job(conn)["status"] == "pending"
    llm = FakeLLM([good(eid)])
    jobs.run_pending(conn, llm, NOW)
    assert jobs.ensure_due_jobs(conn, NOW + timedelta(minutes=5)) == []
    assert job(conn)["status"] == "done"


def test_stale_running_row_is_picked_up_by_next_tick(tmp_path):
    path = str(tmp_path / "stale.db")
    c = connect(path)
    db.init_db(c)
    jobs.ensure_due_jobs(c, NOW)
    c.execute("UPDATE reflection_jobs SET status = 'running'")
    c.commit()
    c.close()
    jobs.scheduler_tick(path, FakeLLM(), NOW)  # no entries: recovered jobs end up skipped
    c = connect(path)
    statuses = {r[0] for r in c.execute("SELECT status FROM reflection_jobs")}
    c.close()
    assert statuses == {"skipped"}


def test_init_db_adds_queued_at_to_legacy_table(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE reflection_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,"
                " period_key TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,"
                " next_attempt_at TEXT NULL, last_error TEXT NULL, reflection_id INTEGER NULL,"
                " created_at TEXT NOT NULL, UNIQUE (kind, period_key))")
    raw.execute("INSERT INTO reflection_jobs(kind, period_key, status, created_at)"
                " VALUES ('weekly', '2026-W44', 'done', '2026-11-01T00:00:00Z')")
    raw.commit()
    raw.close()
    for _ in range(2):  # idempotent
        c = connect(path)
        db.init_db(c)
        row = c.execute("SELECT queued_at FROM reflection_jobs").fetchone()
        c.close()
        assert row["queued_at"] == ""


def test_run_job_twice_on_same_job_one_llm_call(conn, week, tmp_path):
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    llm = FakeLLM([good(week)])
    jid = job(conn)["id"]
    assert jobs.run_job(path, llm, jid, NOW) is True
    assert jobs.run_job(path, llm, jid, NOW) is False  # no longer pending: claim fails, no model call
    assert len(llm.calls) == 1
    assert reflections_count(conn) == 1


# --- F5: a failed job must not block the scheduled run ---
def test_failed_midperiod_job_is_requeued_at_due_time_and_runs(conn):
    eid = add_entry(conn, "2026-11-02")
    jid = jobs.request_now(conn, "weekly", WED)
    jobs.run_pending(conn, FakeLLM([LLMError("x", retryable=False, reason="http_401")]), WED)
    assert job(conn, "weekly", "2026-W45")["status"] == "failed"
    assert jid in jobs.ensure_due_jobs(conn, SUN)
    row = job(conn, "weekly", "2026-W45")
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 0, None)
    assert row["queued_at"] == stamp(SUN)
    llm = FakeLLM([good(eid)])
    jobs.run_pending(conn, llm, SUN)
    assert job(conn, "weekly", "2026-W45")["status"] == "done"


def test_failed_job_queued_after_due_is_left_alone(conn):
    add_entry(conn, "2026-11-02")
    jobs.ensure_due_jobs(conn, SUN)
    jobs.run_pending(conn, FakeLLM([LLMError("x", retryable=False, reason="http_401")]), SUN)
    assert job(conn, "weekly", "2026-W45")["status"] == "failed"
    for minutes in (5, 10):
        assert jobs.ensure_due_jobs(conn, SUN + timedelta(minutes=minutes)) == []
    assert job(conn, "weekly", "2026-W45")["status"] == "failed"


# --- F6: monthly waits for weekly jobs ---
def save_weekly_w43(conn):
    """A weekly reflection that belongs to October, so the October monthly has input."""
    p = periods.week_of(datetime(2026, 10, 21).date())
    content = {"wins": [{"id": "w1", "text": "You finished a draft.", "quote": "q", "entry_id": 1,
                         "date": "2026-10-21"}],
               "tough_love": [], "focus": [], "focus_review": [], "short": False,
               "dropped": [], "unverified": False}
    return reflections.save(conn, kind="weekly", period_key=p.key, period_start=p.start.isoformat(),
                            period_end=p.end.isoformat(), model="m", content=content, now=NOW)


def test_monthly_not_touched_while_weekly_pending(conn, week):
    save_weekly_w43(conn)  # the monthly has input, so a premature run would call the model
    llm = FakeLLM([LLMError("x", retryable=True, reason="timeout")])
    jobs.run_pending(conn, llm, NOW)
    assert job(conn)["status"] == "pending"
    month = job(conn, "monthly")
    assert (month["status"], month["attempts"], month["next_attempt_at"]) == ("pending", 0, None)
    assert len(llm.calls) == 1  # the weekly call only
    assert jobs.run_pending(conn, llm, NOW + timedelta(seconds=10)) == []  # weekly waits, monthly waits
    assert len(llm.calls) == 1


def test_monthly_runs_after_weekly_done_and_sees_weekly_reflection(conn):
    eid = add_entry(conn, "2026-10-21")
    jobs.ensure_due_jobs(conn, NOW)  # W44 (no entries) and October
    conn.execute("INSERT INTO reflection_jobs(kind, period_key, status, created_at, queued_at)"
                 " VALUES ('weekly', '2026-W43', 'pending', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')")
    conn.commit()
    llm = FakeLLM([LLMError("x", retryable=True, reason="timeout"), good(eid), monthly_out()])
    jobs.run_pending(conn, llm, NOW)
    assert job(conn, "weekly", "2026-W43")["status"] == "pending"
    assert job(conn, "monthly")["status"] == "pending" and job(conn, "monthly")["attempts"] == 0
    assert len(llm.calls) == 1
    jobs.run_pending(conn, llm, NOW + timedelta(seconds=61))
    assert job(conn, "weekly", "2026-W43")["status"] == "done"
    assert job(conn, "monthly")["status"] == "done"
    assert versions(conn, "monthly", MONTH) == [1]
    assert len(llm.calls) == 3
    assert "You finished a draft." in llm.calls[2]["user"]


def test_failed_weekly_does_not_block_monthly(conn, week):
    save_weekly_w43(conn)
    llm = FakeLLM([LLMError("x", retryable=False, reason="http_401"), monthly_out()])
    jobs.run_pending(conn, llm, NOW)
    assert job(conn)["status"] == "failed"
    assert job(conn, "monthly")["status"] == "done"


# --- F4: job logs are emitted and carry no text ---
class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_animus_logger_configured_once_at_info(tmp_path):
    for i in range(3):
        create_app(str(tmp_path / f"l{i}.db"), start_scheduler=False)
    logger = logging.getLogger("animus")
    own = [h for h in logger.handlers if getattr(h, "_animus", False)]
    assert len(own) == 1
    assert logger.level == logging.INFO and logger.propagate is False
    assert logging.getLogger("animus.jobs").getEffectiveLevel() == logging.INFO


def test_job_logs_for_done_retry_failed_carry_kind_period_reason_only(conn, week, tmp_path):
    create_app(str(tmp_path / "log.db"), start_scheduler=False)
    collect = _Collect()
    logger = logging.getLogger("animus")
    logger.addHandler(collect)
    try:
        sentinel = LLMError(f"SENTINEL {TEXT}", retryable=True, reason="timeout")
        jobs.run_pending(conn, FakeLLM([sentinel]), NOW)  # retry
        force_due(conn)
        jobs.run_pending(conn, FakeLLM([good(week)]), NOW)  # done
        jobs.request_now(conn, "weekly", NOW)
        jobs.run_pending(conn, FakeLLM([LLMError(f"SENTINEL {TEXT}", retryable=False, reason="http_401")]), NOW)
    finally:
        logger.removeHandler(collect)
    lines = [(r.levelno, r.getMessage()) for r in collect.records if "status=" in r.getMessage()]
    joined = " | ".join(m for _, m in lines)
    assert any("status=pending" in m and "reason=timeout" in m for _, m in lines)
    assert any("status=done" in m and "kind=weekly" in m and f"period={WEEK}" in m for _, m in lines)
    assert any("status=failed" in m and "reason=http_401" in m for _, m in lines)
    assert all("id=" in m for _, m in lines)
    assert [lvl for lvl, m in lines if "status=done" in m] == [logging.INFO]
    assert all(lvl >= logging.WARNING for lvl, m in lines if "status=failed" in m or "status=pending" in m)
    assert "SENTINEL" not in joined and TEXT not in joined and "first draft" not in joined
