# CI/CD: push to main, the server updates itself

```
push to main ──► GitHub Actions CI (.github/workflows/ci.yml)
 (git push or        gateway pytest, api/ingester syntax, dashboard JS syntax,
  edit on GitHub)    docker compose config, image build
                         │ green
                         ▼
Oracle server forex-trader, timer bhm-deploy.timer (every minute)
  ~/bhm/bin/bhm-deploy.sh: git fetch main into ~/bhm/repo
    → new commit and CI green? → copy repo/server-dev/ → ~/bhm/server-dev/
    → rebuild only the changed services → check the dashboard answers
```

| Changed file | What the server does |
|---|---|
| `dashboard.html`, `api/*` | `docker compose up -d --build dashboard` |
| `ingester/*` | `up -d --build ingester` |
| `mosquitto/*` | `restart mosquitto` |
| `docker-compose.yml` | `up -d --build` (all services) |
| `db/*`, scripts | nothing (db/ only runs on a fresh database) |

The server's own files are never touched: `.env`, `secrets/`, `certs/`, `mosquitto/passwd`, `backup-*/`.
Every deploy saves the files it replaces in `~/bhm/server-dev/backup-auto-<time>-<old commit>/`
(the 10 newest are kept).

CI red ⇒ the server stays on the last good version. A commit with no CI result after 15 min deploys anyway.

## On the server

```bash
journalctl --user -u bhm-deploy -n 50 --no-pager   # what got deployed / why not
~/bhm/bin/bhm-deploy.sh --status                   # deployed commit vs. main
~/bhm/bin/bhm-deploy.sh --force                    # deploy main now, skip the CI check
systemctl --user stop bhm-deploy.timer             # pause auto-deploy
```

Roll back: push a revert commit (`git revert <sha>`), or copy a `backup-auto-*` folder back and run
`sudo docker compose -p bhm up -d --build dashboard`.

## Install (once)

```bash
git clone https://github.com/Immanueldavidckp/bhm.git ~/bhm/repo
~/bhm/repo/server-dev/deploy/install.sh
```

The first run only records the current main as the baseline. The timer runs the installed copy
`~/bhm/bin/bhm-deploy.sh`, not the one in the repo, so a push cannot change what runs with sudo. After a
reviewed change to `deploy/`, re-run `install.sh` (the deploy log reminds you).
