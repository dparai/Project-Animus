"""Create the key the server signs daily reminders with. Run it once on the VM:

    sudo -u animus bash -c "cd /opt/animus && .venv/bin/python -m scripts.make_push_key"

Writes ANIMUS_VAPID_PRIVATE_KEY into /opt/animus/.env (other lines and mode 600 are kept), then
restart the animus service. An existing key is kept unless --replace is given: replacing it means
every phone has to turn the reminder on again. Prints only what it did, never the key.
"""
import base64
import os
import sys
import tempfile
from pathlib import Path

from scripts.set_password import DEFAULT_ENV, _key

KEY = "ANIMUS_VAPID_PRIVATE_KEY"


def new_key() -> str:
    from cryptography.hazmat.primitives.asymmetric import ec

    number = ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value
    return base64.urlsafe_b64encode(number.to_bytes(32, "big")).rstrip(b"=").decode("ascii")


def update_env(path: str, replace: bool = False) -> bool:
    """True when a key was written, False when one was already there and kept."""
    target = Path(path)
    existing = target.exists()
    lines = target.read_text(encoding="utf-8").splitlines() if existing else []
    if any(_key(line) == KEY for line in lines) and not replace:
        return False
    kept = [line for line in lines if _key(line) != KEY]
    kept.append(f"{KEY}={new_key()}")
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".env.", suffix=".tmp")  # mode 600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(kept) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if existing:
            st = target.stat()
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True


def main(argv=None, env_path: str = DEFAULT_ENV, out=print) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if update_env(env_path, replace="--replace" in argv):
        out("Reminder key saved. Restart the app.")
    else:
        out("A reminder key already exists. Nothing changed (use --replace to make a new one).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
