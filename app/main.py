import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import ask, config, counselor, dashboard, db, embeddings, entries, extract, focus, jobs, links, memory, reflections, tz
from app import auth as authn
from app.limits import LoginLimiter
from app.csrf import CsrfMiddleware
from app.headers import BODY_MAX, BodyLimitMiddleware, SecurityHeadersMiddleware
from app.security import CrossSiteGuardMiddleware
from app.moods import MOODS

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


class CachedStaticFiles(StaticFiles):
    """Public, read-only assets (GET/HEAD only, no directory listing, no traversal) cached for a day."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response
LIST_LIMIT = 100
# Only loopback may reach the app (blocks DNS rebinding). Extra names come from ANIMUS_ALLOWED_HOSTS.
DEFAULT_ALLOWED_HOSTS = config.BASE_ALLOWED_HOSTS
MOOD_BY_KEY = {m.key: m for m in MOODS}
ENTRY_MAX = 20_000  # characters of entry text (after trimming)
NOTE_MAX = 300  # owner's optional note on a correction, in characters
ENTRY_NOTE_MAX = 1000  # owner's thoughts on one entry, in characters
QUOTED_CLAIM_MAX = 200  # claim text quoted in the stored note; keeps one note under about 600 characters


def _one_line(text: str) -> str:
    """Collapse every run of whitespace (newlines included) so a note cannot imitate a prompt section."""
    return " ".join(text.split())


def _quoted(text: str) -> str:
    text = _one_line(text)
    return text if len(text) <= QUOTED_CLAIM_MAX else text[: QUOTED_CLAIM_MAX - 1].rstrip() + "\u2026"


def _greeting(hour: int) -> str:
    if hour < 5:
        return "Late night"
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def _entry_view(row, zone: str, conn=None) -> dict:
    """Template-ready entry, sentence case.

    The day is the entry's own local_date (fixed when it was written, never recomputed);
    the time of day is created_at (UTC) shown in the currently stored timezone.
    """
    utc = datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    local = utc.astimezone(ZoneInfo(zone))
    day = date.fromisoformat(row["local_date"])
    this_year = datetime.now(timezone.utc).astimezone(ZoneInfo(zone)).year
    year = "" if day.year == this_year else f" {day.year}"
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "when": f"{day:%A} {day.day} {day:%B}{year}, {local:%-I:%M %p}",
        "text": row["text"],
        "mood": MOOD_BY_KEY.get(row["mood"]),
        "energy": row["energy_score"],
        "energy_approx": bool(row["energy_is_approximate"]),
        "links": links.links_for_entry(conn, row["id"]) if conn is not None else None,
    }


def _citation_view(c) -> dict:
    day = date.fromisoformat(c.date)
    return {"entry_id": c.entry_id, "iso": c.date, "when": f"{day:%A} {day.day} {day:%B %Y}", "quote": c.quote}


def _parse_energy(energy: str) -> int | None:
    if not energy.strip():
        return None
    try:
        return int(energy)
    except ValueError:
        raise ValueError("energy must be a whole number")


def _friendly(exc: ValueError) -> str:
    message = str(exc)
    if "too long" in message:
        return "That entry is too long (limit 20,000 characters)."
    if "text" in message:
        return "Write something first. An empty entry isn't saved."
    if "energy" in message:
        return "Energy must be a whole number from 1 to 10."
    return "That mood isn't one of the five choices."


def _check_length(text: str) -> None:
    if len(text.strip()) > ENTRY_MAX:
        raise ValueError("text too long")


def _security_context(request: Request) -> dict:
    """Per-request template globals: the session's CSRF token and this response's CSP nonce."""
    state = request.scope.get("state") or {}
    session = state.get("session")
    return {"csrf_token": session.csrf if session is not None else "",
            "csp_nonce": state.get("csp_nonce", "")}


def _parse_cursor(value: str) -> tuple[str, int]:
    """'<created_at>_<id>' -> (created_at, id); anything else is a 400."""
    created_at, _, raw_id = value.rpartition("_")
    try:
        datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
        return created_at, int(raw_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Bad paging cursor")


def _configure_logging() -> None:
    """INFO job logs on stderr (journald). Idempotent: create_app runs many times in tests."""
    logger = logging.getLogger("animus")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(h, "_animus", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        handler._animus = True
        logger.addHandler(handler)


def _scheduler_enabled(flag: bool | None) -> bool:
    if flag is not None:
        return flag
    return os.environ.get("ANIMUS_SCHEDULER", "on").strip().lower() not in ("off", "0")


def create_app(
    db_path: str | None = None,
    allowed_hosts=None,
    llm=None,
    start_scheduler: bool | None = None,
    auth: authn.AuthSettings | None = None,
    limiter: LoginLimiter | None = None,
    clock=time.time,
) -> FastAPI:
    _configure_logging()
    if allowed_hosts is None:
        allowed_hosts = config.get_allowed_hosts()
    path = db_path or config.get_db_path()
    if not db_path and path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    boot = db.connect(path)
    try:
        db.init_db(boot)
        generation = authn.SessionGeneration.load(boot)
    finally:
        boot.close()

    enabled = _scheduler_enabled(start_scheduler)
    settings = auth if auth is not None else config.get_auth_settings()
    limiter = limiter if limiter is not None else LoginLimiter()
    auth_log = logging.getLogger("animus.auth")
    # At most two bcrypt checks run at once (one app per process, so this is the global gate).
    bcrypt_gate = asyncio.Semaphore(2)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        if enabled:
            conn = db.connect(path)
            try:
                jobs.recover_running(conn)  # a crash mid-call left jobs running
            finally:
                conn.close()
            app.state.scheduler_task = asyncio.create_task(
                jobs.scheduler_loop(path, app.state.llm, stop_event=stop)
            )
        yield
        task = app.state.scheduler_task
        if task is not None:
            stop.set()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (TimeoutError, asyncio.TimeoutError):
                task.cancel()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.llm = llm  # None: the real client is built lazily inside each scheduler run
    app.state.db_path = path
    app.state.scheduler_task = None
    app.state.auth = settings
    app.state.limiter = limiter
    app.state.clock = clock
    app.state.session_gen = generation
    # Order, outermost first: TrustedHost, BodyLimit (400 KB, before anything reads a body; /login
    # has its own 8 KB limit inside Auth), SecurityHeaders (CSP nonce, no-store), CrossSiteGuard,
    # Auth (session into scope["state"]), Csrf (needs the session, so innermost).
    # add_middleware wraps, so the innermost layer is added first.
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(authn.AuthMiddleware, settings=settings, clock=clock, generation=generation)
    app.add_middleware(CrossSiteGuardMiddleware,
                       allowed_hosts=[*DEFAULT_ALLOWED_HOSTS, *allowed_hosts])
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(BodyLimitMiddleware, max_bytes=BODY_MAX)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    # Public assets only (HTMX, Tailwind); never put journal data under app/static.
    app.mount("/static", CachedStaticFiles(directory=str(STATIC_DIR), html=False, follow_symlink=False),
              name="static")
    # autoescape on for .html; csrf_token and csp_nonce are bound per request
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_security_context])

    # Async so the connection is opened, used and closed on one thread (sqlite requirement).
    async def get_conn():
        conn = db.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    def error(request: Request, message: str) -> Response:
        return templates.TemplateResponse(
            request, "_error.html", {"message": message}, status_code=422
        )

    @app.get("/theme-state")
    async def theme_state(conn=Depends(get_conn)):
        """Latest mood from the last 3 days; the page uses it to tint the background glow."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = conn.execute(
            "SELECT mood FROM entries WHERE mood IS NOT NULL AND created_at >= ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (cutoff,),
        ).fetchone()
        return JSONResponse({"mood": row["mood"] if row else ""}, headers=authn.NO_STORE)

    @app.get("/healthz")
    async def healthz():
        return PlainTextResponse("ok", headers=authn.NO_STORE)

    def login_form(request: Request, next_url: str, message: str | None, status_code: int,
                   headers: dict | None = None) -> Response:
        return templates.TemplateResponse(
            request, "login.html", {"next": authn.safe_next(next_url), "message": message},
            status_code=status_code, headers={**authn.NO_STORE, **(headers or {})},
        )

    def too_many(request: Request, next_url: str, ip: str) -> Response:
        left = limiter.blocked(ip)
        if left:
            minutes = (left + 59) // 60
            message = f"Too many tries. Try again in {minutes} minute{'s' if minutes != 1 else ''}."
        else:  # attempts still being checked
            message = "Too many tries at once. Try again in a moment."
        return login_form(request, next_url, message, 429, {"Retry-After": str(max(left, 1))})

    @app.get("/login")
    async def login_page(request: Request, next_url: str = Query("", alias="next")):
        if request.scope.get("state", {}).get("session") is not None:
            return RedirectResponse(authn.safe_next(next_url), status_code=303)
        return login_form(request, next_url, None, 200)

    @app.post("/login")
    async def login_submit(request: Request, password: str = Form(""),
                           next_url: str = Form("", alias="next")):
        ip = authn.client_ip(request.scope)
        tag = authn.ip_tag(ip, settings.secret)
        if limiter.blocked(ip):
            auth_log.warning("login refused reason=blocked ip=%s", tag)
            return too_many(request, next_url, ip)
        # Reserve the attempt before the slow check: failures plus checks in flight stay <= 5.
        if not limiter.reserve(ip):
            auth_log.warning("login refused reason=busy ip=%s", tag)
            return too_many(request, next_url, ip)
        outcome: list[bool] = []  # set by the check itself, so a cancelled handler still knows it
        recorded = False

        def check() -> bool:
            result = authn.verify_password(settings, password)
            outcome.append(result)
            return result

        try:
            # bcrypt takes about 0.25 s at cost 12: off the event loop, at most two at once.
            async with bcrypt_gate:
                ok = await run_in_threadpool(check)
            if not ok:
                if not password:
                    reason = "empty"
                elif len(password) > authn.PASSWORD_MAX:
                    reason = "too_long"
                else:
                    reason = "bad_password"
                limiter.fail(ip)
                recorded = True
                auth_log.warning("login failed reason=%s ip=%s", reason, tag)
                if limiter.blocked(ip):
                    auth_log.warning("login blocked reason=too_many_failures ip=%s", tag)
                return login_form(request, next_url, authn.FAILED, 401)
        finally:
            if outcome and outcome[0] is False and not recorded:  # cancelled after a wrong password
                limiter.fail(ip)
            limiter.release(ip)
        if limiter.blocked(ip):  # blocked by parallel failures while this check ran
            auth_log.warning("login refused reason=blocked ip=%s", tag)
            return too_many(request, next_url, ip)
        limiter.clear(ip)
        response = RedirectResponse(authn.safe_next(next_url), status_code=303, headers=authn.NO_STORE)
        authn.set_session_cookie(response, authn.issue_session(settings, clock(), generation.value),
                                 secure=authn.is_https(request.scope), max_age=settings.lifetime_s)
        auth_log.info("login ok ip=%s", tag)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        # Fail closed: every cookie issued so far stops working, copies included, even when the
        # database is unreachable (bump moves the in-memory generation first and never raises).
        conn = None
        try:
            conn = db.connect(path)
        except Exception as exc:  # noqa: BLE001
            auth_log.error("logout: database not opened (%s)", type(exc).__name__)
        try:
            generation.bump(conn)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        response = RedirectResponse("/login", status_code=303, headers=authn.NO_STORE)
        authn.clear_session_cookie(response, secure=authn.is_https(request.scope))
        return response

    @app.get("/")
    async def write_screen(request: Request, conn=Depends(get_conn)):
        zone = tz.get_timezone(conn)
        now = datetime.now(timezone.utc).astimezone(ZoneInfo(zone))
        return templates.TemplateResponse(
            request,
            "write.html",
            {
                "moods": MOODS,
                "greeting": _greeting(now.hour),
                "date_line": f"{now:%A} {now.day} {now:%B}",
                "clock": f"{now:%-I:%M %p}",
                "page": "write",
            },
        )

    @app.post("/entries")
    async def save_entry(
        request: Request,
        background: BackgroundTasks,
        text: str = Form(""),
        mood: str = Form(""),
        energy: str = Form(""),
        conn=Depends(get_conn),
    ):
        try:
            _check_length(text)
            new_id = entries.create_entry(
                conn,
                text=text,
                mood=mood or None,
                energy=_parse_energy(energy),
                now=datetime.now(timezone.utc),
                tz=tz.get_timezone(conn),
            )
        except ValueError as exc:
            return error(request, _friendly(exc))
        if app.state.llm is not None or config.get_llm_settings().api_key is not None:
            background.add_task(_suggest_links, app.state.db_path, app.state.llm, new_id)
        return templates.TemplateResponse(request, "_saved.html", {})

    def _embedder():
        """The embedder for question vectors: an injected one, else the real one when a key is
        set and no test client is in place. None means keyword search only."""
        injected = getattr(app.state, "embedder", None)
        if injected is not None:
            return injected
        if app.state.llm is None and config.get_llm_settings().api_key is not None:
            return embeddings.Embedder()
        return None

    def _load(conn, entry_id: int):
        row = entries.get_entry(conn, entry_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Entry not found")
        return row

    def _suggest_links(db_path: str, llm, entry_id: int) -> None:
        """Background task: own connection, own LLM client; failures are swallowed inside."""
        bg = db.connect(db_path)
        try:
            client = jobs.resolve_llm(llm)
            links.suggest_for_entry(bg, client, entry_id,
                                    getattr(app.state, "embedder", None) or embeddings.Embedder())
            if llm is None:  # the real client only: a test double has no reply queued for this
                extract.fill_missing(bg, client, limit=1)
        finally:
            bg.close()

    @app.post("/entries/{entry_id}/links/{link_id}/confirm")
    async def link_confirm(request: Request, entry_id: int, link_id: int, conn=Depends(get_conn)):
        _load(conn, entry_id)
        if not links.set_status(conn, entry_id, link_id, "confirmed"):
            raise HTTPException(status_code=404, detail="Link not found")
        return templates.TemplateResponse(
            request, "_entry_row.html", {"e": _entry_view(_load(conn, entry_id), tz.get_timezone(conn), conn)})

    @app.post("/entries/{entry_id}/links/{link_id}/dismiss")
    async def link_dismiss(request: Request, entry_id: int, link_id: int, conn=Depends(get_conn)):
        _load(conn, entry_id)
        if not links.set_status(conn, entry_id, link_id, "dismissed"):
            raise HTTPException(status_code=404, detail="Link not found")
        return templates.TemplateResponse(
            request, "_entry_row.html", {"e": _entry_view(_load(conn, entry_id), tz.get_timezone(conn), conn)})

    @app.delete("/entries/{entry_id}/links/{link_id}")
    async def link_remove(request: Request, entry_id: int, link_id: int, conn=Depends(get_conn)):
        _load(conn, entry_id)
        if not links.remove(conn, entry_id, link_id):
            raise HTTPException(status_code=404, detail="Link not found")
        return templates.TemplateResponse(
            request, "_entry_row.html", {"e": _entry_view(_load(conn, entry_id), tz.get_timezone(conn), conn)})

    @app.get("/entries")
    async def entries_page(request: Request, before: str | None = None, conn=Depends(get_conn)):
        zone = tz.get_timezone(conn)
        cursor = _parse_cursor(before) if before else None
        rows = entries.list_entries(conn, LIST_LIMIT + 1, cursor)  # one extra row: are there more?
        has_more = len(rows) > LIST_LIMIT
        rows = rows[:LIST_LIMIT]
        context = {
            "rows": [_entry_view(r, zone, conn) for r in rows],
            "next_cursor": f"{rows[-1]['created_at']}_{rows[-1]['id']}" if has_more else None,
            "paged": cursor is not None,
            "page": "entries",
        }
        template = "_entries_more.html" if request.headers.get("HX-Request") else "entries.html"
        return templates.TemplateResponse(request, template, context)

    @app.get("/entries/{entry_id}")
    async def entry_row(request: Request, entry_id: int, conn=Depends(get_conn)):
        row = _load(conn, entry_id)
        return templates.TemplateResponse(
            request, "_entry_row.html", {"e": _entry_view(row, tz.get_timezone(conn), conn)}
        )

    def _edit_form(request, conn, row, *, text, mood, energy, message=None, status_code=200):
        return templates.TemplateResponse(
            request,
            "_entry_edit.html",
            {
                "e": _entry_view(row, tz.get_timezone(conn), conn),
                "moods": MOODS,
                "form_text": text,
                "form_mood": mood,
                "form_energy": energy,
                "message": message,
            },
            status_code=status_code,
        )

    @app.get("/entries/{entry_id}/edit")
    async def entry_edit(request: Request, entry_id: int, conn=Depends(get_conn)):
        row = _load(conn, entry_id)
        energy = "" if row["energy_score"] is None else str(row["energy_score"])
        return _edit_form(request, conn, row, text=row["text"], mood=row["mood"] or "", energy=energy)

    @app.put("/entries/{entry_id}")
    async def entry_update(
        request: Request,
        entry_id: int,
        text: str = Form(""),
        mood: str = Form(""),
        energy: str = Form(""),
        conn=Depends(get_conn),
    ):
        row = _load(conn, entry_id)
        try:
            _check_length(text)
            entries.update_entry(
                conn, entry_id, text=text, mood=mood or None, energy=_parse_energy(energy)
            )
        except ValueError as exc:
            return _edit_form(
                request, conn, row, text=text, mood=mood, energy=energy,
                message=_friendly(exc), status_code=422,
            )
        return templates.TemplateResponse(
            request,
            "_entry_row.html",
            {"e": _entry_view(entries.get_entry(conn, entry_id), tz.get_timezone(conn), conn)},
        )

    @app.delete("/entries/{entry_id}")
    async def entry_delete(entry_id: int, conn=Depends(get_conn)):
        _load(conn, entry_id)
        entries.delete_entry(conn, entry_id)
        return Response(status_code=200)

    # ---- Reflections dashboard ----

    def _status_fragment(request: Request, kind: str, state: dict, n: int = 0) -> Response:
        return templates.TemplateResponse(request, "_status.html", {"kind": kind, "s": state, "n": n})

    def _focus_fragment(request: Request, conn, status_code: int = 200) -> Response:
        return templates.TemplateResponse(
            request, "_focus_list.html", {"focus": dashboard.focus_view(conn, tz.get_timezone(conn))},
            status_code=status_code,
        )

    def _kind(kind: str) -> str:
        if kind not in jobs.KINDS:
            raise HTTPException(status_code=400, detail="Unknown reflection kind")
        return kind

    @app.get("/reflections")
    async def reflections_page(request: Request, conn=Depends(get_conn)):
        context = dashboard.page_context(conn, datetime.now(timezone.utc))
        return templates.TemplateResponse(request, "dashboard.html", context)

    @app.post("/reflections/generate")
    async def reflections_generate(
        request: Request, background: BackgroundTasks, kind: str = "", conn=Depends(get_conn)
    ):
        _kind(kind)
        if app.state.llm is None and config.get_llm_settings().api_key is None:
            return _status_fragment(request, kind, dashboard.NO_KEY_STATE)  # no job, no API call
        now = datetime.now(timezone.utc)
        job_id = jobs.request_now(conn, kind, now)
        background.add_task(jobs.run_job, app.state.db_path, app.state.llm, job_id, now)
        return _status_fragment(request, kind, dashboard.reflection_state(conn, kind, now))

    @app.get("/reflections/status")
    async def reflections_status(request: Request, kind: str = "", n: int = 0, conn=Depends(get_conn)):
        _kind(kind)
        state = dashboard.reflection_state(conn, kind, datetime.now(timezone.utc))
        capped = state["poll"] and n >= dashboard.MAX_POLLS
        if capped:  # a job nothing finishes must not poll for as long as the tab is open
            state = dashboard.STILL_WORKING_STATE
        response = _status_fragment(request, kind, state, n)
        if not capped and not state["poll"] and request.headers.get("HX-Request"):
            response.headers["HX-Refresh"] = "true"  # polling ended: reload the panel and state line
        return response

    @app.get("/focus")
    async def focus_list(request: Request, conn=Depends(get_conn)):
        return _focus_fragment(request, conn)

    @app.post("/focus/{focus_id}/status")
    async def focus_status(
        request: Request, focus_id: int, status: str = Form(""), conn=Depends(get_conn)
    ):
        if status not in ("active", "done"):
            return error(request, "Choose active or done.")
        if not focus.set_status(conn, focus_id, status, datetime.now(timezone.utc)):
            raise HTTPException(status_code=404, detail="Focus area not found")
        return _focus_fragment(request, conn)

    @app.get("/reflections/{reflection_id}/view")
    async def reflection_view(request: Request, reflection_id: int, conn=Depends(get_conn)):
        row = reflections.get(conn, reflection_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Reflection not found")
        return templates.TemplateResponse(
            request, "_report.html", dashboard.report_context(conn, row, tz.get_timezone(conn)),
        )

    def _note_entry(conn, reflection_id: int, entry_id: int):
        if reflections.get(conn, reflection_id) is None:
            raise HTTPException(status_code=404, detail="Reflection not found")
        entry = conn.execute("SELECT id, created_at, local_date, text FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if entry is None:
            raise HTTPException(status_code=404, detail="Entry not found")
        return entry

    def _note_fragment(request, conn, reflection_id, entry, *, note="", message=None, done=False, status_code=200):
        return templates.TemplateResponse(
            request, "_entry_note.html",
            {"rid": reflection_id, "eid": entry["id"], "text": entry["text"], "note": note, "message": message,
             "done": done, "note_max": ENTRY_NOTE_MAX, "when": dashboard._entry_when(entry, tz.get_timezone(conn))},
            status_code=status_code,
        )

    @app.get("/reflections/{reflection_id}/entries/{entry_id}/note")
    async def entry_note_form(request: Request, reflection_id: int, entry_id: int, conn=Depends(get_conn)):
        return _note_fragment(request, conn, reflection_id, _note_entry(conn, reflection_id, entry_id))

    @app.post("/reflections/{reflection_id}/entries/{entry_id}/note")
    async def entry_note_save(
        request: Request, reflection_id: int, entry_id: int, note: str = Form(""), conn=Depends(get_conn)
    ):
        entry = _note_entry(conn, reflection_id, entry_id)
        note = note.strip()
        if not note:
            return _note_fragment(request, conn, reflection_id, entry, status_code=422,
                                  message="Write something first.")
        if len(note) > ENTRY_NOTE_MAX:
            return _note_fragment(
                request, conn, reflection_id, entry, note=note, status_code=422,
                message=f"That is {len(note)} characters. Please keep it to {ENTRY_NOTE_MAX} characters or fewer.",
            )
        when = dashboard._entry_when(entry, tz.get_timezone(conn))
        memory.add_note(
            conn, kind="note", text=f"My thoughts on my entry from {when}: {_one_line(note)}",
            reflection_id=reflection_id, now=datetime.now(timezone.utc),
        )
        return _note_fragment(request, conn, reflection_id, entry, done=True)

    # ---- Corrections and memory notes ----

    def _claim(conn, reflection_id: int, claim_id: str) -> dict:
        row = reflections.get(conn, reflection_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Reflection not found")
        try:
            content = reflections.content(row)
        except ValueError:
            content = {}
        claim = dashboard.claim_index(content, reflection_id).get(claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail="Claim not found")
        return claim

    def _correction_fragment(request, reflection_id, claim_id, *, note="", message=None, done=False, status_code=200):
        return templates.TemplateResponse(
            request,
            "_correction_form.html",
            {"rid": reflection_id, "cid": claim_id, "note": note, "message": message, "done": done,
             "note_max": NOTE_MAX},
            status_code=status_code,
        )

    @app.get("/reflections/{reflection_id}/claims/{claim_id:path}/correct")
    async def correction_form(request: Request, reflection_id: int, claim_id: str, conn=Depends(get_conn)):
        _claim(conn, reflection_id, claim_id)
        return _correction_fragment(request, reflection_id, claim_id)

    @app.post("/reflections/{reflection_id}/claims/{claim_id:path}/correct")
    async def correction_save(
        request: Request, reflection_id: int, claim_id: str, note: str = Form(""), conn=Depends(get_conn)
    ):
        claim = _claim(conn, reflection_id, claim_id)
        note = note.strip()
        if len(note) > NOTE_MAX:  # nothing saved; the form comes back with the typed note
            return _correction_fragment(
                request, reflection_id, claim_id, note=note, status_code=422,
                message=f"That note is {len(note)} characters. Please keep it to {NOTE_MAX} characters or fewer.",
            )
        text = f'Not what I meant: "{_quoted(claim["text"])}".'
        if note:
            text += " " + _one_line(note)
        memory.add_note(
            conn, kind="correction", text=text, reflection_id=reflection_id, claim_id=claim_id,
            now=datetime.now(timezone.utc),
        )
        return _correction_fragment(request, reflection_id, claim_id, done=True)

    @app.get("/memory")
    async def memory_page(request: Request, conn=Depends(get_conn)):
        context = dashboard.memory_view(conn, tz.get_timezone(conn))
        context["page"] = "memory"
        return templates.TemplateResponse(request, "memory.html", context)

    @app.delete("/memory/{note_id}")
    async def memory_delete(note_id: int, conn=Depends(get_conn)):
        if not memory.delete(conn, note_id, datetime.now(timezone.utc)):
            raise HTTPException(status_code=404, detail="Note not found")
        return Response(status_code=200)

    # ---- Ask My Journal ----

    @app.get("/ask")
    async def ask_page(request: Request):
        return templates.TemplateResponse(request, "ask.html", {"page": "ask"})

    @app.post("/ask")
    async def ask_post(request: Request, question: str = Form(""), conn=Depends(get_conn)):
        zone = tz.get_timezone(conn)
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(zone)).date()
        try:
            qvec = await asyncio.to_thread(embeddings.embed_query, _embedder(), ask.clean_question(question))
            ctx = ask.prepare(conn, question, today, qvec)  # on the request connection, before the thread
        except ValueError:
            return error(request, "Type a question first (up to 500 characters).")
        # Slow network call off the event loop; no database connection is used inside the thread.
        result = await asyncio.to_thread(ask.answer, jobs.resolve_llm(app.state.llm), ctx)
        return templates.TemplateResponse(
            request, "_ask_answer.html",
            {"r": result, "citations": [_citation_view(c) for c in result.citations]},
        )

    _ASK_STEPS = {
        "asking": "Asking the model",
        "checking": "Checking the answer against your entries",
        "retry": "Looking again, more carefully",
    }

    def _sse(kind: str, text: str) -> str:
        return "data: " + json.dumps({"k": kind, "t": text}) + "\n\n"

    @app.post("/ask/stream")
    async def ask_stream(request: Request, question: str = Form(""), conn=Depends(get_conn)):
        """Same answer as POST /ask, sent as server events: live step names, then the finished card."""
        zone = tz.get_timezone(conn)
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(zone)).date()
        try:
            qvec = await asyncio.to_thread(embeddings.embed_query, _embedder(), ask.clean_question(question))
            ctx = ask.prepare(conn, question, today, qvec)
        except ValueError:
            return error(request, "Type a question first (up to 500 characters).")
        llm = jobs.resolve_llm(app.state.llm)
        loop = asyncio.get_running_loop()
        steps: asyncio.Queue = asyncio.Queue()

        def progress(stage: str) -> None:  # called from the worker thread
            loop.call_soon_threadsafe(steps.put_nowait, _ASK_STEPS.get(stage, ""))

        async def events():
            n = len(ctx.entries)
            if n:
                yield _sse("step", f"Found {n} {'entry' if n == 1 else 'entries'} to read")
            task = asyncio.create_task(asyncio.to_thread(ask.answer, llm, ctx, progress))
            while not task.done() or not steps.empty():
                try:
                    text = await asyncio.wait_for(steps.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                if text:
                    yield _sse("step", text)
            try:
                result = await task
            except Exception as exc:
                result = ask._error_result(exc)
            page = templates.TemplateResponse(
                request, "_ask_answer.html",
                {"r": result, "citations": [_citation_view(c) for c in result.citations]},
            )
            yield _sse("done", page.body.decode("utf-8"))

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # ---- Counselor chat ----

    app.state.chat_busy = set()  # conversation ids with a model call in flight (single process)

    def _cid(raw: str) -> int:
        """Path id as an int; anything else (letters, huge numbers) is simply not found."""
        try:
            value = int(raw) if raw.isascii() and raw.isdigit() else 0
        except ValueError:
            value = 0
        if not 0 < value < 2**63:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return value

    def _chat_conv(conn, conv_id: int):
        conv = counselor.get_conversation(conn, conv_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conv

    def _local_when(ts: str, zone: str) -> str:
        utc = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        local = utc.astimezone(ZoneInfo(zone))
        return f"{local:%a} {local.day} {local:%b}, {local:%-I:%M %p}"

    def _chat_messages(conn, conv_id: int) -> list[dict]:
        msgs = counselor.messages(conn, conv_id)
        ids = sorted({i for m in msgs for i in m["cited_entry_ids"]})
        days = {}
        if ids:  # resolved now, so an entry deleted since the reply is simply skipped
            marks = ",".join("?" * len(ids))
            for r in conn.execute(f"SELECT id, local_date FROM entries WHERE id IN ({marks})", ids):
                days[r["id"]] = date.fromisoformat(r["local_date"])
        for m in msgs:
            m["cites"] = [
                {"id": i, "when": f"{days[i]:%A} {days[i].day} {days[i]:%B %Y}"}
                for i in m["cited_entry_ids"] if i in days
            ]
        return msgs

    def _thread(request, conn, conv_id: int, notice=None, status_code: int = 200, retry: bool = True):
        conv = _chat_conv(conn, conv_id)
        msgs = _chat_messages(conn, conv_id)
        ended = conv["ended_at"] is not None
        pending = bool(msgs) and msgs[-1]["role"] == "user" and not ended
        if pending and notice is None:
            notice = {"kind": "info", "text": "This message has not been answered yet."}
        return templates.TemplateResponse(
            request, "_chat_thread.html",
            {"conv_id": conv_id, "msgs": msgs, "ended": ended, "pending": pending and retry,
             "notice": notice},
            status_code=status_code,
        )

    def _chat_page(request, conn, conv_id: int | None):
        zone = tz.get_timezone(conn)
        rows = counselor.list_conversations(conn)
        convs = [
            {"id": r["id"], "when": _local_when(r["started_at"], zone), "count": r["message_count"],
             "ended": r["ended_at"] is not None}
            for r in rows
        ]
        context = {"page": "counselor", "convs": convs, "conv_id": conv_id}
        if conv_id is not None:
            conv = _chat_conv(conn, conv_id)
            msgs = _chat_messages(conn, conv_id)
            ended = conv["ended_at"] is not None
            pending = bool(msgs) and msgs[-1]["role"] == "user" and not ended
            context.update(
                msgs=msgs, ended=ended, pending=pending, conv_when=_local_when(conv["started_at"], zone),
                notice={"kind": "info", "text": "This message has not been answered yet."} if pending else None,
            )
        return templates.TemplateResponse(request, "counselor.html", context)

    async def _chat_turn(request, conn, conv_id: int, text: str | None):
        """One reply round. text is the owner's new message, or None to retry the last one."""
        conv = _chat_conv(conn, conv_id)
        busy = app.state.chat_busy
        if conv_id in busy:  # no second model call for the same conversation
            return _thread(request, conn, conv_id, status_code=409, notice={
                "kind": "info", "text": "A reply is already on its way. Give it a moment."},
                           retry=False)
        busy.add(conv_id)
        try:
            now = datetime.now(timezone.utc)
            if text is not None:
                try:
                    counselor.add_user_message(conn, conv_id, text, now)  # saved before the model call
                except ValueError as exc:
                    if str(exc) == "conversation":
                        raise HTTPException(status_code=404, detail="Conversation not found")
                    if str(exc) == "ended":
                        return _thread(request, conn, conv_id, status_code=409, notice={
                            "kind": "info", "text": "This conversation has ended."})
                    return _thread(request, conn, conv_id, status_code=422, notice={
                        "kind": "error",
                        "text": f"Write a message first (up to {counselor.MESSAGE_MAX} characters)."})
            else:
                last = conn.execute(
                    "SELECT role FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                    (conv_id,)).fetchone()
                if conv["ended_at"] is not None or last is None or last["role"] != "user":
                    return _thread(request, conn, conv_id, status_code=409, notice={
                        "kind": "info", "text": "There is nothing waiting for a reply."})
            zone = tz.get_timezone(conn)
            today = datetime.now(timezone.utc).astimezone(ZoneInfo(zone)).date()
            qvec = await asyncio.to_thread(
                embeddings.embed_query, _embedder(), counselor.query_text(conn, conv_id))
            ctx = counselor.prepare(conn, conv_id, today, qvec)  # on the request connection, before the thread
            # Slow network call off the event loop; no database connection is used inside the thread.
            result = await asyncio.to_thread(counselor.reply, jobs.resolve_llm(app.state.llm), ctx)
            counselor.save_reply(conn, conv_id, result, datetime.now(timezone.utc))
            notice = None
            if result.error:
                notice = {"kind": "error", "text": counselor.error_text(result.error)}
            return _thread(request, conn, conv_id, notice=notice)
        finally:
            busy.discard(conv_id)

    @app.get("/counselor")
    async def counselor_page(request: Request, conn=Depends(get_conn)):
        latest = next((r["id"] for r in counselor.list_conversations(conn) if r["ended_at"] is None), None)
        return _chat_page(request, conn, latest)

    @app.post("/counselor/new")
    async def counselor_new(conn=Depends(get_conn)):
        conv_id = counselor.start(conn, datetime.now(timezone.utc))
        return RedirectResponse(f"/counselor/{conv_id}", status_code=303)

    @app.get("/counselor/{conv_id}")
    async def counselor_view(request: Request, conv_id: str, conn=Depends(get_conn)):
        return _chat_page(request, conn, _cid(conv_id))

    @app.post("/counselor/{conv_id}/message")
    async def counselor_message(request: Request, conv_id: str, text: str = Form(""), conn=Depends(get_conn)):
        return await _chat_turn(request, conn, _cid(conv_id), text)

    @app.post("/counselor/{conv_id}/retry")
    async def counselor_retry(request: Request, conv_id: str, conn=Depends(get_conn)):
        return await _chat_turn(request, conn, _cid(conv_id), None)

    @app.post("/counselor/{conv_id}/end")
    async def counselor_end(conv_id: str, conn=Depends(get_conn)):
        conv_id = _cid(conv_id)
        _chat_conv(conn, conv_id)
        counselor.end(conn, conv_id, datetime.now(timezone.utc))
        return Response(status_code=204, headers={"HX-Redirect": f"/counselor/{conv_id}"})

    @app.delete("/counselor/{conv_id}")
    async def counselor_delete(conv_id: str, conn=Depends(get_conn)):
        if not counselor.delete(conn, _cid(conv_id)):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return Response(status_code=200, headers={"HX-Redirect": "/counselor"})

    @app.post("/timezone")
    async def post_timezone(request: Request, tz_name: str = Form("", alias="tz"), conn=Depends(get_conn)):
        try:
            tz.set_timezone(conn, tz_name)
        except ValueError:
            return Response(status_code=422)
        return Response(status_code=204)

    return app
