import os
import subprocess
from pathlib import Path

from scripts import preflight_push

REPO = Path(__file__).resolve().parent.parent

FAKE_IP = "203.0.113.77"
FAKE_KEY = "fake_test_key_file"
FAKE_MAIL = "someone@example.test"
FAKE_NVAPI = "nvapi" + "-FAKEFAKEFAKEFAKEFAKE"
FAKE_BCRYPT = "$2b$" + "12$" + "a" * 53
FAKE_PRIVATE = "SOMEVALUE12345"
ENV_NAME = "ANIMUS_SESSION" + "_SECRET"


def git(repo, *args, **identity):
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
        "PATH": os.environ["PATH"], "HOME": str(repo),
    }
    env.update(identity)
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)


def init(repo):
    git(repo, "init", "-q")
    git(repo, "config", "commit.gpgsign", "false")


def _run(tmp_path, capsys, *extra):
    code = preflight_push.main(
        ["--repo", str(tmp_path), "--ip", FAKE_IP, "--key-name", FAKE_KEY, "--email", FAKE_MAIL, *extra]
    )
    return code, capsys.readouterr().out


def _hits(out):
    return {tuple(line.split("\t")[:2]) for line in out.splitlines() if line.count("\t") == 2}


def test_preflight_flags_nvapi_ip_hash_env_db_in_history_without_printing_values(tmp_path, capsys):
    init(tmp_path)
    (tmp_path / "notes.md").write_text(f"ssh -i {FAKE_KEY} ubuntu@{FAKE_IP}\nmail {FAKE_MAIL}\n")
    (tmp_path / "LICENSE").write_text(f"Copyright {FAKE_MAIL}\n")
    (tmp_path / "conf.txt").write_text(
        f"key={FAKE_NVAPI}\nhash {FAKE_BCRYPT}\n{ENV_NAME}={FAKE_PRIVATE}\n"
    )
    (tmp_path / ".env").write_text("X=1\n")
    (tmp_path / "vault.db").write_bytes(b"SQLite format 3\0")
    (tmp_path / "private-data").mkdir()
    (tmp_path / "private-data" / "export.json").write_text("{}")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "one")
    # Remove everything in a later commit: the history must still be flagged.
    git(tmp_path, "rm", "-q", "-r", "notes.md", "conf.txt", ".env", "vault.db", "private-data")
    git(tmp_path, "commit", "-q", "-m", "two")
    first = git(tmp_path, "rev-list", "--max-parents=0", "HEAD").stdout.strip()

    code, out = _run(tmp_path, capsys)
    assert code == 1
    rules = {line.split("\t")[0] for line in out.splitlines() if "\t" in line}
    assert rules == {"nvapi", "vm-ip", "ssh-key-name", "owner-email", "env-secret", "bcrypt-hash",
                     "private-path", "binary-file"}
    assert first[:12] in out
    assert f"binary-file\tvault.db\t{first[:12]}" in out
    assert "notes.md" in out and "conf.txt" in out
    # LICENSE is exempt for the e-mail rule.
    assert not any(line.startswith("owner-email\tLICENSE") for line in out.splitlines())
    for secret in (FAKE_IP, FAKE_KEY, FAKE_MAIL, FAKE_NVAPI, FAKE_BCRYPT, FAKE_PRIVATE):
        assert secret not in out


def test_preflight_clean_repo_passes(tmp_path, capsys):
    init(tmp_path)
    (tmp_path / "README.md").write_text("Nothing private here. Use <VM_IP> and <SSH_KEY>.\n")
    (tmp_path / "app.py").write_text("ANIMUS_PASSWORD_HASH=<bcrypt hash>\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "clean")
    code, out = _run(tmp_path, capsys, "--also", "music.example.test")
    assert code == 0
    assert "OK" in out


def test_preflight_scans_commit_metadata_without_printing_it(tmp_path, capsys):
    init(tmp_path)
    (tmp_path / "a.txt").write_text("clean\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", f"deploy to {FAKE_IP}",
        GIT_AUTHOR_EMAIL=FAKE_MAIL, GIT_COMMITTER_NAME="Name " + FAKE_KEY)
    commit = git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert {("owner-email", "(commit-metadata)"), ("vm-ip", "(commit-metadata)"),
            ("ssh-key-name", "(commit-metadata)")} <= _hits(out)
    assert commit[:12] in out
    for secret in (FAKE_IP, FAKE_KEY, FAKE_MAIL):
        assert secret not in out


def test_preflight_covers_merge_commit_diffs(tmp_path, capsys):
    init(tmp_path)
    git(tmp_path, "checkout", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("one\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "side")
    (tmp_path / "b.txt").write_text("side\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "side work")
    git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "c.txt").write_text("main\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "main work")
    git(tmp_path, "merge", "-q", "--no-ff", "--no-commit", "side")
    (tmp_path / "evil.txt").write_text(f"host {FAKE_IP}\n")  # only the merge commit adds it
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "merge")
    merge = git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    assert len(git(tmp_path, "rev-list", "--parents", "-n1", "HEAD").stdout.split()) == 3
    git(tmp_path, "rm", "-q", "evil.txt")
    git(tmp_path, "commit", "-q", "-m", "remove")
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert f"vm-ip\tevil.txt\t{merge[:12]}" in out
    assert FAKE_IP not in out


def test_preflight_handles_non_ascii_and_quoted_paths(tmp_path, capsys):
    init(tmp_path)
    names = ["café notes.md", "日本語.txt"]
    if os.name != "nt":
        names.append('we"ird.txt')
    for name in names:
        (tmp_path / name).write_text(f"ip {FAKE_IP}\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "paths")
    first = git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    code, out = _run(tmp_path, capsys)
    assert code == 1
    for name in names:
        assert f"vm-ip\t{name}\t{first[:12]}" in out
        assert f"vm-ip\t{name}\tworktree" in out
    assert FAKE_IP not in out


def test_preflight_flags_binary_files_except_under_static(tmp_path, capsys):
    init(tmp_path)
    (tmp_path / "app" / "static").mkdir(parents=True)
    (tmp_path / "app" / "static" / "font.woff2").write_bytes(b"wOF2\0\1\2")
    (tmp_path / "photo.png").write_bytes(b"\x89PNG\0\0")
    (tmp_path / "doc.txt").write_text("plain\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "bin")
    commit = git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    code, out = _run(tmp_path, capsys)
    assert code == 1
    assert _hits(out) == {("binary-file", "photo.png")}
    assert f"binary-file\tphoto.png\t{commit[:12]}" in out
    assert "count binary-file: 2" in out


def test_preflight_also_flags_extra_strings_as_music_domain(tmp_path, capsys):
    init(tmp_path)
    (tmp_path / "x.md").write_text("see Music.Example.Test for songs\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "x")
    code, out = _run(tmp_path, capsys)
    assert code == 0  # not looked for unless asked
    code, out = _run(tmp_path, capsys, "--also", "music.example.test", "--also", "")
    assert code == 1
    assert ("music-domain", "x.md") in _hits(out)
    assert "music.example.test" not in out.lower()


def test_gitattributes_keeps_crlf_files_crlf():
    lines = (REPO / ".gitattributes").read_text().splitlines()
    assert "* text=auto" in lines
    crlf = {ln.split()[0] for ln in lines if ln.endswith("text eol=crlf")}
    assert {"app/templates/base.html", "app/llm.py"} <= crlf
    for rel in crlf:
        data = (REPO / rel).read_bytes()
        assert b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n"), rel
    listing = subprocess.run(["git", "-C", str(REPO), "ls-files", "app", "scripts", "deploy"], capture_output=True, text=True)
    if listing.returncode != 0:
        return  # not a git checkout
    for rel in listing.stdout.split():
        if rel in crlf or not rel.endswith((".py", ".html", ".md", ".service", ".timer", ".txt", ".ps1")):
            continue
        assert b"\r\n" not in (REPO / rel).read_bytes(), rel
