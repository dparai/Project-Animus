"""Set the owner's login password. Run it yourself on the VM:

    sudo -u animus bash -c "cd /opt/animus && .venv/bin/python -m scripts.set_password"

Asks twice (nothing is echoed), needs at least 12 characters, then writes ANIMUS_PASSWORD_HASH
(bcrypt, cost 12) and a fresh ANIMUS_SESSION_SECRET into /opt/animus/.env, keeping every other line
and mode 600. A new secret logs out every session. Restart the animus service afterwards.
Prints only "Password saved." (or why nothing changed); never the password, hash or secret.
"""
import getpass as _getpass
import hmac
import os
import secrets
import sys
import tempfile
from pathlib import Path

from app import auth

DEFAULT_ENV = "/opt/animus/.env"
MIN_LENGTH = 12
KEYS = ("ANIMUS_PASSWORD_HASH", "ANIMUS_SESSION_SECRET")


def _key(line: str) -> str | None:
    text = line.strip()
    if not text or text.startswith("#") or "=" not in text:
        return None
    if text.startswith("export "):
        text = text[len("export "):]
    return text.split("=", 1)[0].strip()


def update_env(path: str, password_hash: bytes) -> None:
    """Replace the two auth lines, keep the rest, write atomically with mode 600."""
    target = Path(path)
    existing = target.exists()
    lines = target.read_text(encoding="utf-8").splitlines() if existing else []
    kept = [line for line in lines if _key(line) not in KEYS]
    # Single quotes: systemd's EnvironmentFile takes the value literally ($ in the hash).
    kept.append(f"ANIMUS_PASSWORD_HASH='{password_hash.decode('ascii')}'")
    kept.append(f"ANIMUS_SESSION_SECRET={secrets.token_urlsafe(48)}")
    data = "\n".join(kept) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".env.", suffix=".tmp")  # mode 600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if existing:
            st = target.stat()
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass  # same user already; only root could change it
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(env_path: str = DEFAULT_ENV, getpass=_getpass.getpass, out=print,
         cost: int = auth.BCRYPT_COST) -> int:
    if getpass is _getpass.getpass and not sys.stdin.isatty():
        # Piped input would bypass the no-echo prompt (and could sit in shell history or logs).
        out("Run this in a real terminal.")
        return 1
    first = getpass("New password: ")
    if len(first) < MIN_LENGTH:
        out(f"Use at least {MIN_LENGTH} characters. Nothing changed.")
        return 1
    if len(first) > auth.PASSWORD_MAX:
        out(f"Use at most {auth.PASSWORD_MAX} characters. Nothing changed.")
        return 1
    second = getpass("Same password again: ")
    if not hmac.compare_digest(first.encode("utf-8", "surrogatepass"), second.encode("utf-8", "surrogatepass")):
        out("The two passwords don't match. Nothing changed.")
        return 1
    update_env(env_path, auth.hash_password(first, cost=cost))
    out("Password saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
