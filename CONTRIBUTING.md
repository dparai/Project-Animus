# Contributing

Issues and pull requests are welcome, but this is a personal project: there is no guarantee of a reply, a review or a merge.

- Keep changes small and simple. The app is deliberately one process, SQLite, and no front-end build step.
- Run the tests before opening a pull request: `pip install -r requirements.txt && pytest -q`.
- Never put real journal text, keys, hosts or personal data in issues, tests or fixtures.
- Security problems: see [SECURITY.md](SECURITY.md); do not open a public issue.
