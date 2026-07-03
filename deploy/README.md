# Deploying Tally

Three ways to keep Tally running. All of them serve on 127.0.0.1:8787 only; put your
own VPN or reverse proxy in front if you want remote access.

## macOS (launchd)

From the repo root:

```sh
tools/install_launchd.sh
```

This installs two user LaunchAgents:

- `com.tally.server`: runs the server, starts at login, restarts if it dies.
- `com.tally.sync`: runs the Plaid sync once a day at 08:00.

Logs land in `backend/data/logs/`. Re-run the script after moving the checkout or
updating uv. To remove: `launchctl unload ~/Library/LaunchAgents/com.tally.*.plist`
and delete the plists.

## Linux (systemd)

Example units live in this directory. Edit the paths and user in each file first
(they assume the repo at `/opt/tally`, uv at `/usr/local/bin/uv`, and a `tally` user).

```sh
sudo cp deploy/tally.service deploy/tally-sync.service deploy/tally-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tally
sudo systemctl enable --now tally-sync.timer
```

## Docker

Build from the repo root and publish the port bound to loopback only:

```sh
docker build -f deploy/Dockerfile -t tally .
docker run -d --name tally -p 127.0.0.1:8787:8787 -v tally-data:/app/backend/data tally
```

The volume holds the database; without it your data disappears with the container.
The image includes poppler and tesseract for PDF OCR on Linux.

## Backups

```sh
tools/backup.sh
```

Writes a consistent snapshot to `backend/data/backups/` using `VACUUM INTO`, which is
safe while the server is running. To restore: stop the server, copy the backup file
over `backend/data/tally.db` (also delete any `tally.db-wal` and `tally.db-shm` files
next to it), then start the server again.
