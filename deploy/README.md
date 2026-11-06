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

Pages: `/ask` (Ask My Journal) and `/counselor` (chat). Both need `LLM_API_KEY` in `.env`; without it they show a calm "no API key" message and nothing breaks.

What goes to your LLM provider:

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

## Off-site copies on Google Drive

Every night at 04:00 (after the 03:30 backup) the `animus-offsite` job makes two copies in your Google Drive, using `rclone`:

- **Readable:** your journal entries only (no reflections, chats or memory notes), one Google Doc per month, named `Animus Journal YYYY-MM`, in the Drive folder `Animus Journal`. The first run writes every month; later runs only refresh the current and previous month.
- **Encrypted:** the VM's nightly database backups, mirrored to the Drive folder `Animus Backup` and encrypted before they leave the VM (file names too). Drive keeps the same 14 daily + 8 weekly copies as the VM. The job refuses to delete more than 5 files in one run, and refuses to sync at all if the VM has no backups.

### One-time setup (you do this; the assistant never sees the token or passphrase)

1. On your PC (with a browser), install rclone and run `rclone authorize "drive"`. Sign in to Google when asked. It prints a token blob; copy it.
2. On the VM, create the config folder and start the wizard as the `animus` user:

        sudo install -d -m 700 -o animus -g animus /opt/animus/rclone
        sudo -u animus RCLONE_CONFIG=/opt/animus/rclone/rclone.conf rclone config

3. `n` (new remote), name `gdrive`, type `drive`. Leave client id and secret empty. Scope: choose `drive.file` (access only to files rclone itself creates, nothing else in your Drive). Answer `n` to "Use web browser to automatically authenticate" and paste the token from step 1 when asked. Not a Shared Drive.
4. `n` again, name `gdrive-crypt`, type `crypt`. Remote to encrypt: `gdrive:Animus Backup`. Filename encryption: `standard`. Directory name encryption: `true`. Password: choose `y` (type in your own) and enter a long passphrase of your own; do the same for the second (salt) password. Quit the wizard.
5. Lock the config file and check it:

        sudo chown animus:animus /opt/animus/rclone/rclone.conf
        sudo chmod 600 /opt/animus/rclone/rclone.conf
        sudo -u animus RCLONE_CONFIG=/opt/animus/rclone/rclone.conf rclone lsd gdrive:

   The config holds your Drive access and the (obscured) encryption passwords. Never share it, paste it anywhere, or commit it.

**Store the passphrase and the salt password in your password manager now.** Without them the encrypted backups cannot be read by anyone, including you, and the config file on the VM will not help if the VM is lost.

### Install the timer

    sudo cp /opt/animus/deploy/animus-offsite.service /opt/animus/deploy/animus-offsite.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now animus-offsite.timer
    sudo systemctl start animus-offsite.service      # first run now (every month is exported)
    systemctl list-timers animus-offsite.timer
    journalctl -u animus-offsite -n 5 --no-pager

The log shows `docs_uploaded=N` and `backups_synced`, or a reason code: `rclone_missing`, `docs_failed`, `backup_sync_failed`, `backup_dir_empty`, `unexpected`. It never contains entry text. If one half fails the other still runs and the service ends with an error. The unit has network access (IPv4/IPv6) but no other privileges; it reads `backups/` read-only and writes only the rclone config (token refresh) and a small state file, `data/offsite_state.json`, which records that the first full export is done. It does not read `.env`. Defaults can be overridden with `ANIMUS_RCLONE_DOCS_REMOTE` (`gdrive:Animus Journal`) and `ANIMUS_RCLONE_BACKUP_REMOTE` (`gdrive-crypt:`).

### Check for duplicate Docs after the first authorised run (controller step)

The readable copy relies on rclone's HTML import (`--drive-import-formats html --drive-export-formats html`; rclone refuses the upload unless the export format matches), which converts each file to a Google Doc and should update the same Doc on the next run. After the owner has authorised Drive, run `animus-offsite.service` a second time and list the folder (`rclone lsl "gdrive:Animus Journal"`, as `animus` with `RCLONE_CONFIG` set). There must be exactly one Doc per month. If duplicates appear, switch the readable copy to plain `.md` files uploaded without import conversion (still readable in Drive's previewer and any editor) and delete the duplicates in Drive by hand.

### Restore from the encrypted copy

1. List and fetch the backup you want; decryption happens through the crypt remote (run as `animus` with `RCLONE_CONFIG` set):

        rclone ls gdrive-crypt:
        rclone copy gdrive-crypt:animus-<YYYYMMDD-HHMMSS>.db <RESTORE_FOLDER>/

   On a new machine, recreate the two remotes with the same Drive login and **the same passphrase and salt password**, then run the same commands.
2. Check the copy: `sqlite3 <RESTORE_FOLDER>/animus-<YYYYMMDD-HHMMSS>.db "select count(*) from entries"`.
3. Stop the app (`sudo systemctl stop animus`), keep the old file (`mv data/vault.db data/vault.db.old`), put the copy at `/opt/animus/data/vault.db` (owner `animus`, mode 600), delete any stale `vault.db-wal` / `vault.db-shm`, then `sudo systemctl start animus`.
