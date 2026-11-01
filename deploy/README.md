# Animus deployment

Service `animus`, listening on `127.0.0.1:8100` only.

## Install

    sudo cp /opt/animus/deploy/animus.service /etc/systemd/system/animus.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now animus

Caps: `MemoryMax=1536M`, `CPUQuota=100%`, `CPUWeight=50`, `IOWeight=50`. The unit also sets `UMask=0077` and the systemd sandbox directives (ProtectHome, PrivateDevices, Protect*, Restrict*, empty CapabilityBoundingSet); keep `data/vault.db*` at mode 600, `data/` and `backups/` at 700 and `/opt/animus` at 750. Config lives in `/opt/animus/.env` (edit it yourself).

## Open it from your PC or phone-sized browser (SSH tunnel)

    ssh -i <PATH_TO_SSH_KEY> -L 8100:127.0.0.1:8100 <SSH_USER>@<VM_IP>

Then browse http://localhost:8100 (use the browser's narrow-width view for the mobile look).

## Measured against the caps

After loading `/` and `/entries` (nothing saved):

    MemoryPeak=36118528 (about 34 MiB)
    MemoryMax=1610612736 (1536 MiB)
    CPUUsageNSec=510885000

## Check

    systemctl is-active animus
    ss -ltn | grep 8100     # must show 127.0.0.1 only
    journalctl -u animus -n 30 --no-pager

## Phase 3: Ask and Counselor

Pages: `/ask` (Ask My Journal) and `/counselor` (chat). Both need the NVIDIA key in `.env`; without it they show a calm "no API key" message and nothing breaks.

What goes to NVIDIA:

- Ask: your question plus the best 8 matching entries (text, date, mood, energy). Nothing is stored; the answer is shown once.
- Counselor: your recent chat messages (last 20), your 4 most relevant entries (chosen from your last 3 messages), and your memory notes. The chat itself is stored in the local database.
- Memory summaries: after you end a chat (or it sits idle 6 hours) the chat text is sent once to write up to 3 short notes. They appear on the Memory page, where you can delete any of them. When more than 60 notes pile up, the oldest 30 are merged into at most 10. Corrections are never merged.

Limits: interactive calls wait at most 90 s per model (background reflections 240 s). Messages are capped at 2000 characters, questions at 500.

Check without reading any journal text (counts and codes only):

    journalctl -u animus -n 50 --no-pager | grep -E "WARNING|ERROR|fallback"
    sqlite3 /opt/animus/data/vault.db "select count(*) from conversations; select count(*) from messages; select count(*) from ai_memory where deleted_at is null;"

## Phase 4: data directory and backups

The database lives in `/opt/animus/data/vault.db` (the app creates `data/` mode 700 if it is missing). The service may now write only `/opt/animus/data`, set by `ReadWritePaths=` and `Environment=ANIMUS_DB_PATH=` in the unit. Without `ANIMUS_DB_PATH` the app uses `data/vault.db` next to the code.

### One-time move (restart only `animus`; touch nothing else)

Before anything else, the owner (or whoever controls the VM) must delete any old `ANIMUS_DB_PATH=` line from `/opt/animus/.env` by hand. `EnvironmentFile=` overrides `Environment=` in the unit, so a stale line there would keep the app on the old file. Check without printing anything; it must print `0` before the restart:

    sudo grep -c '^ANIMUS_DB_PATH=' /opt/animus/.env

Then:

    sudo systemctl stop animus
    sudo -u animus bash -c "cd /opt/animus && mkdir -p -m 700 data backups && .venv/bin/python -m scripts.move_db vault.db data/vault.db"
    sudo cp /etc/systemd/system/animus.service /etc/systemd/system/animus.service.pre-phase4
    sudo cp /opt/animus/deploy/animus.service /etc/systemd/system/animus.service
    sudo systemctl daemon-reload
    sudo systemctl start animus
    systemctl is-active animus

The `.pre-phase4` copy is the rollback point; take it before installing the new unit.

Check that the app really uses the new file: save one test entry in the app, then

    ls -l /opt/animus/data/vault.db-wal       # must exist (SQLite WAL of the new database)
    ls -l --time-style=full-iso /opt/animus/vault.db   # mtime must be the same as before the restart

The old `vault.db` must not change after the move; if its mtime moves, the app is still writing to it.

`move_db` copies with the SQLite backup API, checks integrity, compares the row count of every table, refuses to overwrite an existing target, and leaves the old `vault.db` where it is (it prints counts only). Keep the old file until you have used the app for a while; delete it yourself.

Rollback (the old file was never touched): `sudo systemctl stop animus`, restore the saved unit (`sudo cp /etc/systemd/system/animus.service.pre-phase4 /etc/systemd/system/animus.service`; not git history, which may not match what was installed), `sudo systemctl daemon-reload`, `sudo systemctl start animus`. After the rollback the app uses whatever `ANIMUS_DB_PATH` says (in the restored unit or in `.env`), or `vault.db` next to the code if it is unset; make sure that is the file you want. Changes made after the move live only in `data/vault.db`; copy them back first if you need them.

### Daily backup

    sudo cp /opt/animus/deploy/animus-backup.service /opt/animus/deploy/animus-backup.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now animus-backup.timer
    sudo systemctl start animus-backup.service      # first backup now
    systemctl list-timers animus-backup.timer
    journalctl -u animus-backup -n 5 --no-pager      # "backup ok kept=N pruned=M" or a reason code

The timer runs daily at 03:30 server time (random delay up to 10 minutes, catches up after downtime) as user `animus` with no network. Copies go to `/opt/animus/backups/animus-YYYYMMDD-HHMMSS.db` (mode 600, folder 700), taken with the SQLite backup API so they are consistent while the app writes. Retention: the newest copy of each of the last 14 days plus the newest of each of the last 8 weeks; the newest copy is never deleted. The backup unit also lists `data/` as writable only so SQLite can open the live WAL file; the script just reads it.

Reason codes in the log: `source_missing`, `out_dir_unwritable`, `write_failed`, `backup_failed`, `integrity_failed`, `unexpected`.

### Copy a backup to your PC (run it yourself, in PowerShell)

    .\scripts\pull-backup.ps1 -Dest <FOLDER_ON_EXTERNAL_DRIVE> -KeyPath <SSH_KEY> -HostName <VM_IP>

It fetches the newest backup with `scp` and prints the file name only. Test a restore occasionally: open the copy with `sqlite3 <file> "select count(*) from entries"`.

## HTTPS through Caddy (optional)

Do this only when you decide to expose Animus beyond an SSH tunnel. The app stays on `127.0.0.1:8100`; Caddy terminates TLS in front of it.

1. Tell the app which public name it may answer to (hosts only: no scheme, port, path or `*`; invalid entries are ignored with a warning). Add this line to `/opt/animus/.env`, then restart `animus`:

        ANIMUS_ALLOWED_HOSTS=<YOUR_DOMAIN>

   `localhost` and `127.0.0.1` stay allowed. The same list feeds the Host check and the cross-site Origin check.

2. Add a separate site file, `/etc/caddy/animus.caddy` (leave any existing site blocks alone; import the file from the main Caddyfile only after `caddy validate` passes):

        <YOUR_DOMAIN> {
            header Strict-Transport-Security "max-age=31536000"
            reverse_proxy 127.0.0.1:8100
        }

   Caddy does not send HSTS by default, so the header line is explicit. Start with the short `max-age` above; do not add `includeSubDomains` or `preload`.

3. Check with `caddy validate --config /etc/caddy/Caddyfile`, reload Caddy yourself, then confirm `curl -sI https://<YOUR_DOMAIN>/login` shows the HSTS header and that an unknown Host is answered with 400 or no certificate.
