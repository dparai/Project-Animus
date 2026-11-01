"""Check the repository before it is pushed anywhere: nothing private may be in it.

    python -m scripts.preflight_push --ip <VM_IP> --key-name <KEY_FILE_NAME> --email <OWNER_EMAIL> \
        --also <PRIVATE_DOMAIN>

Scans the tracked files, the whole git history (every branch, every commit, merge diffs included)
and the commit metadata (author and committer names and e-mails, commit messages) for:
  nvapi            a key that starts with the NVIDIA prefix
  vm-ip            the VM address given with --ip (repeatable)
  ssh-key-name     the key file name given with --key-name (repeatable)
  owner-email      the e-mail given with --email (repeatable); the LICENSE file is exempt
  music-domain     a string given with --also (repeatable), e.g. the live music site's domain
  env-secret       a value after the password-hash or session-secret variable names
  bcrypt-hash      a bcrypt hash
  binary-file      a tracked binary file (or one ever committed) outside app/static/
  private-path     a *.db / *.sqlite* / .env / private-data path that was ever committed

The strings to look for come from the command line so they are never written into the repo.
Output is "rule<TAB>path<TAB>commit" per hit and a count per rule. The matched text is never
printed. Commit metadata hits show the path "(commit-metadata)". Exit code 1 on any hit, 0 when
clean. "worktree" as commit means the current tracked file.
"""
import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

# Built from pieces so this file does not match its own rules.
_NVAPI = "nvapi" + "-"
_BCRYPT = re.compile(r"\$2[aby]\$" + r"\d{2}\$" + r"[./A-Za-z0-9]{53}")
_ENV_SECRET = re.compile(
    r"ANIMUS_(?:PASSWORD_HASH|SESSION_SECRET)\s*=\s*[^\s<{'\"$][^\s]{7,}"
)
_PRIVATE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3")
_LICENSE_NAMES = {"LICENSE", "LICENSE.md", "LICENSE.txt"}
_MARK = "@@COMMIT@@"
_META = "(commit-metadata)"
_STATIC_PREFIX = "app/static/"
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_SIMPLE_ESC = {"n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11, "\\": 92, '"': 34}
_RULES = ("nvapi", "vm-ip", "ssh-key-name", "owner-email", "music-domain", "env-secret",
          "bcrypt-hash", "binary-file", "private-path")


def _unquote(body: str) -> str:
    """Undo git's C-style path quoting (octal bytes and simple escapes)."""
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in "01234567":
            j = i + 1
            while j < len(body) and j < i + 4 and body[j] in "01234567":
                j += 1
            out.append(int(body[i + 1:j], 8) & 0xFF)
            i = j
        else:
            out.append(_SIMPLE_ESC.get(nxt, ord(nxt) & 0x7F))
            i += 2
    return out.decode("utf-8", "replace")


def _header_path(raw: str) -> str:
    """Path from a "diff --git a/P b/P" line (run with -M0, so both sides name the same path)."""
    rest = raw[len("diff --git "):]
    if rest.startswith('"'):
        match = _QUOTED.match(rest)
        path = _unquote(match.group(1)) if match else ""
        return path[2:] if path.startswith("a/") else path
    size = (len(rest) - 5) // 2
    return rest[2:2 + size] if size > 0 else ""


def _binary_ok(path: str) -> bool:
    return path.startswith(_STATIC_PREFIX)


def _private_path(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name.lower()
    if name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample"}):
        return True
    if name.endswith(_PRIVATE_SUFFIXES) or ".sqlite" in name:
        return True
    return "private-data" in p.parts


def _line_rules(line: str, path: str, strings: dict[str, list[str]]) -> set[str]:
    hits = set()
    if _NVAPI in line:
        hits.add("nvapi")
    if _BCRYPT.search(line):
        hits.add("bcrypt-hash")
    if _ENV_SECRET.search(line):
        hits.add("env-secret")
    lowered = line.lower()
    for needle in strings["vm-ip"]:
        if needle.lower() in lowered:
            hits.add("vm-ip")
    for needle in strings["ssh-key-name"]:
        if needle.lower() in lowered:
            hits.add("ssh-key-name")
    for needle in strings["music-domain"]:
        if needle.lower() in lowered:
            hits.add("music-domain")
    if PurePosixPath(path).name not in _LICENSE_NAMES:
        for needle in strings["owner-email"]:
            if needle.lower() in lowered:
                hits.add("owner-email")
    return hits


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off", *args],
                          capture_output=True, check=False)


def scan(repo, ips=(), key_names=(), emails=(), also=()) -> list[tuple[str, str, str]]:
    """Return sorted unique (rule, path, commit) hits. Never returns matched text."""
    repo = Path(repo)
    strings = {
        "vm-ip": [s for s in ips if s],
        "ssh-key-name": [s for s in key_names if s],
        "owner-email": [s for s in emails if s],
        "music-domain": [s for s in also if s],
    }
    hits: set[tuple[str, str, str]] = set()

    tracked = _git(repo, "ls-files", "-z")
    for rel in filter(None, tracked.stdout.decode("utf-8", "replace").split("\0")):
        if _private_path(rel):
            hits.add(("private-path", rel, "worktree"))
        try:
            data = (repo / rel).read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            if not _binary_ok(rel):
                hits.add(("binary-file", rel, "worktree"))
            continue
        for line in data.decode("utf-8", "replace").splitlines():
            for rule in _line_rules(line, rel, strings):
                hits.add((rule, rel, "worktree"))

    # Commit metadata: names, e-mails and messages of every commit on every branch.
    meta = _git(repo, "log", "--all", "-z", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B")
    for record in meta.stdout.decode("utf-8", "replace").split("\0"):
        parts = record.lstrip("\n").split("\x1f", 5)
        if len(parts) < 6:
            continue
        commit = parts[0]
        for line in "\n".join(parts[1:]).split("\n"):
            for rule in _line_rules(line, _META, strings):
                hits.add((rule, _META, commit))

    # Diffs of every commit; -m also diffs merge commits against each of their parents.
    log = _git(repo, "log", "--all", "-p", "-m", "--no-color", "--no-ext-diff", "-M0",
               f"--format={_MARK}%H")
    commit, path = "", ""
    for raw in log.stdout.decode("utf-8", "replace").split("\n"):
        raw = raw.rstrip("\r")
        if raw.startswith(_MARK):
            commit, path = raw[len(_MARK):], ""
            continue
        if raw.startswith("diff --git "):
            path = _header_path(raw)
            if path and _private_path(path):
                hits.add(("private-path", path, commit))
            continue
        if raw.startswith("Binary files ") and raw.endswith(" differ"):
            if path and not _binary_ok(path):
                hits.add(("binary-file", path, commit))
            continue
        if raw[:1] in ("+", "-") and not raw.startswith(("+++", "---")):
            for rule in _line_rules(raw[1:], path, strings):
                hits.add((rule, path, commit))
    return sorted(hits)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan tracked files and git history for private data.")
    parser.add_argument("--repo", default=".", help="repository to scan (default: current directory)")
    parser.add_argument("--ip", action="append", default=[], help="VM address to look for")
    parser.add_argument("--key-name", action="append", default=[], help="SSH key file name to look for")
    parser.add_argument("--email", action="append", default=[], help="owner e-mail to look for")
    parser.add_argument("--also", action="append", default=[],
                        help="extra private string to look for (reported as music-domain)")
    args = parser.parse_args(argv)

    found = scan(args.repo, ips=args.ip, key_names=args.key_name, emails=args.email, also=args.also)
    for rule, path, commit in found:
        print(f"{rule}\t{path}\t{commit[:12]}")
    counts = Counter(rule for rule, _path, _commit in found)
    for rule in _RULES:
        print(f"count {rule}: {counts.get(rule, 0)}")
    if found:
        print("FAIL: private data found (matched text is not shown).")
        return 1
    print("OK: nothing found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
