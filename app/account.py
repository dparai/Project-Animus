"""Routes for the app lock, passkeys, the device list and the daily reminder.

`install` is called once from create_app with the pieces these routes share with the login code.
Rules that hold everywhere here:
- a password or passkey check goes through the same per-IP limiter as the login form;
- changing how a device locks, or adding or removing a passkey, needs the password again
  ("sudo", five minutes, per device), so someone holding an unlocked phone cannot switch it off;
- nothing here logs a password, a key or a push address.
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app import auth as authn
from app import db, devices, passkeys, push, tz

log = logging.getLogger("animus.auth")

TIMER_LABELS = {0: "At once", 60: "After 1 minute", 300: "After 5 minutes", 900: "After 15 minutes"}
MODE_LABELS = {"none": "No lock", "passkey": "Fingerprint or face", "password": "Password"}
PROBLEMS = {
    "password": "That password didn't work.",
    "blocked": "Too many tries. Wait a few minutes, then try again.",
    "confirm": "Enter your password first to change lock settings.",
    "nopasskey": "Add a fingerprint first, then choose it as the lock.",
    "nodevice": "Log out and log in again on this device to set up its lock.",
    "time": "Pick a time like 21:00.",
}


def install(app, *, templates, get_conn, settings, limiter, generation, clock, bcrypt_gate, db_path,
            start_session):
    challenges = passkeys.Challenges()
    app.state.passkey_challenges = challenges

    def device_of(request: Request) -> dict | None:
        return (request.scope.get("state") or {}).get("device")

    def site_of(request: Request):
        if not passkeys.AVAILABLE:
            return None
        return passkeys.site(request.headers.get("host", ""), authn.is_https(request.scope))

    def scripted(request: Request) -> bool:
        return "x-requested-with" in request.headers

    async def body_json(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def renew(request: Request, response: Response, conn, device_id: str) -> None:
        """Re-issue the session for this device with the lifetime its lock allows (same CSRF token)."""
        session = request.scope["state"]["session"]
        lifetime = devices.lifetime_for(devices.get(conn, device_id), settings.lifetime_s)
        value = authn.issue_session(settings, clock(), generation.value, device=device_id,
                                    lifetime_s=lifetime, csrf=session.csrf)
        authn.set_session_cookie(response, value, secure=authn.is_https(request.scope), max_age=lifetime)

    async def password_ok(request: Request, password: str, what: str) -> str:
        """"ok", "bad" or "blocked". Same budget as the login form: 5 failures block the IP."""
        ip = authn.client_ip(request.scope)
        tag = authn.ip_tag(ip, settings.secret)
        if limiter.blocked(ip) or not limiter.reserve(ip):
            log.warning("%s refused reason=blocked ip=%s", what, tag)
            return "blocked"
        outcome: list[bool] = []
        recorded = False

        def check() -> bool:
            result = authn.verify_password(settings, password)
            outcome.append(result)
            return result

        try:
            async with bcrypt_gate:
                ok = await run_in_threadpool(check)
            if not ok:
                limiter.fail(ip)
                recorded = True
                log.warning("%s failed reason=bad_password ip=%s", what, tag)
                return "bad"
        finally:
            if outcome and outcome[0] is False and not recorded:
                limiter.fail(ip)
            limiter.release(ip)
        if limiter.blocked(ip):
            return "blocked"
        limiter.clear(ip)
        return "ok"

    def passkey_refused(request: Request, what: str) -> Response | None:
        """429 when this IP is blocked, else None."""
        ip = authn.client_ip(request.scope)
        if limiter.blocked(ip):
            log.warning("%s refused reason=blocked ip=%s", what, authn.ip_tag(ip, settings.secret))
            return JSONResponse({"ok": False, "message": PROBLEMS["blocked"]}, status_code=429,
                                headers=authn.NO_STORE)
        return None

    def passkey_failed(request: Request, what: str) -> Response:
        ip = authn.client_ip(request.scope)
        limiter.fail(ip)
        log.warning("%s failed reason=bad_passkey ip=%s", what, authn.ip_tag(ip, settings.secret))
        return JSONResponse({"ok": False, "message": authn.FAILED}, status_code=401, headers=authn.NO_STORE)

    def when(conn, stamp: int | None) -> str:
        if not stamp:
            return "never"
        local = datetime.fromtimestamp(stamp, timezone.utc).astimezone(ZoneInfo(tz.get_timezone(conn)))
        return f"{local.day} {local:%B %Y}, {local:%-I:%M %p}"

    # ---- the lock screen and unlocking ----

    def lock_page(request: Request, conn, next_url: str, message: str | None, status_code: int) -> Response:
        return templates.TemplateResponse(
            request, "locked.html", {"next": authn.safe_next(next_url), "message": message},
            status_code=status_code, headers=authn.NO_STORE)

    @app.get(authn.LOCKED_PATH)
    async def locked_page(request: Request, next_url: str = Query("", alias="next"), conn=Depends(get_conn)):
        device = device_of(request)
        if device is None or not device["locked"]:
            return RedirectResponse(authn.safe_next(next_url), status_code=303)
        return lock_page(request, conn, next_url, None, 200)

    @app.post("/unlock")
    async def unlock(request: Request, password: str = Form(""), next_url: str = Form("", alias="next"),
                     conn=Depends(get_conn)):
        device = device_of(request)
        target = authn.safe_next(next_url)
        if device is None:
            return JSONResponse({"ok": True}) if scripted(request) else RedirectResponse(target, status_code=303)
        result = await password_ok(request, password, "unlock")
        if result != "ok":
            message = authn.FAILED if result == "bad" else PROBLEMS["blocked"]
            status = 401 if result == "bad" else 429
            if scripted(request):
                return JSONResponse({"ok": False, "message": message}, status_code=status, headers=authn.NO_STORE)
            return lock_page(request, conn, next_url, message, status)
        devices.unlock(conn, device["id"], clock())
        response = (JSONResponse({"ok": True}, headers=authn.NO_STORE) if scripted(request)
                    else RedirectResponse(target, status_code=303, headers=authn.NO_STORE))
        renew(request, response, conn, device["id"])
        return response

    @app.post("/unlock/passkey/options")
    async def unlock_passkey_options(request: Request, conn=Depends(get_conn)):
        device, site = device_of(request), site_of(request)
        if device is None or site is None or not passkeys.count(conn):
            return JSONResponse({"ok": False}, status_code=404)
        return JSONResponse(passkeys.authentication_options(conn, site[0], challenges, "unlock", device["id"], clock()),
                            headers=authn.NO_STORE)

    @app.post("/unlock/passkey/verify")
    async def unlock_passkey_verify(request: Request, conn=Depends(get_conn)):
        device, site = device_of(request), site_of(request)
        if device is None or site is None:
            return JSONResponse({"ok": False}, status_code=404)
        refused = passkey_refused(request, "unlock")
        if refused is not None:
            return refused
        data = await body_json(request)
        if not passkeys.authenticate(conn, site[0], site[1], challenges, data.get("handle"), data.get("credential"),
                                     "unlock", device["id"], clock()):
            return passkey_failed(request, "unlock")
        devices.unlock(conn, device["id"], clock())
        response = JSONResponse({"ok": True}, headers=authn.NO_STORE)
        renew(request, response, conn, device["id"])
        return response

    @app.post("/lock")
    async def lock_now(request: Request, conn=Depends(get_conn)):
        device = device_of(request)
        if device is not None:
            devices.lock(conn, device["id"])
        return Response(status_code=204)

    @app.post("/lock/ping")
    async def lock_ping(request: Request, conn=Depends(get_conn)):
        device = device_of(request)
        if device is not None:  # a locked device never gets here: the gate answers 423 first
            devices.ping(conn, device["id"], clock())
        return Response(status_code=204)

    # ---- logging in with a passkey (no session yet) ----

    @app.post("/login/passkey/options")
    async def login_passkey_options(request: Request, conn=Depends(get_conn)):
        site = site_of(request)
        if site is None or not passkeys.count(conn):
            return JSONResponse({"ok": False}, status_code=404)
        refused = passkey_refused(request, "login")
        if refused is not None:
            return refused
        return JSONResponse(passkeys.authentication_options(conn, site[0], challenges, "login", None, clock()),
                            headers=authn.NO_STORE)

    @app.post("/login/passkey/verify")
    async def login_passkey_verify(request: Request, conn=Depends(get_conn)):
        site = site_of(request)
        if site is None:
            return JSONResponse({"ok": False}, status_code=404)
        refused = passkey_refused(request, "login")
        if refused is not None:
            return refused
        data = await body_json(request)
        if not passkeys.authenticate(conn, site[0], site[1], challenges, data.get("handle"), data.get("credential"),
                                     "login", None, clock()):
            return passkey_failed(request, "login")
        response = JSONResponse({"ok": True, "next": authn.safe_next(str(data.get("next") or ""))},
                                headers=authn.NO_STORE)
        start_session(request, response)
        log.info("login ok method=passkey ip=%s", authn.ip_tag(authn.client_ip(request.scope), settings.secret))
        return response

    # ---- settings page ----

    def back(code: str = "", key: str = "problem") -> Response:
        return RedirectResponse("/settings" + (f"?{key}={code}" if code else ""), status_code=303)

    def fresh(conn, request: Request):
        """This request's device row read again (the gate's copy may be a moment old)."""
        device = device_of(request)
        return devices.get(conn, device["id"]) if device is not None else None

    @app.get("/settings")
    async def settings_page(request: Request, problem: str = "", done: str = "", conn=Depends(get_conn)):
        device = fresh(conn, request)
        now = clock()
        on, at = push.get_reminder(conn)
        vapid = app.state.vapid
        people = [{**d, "seen": when(conn, d["last_seen_at"]), "mode_label": MODE_LABELS[d["lock_mode"]],
                   "here": device is not None and d["id"] == device["id"]}
                  for d in devices.list_all(conn, generation.value)]
        keys = [{**k, "added": when(conn, k["created_at"]), "used": when(conn, k["last_used_at"])}
                for k in passkeys.list_all(conn)]
        return templates.TemplateResponse(request, "settings.html", {
            "page": "settings",
            "device": dict(device) if device is not None else None,
            "sudo": devices.has_sudo(device, now),
            "timers": [(s, TIMER_LABELS[s]) for s in devices.TIMERS],
            "passkeys": keys,
            "passkeys_ready": site_of(request) is not None,
            "devices": people,
            "reminder_on": on, "reminder_time": at,
            "push_key": vapid.public_key if vapid is not None else "",
            "subscribed": device is not None and push.device_subscribed(conn, device["id"]),
            "problem": PROBLEMS.get(problem), "done": done in ("lock", "reminder", "passkey", "device"),
        })

    @app.post("/settings/confirm")
    async def settings_confirm(request: Request, password: str = Form(""), conn=Depends(get_conn)):
        device = fresh(conn, request)
        if device is None:
            return back("nodevice")
        result = await password_ok(request, password, "confirm")
        if result != "ok":
            return back("password" if result == "bad" else "blocked")
        devices.grant_sudo(conn, device["id"], clock())
        return back()

    @app.post("/settings/lock")
    async def settings_lock(request: Request, mode: str = Form(""), after: str = Form(""), conn=Depends(get_conn)):
        device = fresh(conn, request)
        if device is None:
            return back("nodevice")
        if not devices.has_sudo(device, clock()):
            return back("confirm")
        if mode == "passkey" and (not passkeys.AVAILABLE or not passkeys.count(conn)):
            return back("nopasskey")
        try:
            devices.set_lock(conn, device["id"], mode, int(after) if after.lstrip("-").isdigit() else -1, clock())
        except ValueError:
            return Response(status_code=422)
        response = back("lock", "done")
        renew(request, response, conn, device["id"])
        return response

    @app.post("/settings/passkeys/options")
    async def passkey_add_options(request: Request, conn=Depends(get_conn)):
        device, site = fresh(conn, request), site_of(request)
        if device is None or site is None:
            return JSONResponse({"ok": False}, status_code=404)
        if not devices.has_sudo(device, clock()):
            return JSONResponse({"ok": False, "message": PROBLEMS["confirm"]}, status_code=403)
        return JSONResponse(passkeys.registration_options(conn, site[0], challenges, device["id"], clock()),
                            headers=authn.NO_STORE)

    @app.post("/settings/passkeys/verify")
    async def passkey_add_verify(request: Request, conn=Depends(get_conn)):
        device, site = fresh(conn, request), site_of(request)
        if device is None or site is None:
            return JSONResponse({"ok": False}, status_code=404)
        if not devices.has_sudo(device, clock()):
            return JSONResponse({"ok": False, "message": PROBLEMS["confirm"]}, status_code=403)
        data = await body_json(request)
        label = str(data.get("label") or "") or device["label"]
        if not passkeys.register(conn, site[0], site[1], challenges, data.get("handle"), data.get("credential"),
                                 label, device["id"], clock()):
            return JSONResponse({"ok": False, "message": "That didn't work. Try again."}, status_code=422)
        log.info("passkey added")
        return JSONResponse({"ok": True}, headers=authn.NO_STORE)

    @app.post("/settings/passkeys/{passkey_id}/remove")
    async def passkey_remove(request: Request, passkey_id: int, conn=Depends(get_conn)):
        device = fresh(conn, request)
        if device is None:
            return back("nodevice")
        if not devices.has_sudo(device, clock()):
            return back("confirm")
        if passkeys.remove(conn, passkey_id):
            log.info("passkey removed")
            if not passkeys.count(conn):
                devices.passkeys_gone(conn)
        return back("passkey", "done")

    @app.post("/settings/devices/{device_id}/signout")
    async def device_signout(request: Request, device_id: str, conn=Depends(get_conn)):
        device = device_of(request)
        devices.revoke(conn, device_id)
        log.info("device signed out")
        if device is not None and device["id"] == device_id:
            response = RedirectResponse("/login", status_code=303, headers=authn.NO_STORE)
            authn.clear_session_cookie(response, secure=authn.is_https(request.scope))
            return response
        return back("device", "done")

    # ---- daily reminder ----

    @app.post("/settings/reminder")
    async def reminder_save(on: str = Form(""), at: str = Form("", alias="time"), conn=Depends(get_conn)):
        try:
            push.set_reminder(conn, on == "1", at)
        except ValueError:
            return back("time")
        return back("reminder", "done")

    @app.post("/push/subscribe")
    async def push_subscribe(request: Request, conn=Depends(get_conn)):
        device = device_of(request)
        data = await body_json(request)
        if app.state.vapid is None or device is None or not push.subscribe(conn, device["id"], data.get("endpoint"), clock()):
            return Response(status_code=422)
        return Response(status_code=204)

    @app.post("/push/unsubscribe")
    async def push_unsubscribe(request: Request, conn=Depends(get_conn)):
        device = device_of(request)
        if device is not None:
            push.unsubscribe(conn, device["id"])
        return Response(status_code=204)

    @app.post("/push/test")
    async def push_test(request: Request):
        device, vapid = device_of(request), app.state.vapid
        if device is None or vapid is None:
            return JSONResponse({"sent": 0}, status_code=422)

        def work() -> int:  # the connection is opened and used on the worker thread
            conn = db.connect(db_path)
            try:
                return push.send_all(conn, vapid, clock(), app.state.push_transport, device_id=device["id"])
            finally:
                conn.close()

        return JSONResponse({"sent": await run_in_threadpool(work)}, headers=authn.NO_STORE)
