# Animus design overview

Animus is one FastAPI process serving server-rendered Jinja2 pages with HTMX for partial updates, backed by a single SQLite file. It is built for one owner on one small server.

## Modules

| Module | Role |
| --- | --- |
| `app/main.py` | app factory, routes, middleware wiring, scheduler start |
| `app/config.py` | environment settings (database path, allowed hosts, NVIDIA endpoint and models) |
| `app/db.py` | schema, connection (WAL, foreign keys), small in-place migrations |
| `app/auth.py`, `app/limits.py` | bcrypt login, signed session cookie, in-memory login rate limit |
| `app/csrf.py`, `app/security.py`, `app/headers.py` | CSRF, cross-site guard, CSP and other headers, request body limit |
| `app/entries.py`, `app/moods.py`, `app/stats.py`, `app/dashboard.py`, `app/mood_streak.py` | entries CRUD, mood and energy, statistics, dashboard |
| `app/periods.py`, `app/tz.py` | week and month boundaries in the owner's time zone |
| `app/search.py` | full-text search over entries, with date and mood hints parsed from a question |
| `app/prompts.py` | every piece of wording and output schema sent to the model |
| `app/llm.py` | OpenAI-compatible client with a model priority list and typed failure reasons |
| `app/reflect.py`, `app/reflections.py`, `app/jobs.py` | weekly and monthly reflections, quote verification, persistent job queue and scheduler |
| `app/focus.py`, `app/memory.py` | Focus Areas, AI memory notes and corrections |
| `app/ask.py` | Ask My Journal |
| `app/counselor.py`, `app/chat_memory.py` | Counselor chat and its memory summaries |
| `scripts/` | set password, backup, move database, preflight scan, optional importer |

## Data model (SQLite)

| Table | Holds |
| --- | --- |
| `entries` | `created_at`, `local_date`, `text`, optional `mood` (good, okay, rough, low, sad), optional `energy_score` 1-10, import `source_id` |
| `entries_fts` | FTS5 index over entry text, kept in sync by triggers |
| `settings` | key/value (for example the time zone) |
| `reflections` | weekly or monthly reflection as JSON content, with version and `is_latest` (regeneration keeps history) |
| `reflection_jobs` | one job per kind and period: status, attempts, next attempt, error code |
| `focus_areas` | focus items with status active, done or failing, linked to the reflection that proposed them |
| `ai_memory` | notes and corrections; soft-deleted with `deleted_at`; may link to a reflection claim or a conversation |
| `conversations`, `messages` | Counselor chats and their messages, with the entry ids each reply cited |

## Request flow

Middleware, outermost first: trusted-host check, request body limit (400 KB), security headers and CSP nonce, cross-site Origin guard, authentication (signed session cookie), CSRF token check. The route then opens a short-lived SQLite connection, does its work, and renders a full page or an HTMX fragment. Network calls to the model happen outside database transactions.

## Security model

- One owner. The password is hashed with bcrypt (the input is first normalised and SHA-256 hashed so any length fits). Every login check does constant work, and a missing configuration fails closed with 503.
- The session is a signed, `HttpOnly`, `SameSite=Lax` cookie that is `Secure` over HTTPS and valid for 24 hours. Changing the password rotates the secret and ends all sessions.
- Five failed logins from one client address block it for 15 minutes (in memory, bounded).
- Every unsafe request needs the CSRF token from the session, sent by HTMX as `X-CSRF-Token` or by forms as a hidden field. An Origin / `Sec-Fetch-Site` guard sits in front as a second layer.
- CSP: same-origin only, inline scripts only with a per-request nonce, no `unsafe-eval`. No third-party hosts are contacted from the browser.
- User text placed in prompts is escaped so it cannot close or open the prompt's markup, and model output is treated as untrusted data.
- Logs and stored error fields hold counts, ids and reason codes, never journal text or model output.
- The systemd unit runs as an unprivileged user with a sandbox that can write only the data directory (see `deploy/`).

## Reflections

A scheduler loop (every 5 minutes, in the app process) creates a pending job for the latest completed week and month when none exists. A job reads that period's entries, asks the model for a JSON reflection (wins, tough love, focus suggestions, monthly patterns), verifies it, and stores a new version. Failures retry quickly at first, then hourly, and only after about a day of outage does a job become `failed`; a missing API key delays the job instead of failing it. Jobs left running by a crash go back to pending at start-up. "Generate now" on the page queues the same job.

## Quote verification

The model must cite an entry id and a short verbatim quote for each claim. `reflect._match_quote` normalises whitespace, quotes and case and checks that the quote is really a substring of that entry's text (very short quotes must equal the whole entry). A claim whose quote or entry does not check out is dropped; if too much is dropped the call is retried. Ask applies the same check to its citations and shows the entry's real date. The Counselor additionally drops sentences that state dates or numbers not found in the entries it was given.

## Ask My Journal

The question is turned into a full-text query, with month, year and mood hints used to narrow the search. The best 8 entries go to the model with a strict instruction to answer only from them. If the answer finds nothing, the app can retry once, escalating to a stronger model, within a time budget. If the journal has no answer it says "The journal does not mention this." Nothing is stored.

## Counselor and its memory

Each reply is built from the recent messages, a few entries selected from the latest messages, and the active memory notes. When a chat is ended, or idle for 6 hours, its text is summarised into at most 3 short notes. Above 60 notes, the oldest 30 are merged into at most 10; corrections are never merged. Every note can be deleted on the Memory page. Reflection corrections also become notes and are included in later reflections.

## Backups

`scripts/backup.py` copies the live database with the SQLite backup API (consistent while the app writes), checks integrity, and keeps the newest copy of each of the last 14 days and 8 weeks, never deleting the newest. A systemd timer runs it daily in a sandbox without network access. Copy backups off the server regularly; a backup on the same disk is not a disaster plan.
