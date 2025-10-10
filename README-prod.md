Production Readiness Guide — BTC DCA

1) Environment and flags
- Create `.env.production` (do not commit). Example keys:
  - DB_HOST, DB_USER, DB_PASSWORD, DB_NAME
  - BINANCE_API_KEY, BINANCE_API_SECRET
  - OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE, OKX_LIVE_ENABLED=1
  - ADMIN_TOKEN=<secure-random>
  - CORS_ORIGIN=https://your.domain.com
  - Optional: USE_BINANCE_TESTNET=0, DRY_RUN=0, STRATEGY_DRY_RUN=0

2) Set strategy flags (CDC and global exchange)
- Dry run example:
  - `venv/bin/python scripts/set_strategy_flags.py cdc=on exchange=okx`

3) Systemd services
- Copy unit files with your UNIX user substituted for `%i` or template to `systemd`:
  - `scripts/systemd/btc-dca-web.service`
  - `scripts/systemd/btc-dca-scheduler.service`
- Install:
  - `sudo cp scripts/systemd/btc-dca-*.service /etc/systemd/system/`
  - `sudo systemctl daemon-reload`
  - `sudo systemctl enable --now btc-dca-web.service btc-dca-scheduler.service`
- Logs write to: `web.out/web.err/scheduler.out/scheduler.err` (and `app.log`)

4) Nginx with WebSocket
- Use `scripts/nginx/btc-dca.conf.example`
- Ensure TLS certs present; reload Nginx

5) Log rotation
- Use `scripts/logrotate/btc-dca`:
  - `sudo cp scripts/logrotate/btc-dca /etc/logrotate.d/btc-dca`
  - Rotates at 10MB, keeps 7 versions, compresses

6) Operational checks
- Health: `curl -sSf https://your.domain.com/health`
- Web loads: `/` returns 200, Socket.IO connects
- APIs return JSON (no HTML errors) for `/api/*`

7) Safety
- Keep only one `main.py` (scheduler) process. The web app already prevents multiple instances.
- Prefer storing secrets in environment/secret managers; rotate keys after migration.

