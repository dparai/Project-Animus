# Security policy

## Reporting a vulnerability

Please report privately through GitHub security advisories: open the repository's Security tab and choose "Report a vulnerability". Do not open a public issue for a security problem. This is a personal project, so there is no guaranteed response time, but reports are read.

## Scope

Animus is a single-user application: one owner, one password, one SQLite database. In scope: authentication and session handling, CSRF, injection (SQL, template, HTML), file exposure, and anything that could leak journal content. Out of scope: multi-user isolation (there are no other users), attacks that need access to your server or `.env`, denial of service against a single-process app, and the behaviour of the third-party language-model API.

## Hardening advice for your own copy

- Use a strong, unique password (12 characters minimum is enforced; longer is better).
- Serve it only over HTTPS (for example behind Caddy) and keep the app itself on 127.0.0.1.
- Keep the server, Python and the dependencies updated.
- Keep `.env`, `data/` and `backups/` private (mode 600 / 700) and never commit them.

## Built-in measures

Failed logins are rate limited (5 failures from one client address block it for 15 minutes), and every state-changing request needs a CSRF token on top of SameSite cookies and an Origin check. Responses carry a strict Content-Security-Policy with per-request nonces and no third-party sources, plus nosniff, frame and referrer headers.
