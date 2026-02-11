Production Readiness Guide — BTC DCA

1) Environment and flags
- รัน `scripts/setup_production_env.sh` เพื่อสร้างไฟล์ `.env.production` ตัวอย่าง (จะไม่ทับไฟล์ที่มีอยู่)
- เติมค่า DB/API/LINE/ADMIN_TOKEN/APP_ENCRYPTION_KEY ให้ครบในไฟล์นี้ (ห้าม commit)
- รวมค่าแนะนำที่สำคัญ (ตั้งค่า 0 = production, 1 = test) เช่น `DRY_RUN=0`, `OKX_LIVE_ENABLED=1`, `FEATURE_S4_ENABLED=1`
- สามารถ symlink `.env.production` เป็น `.env` หรือใช้ export ผ่าน systemd unit/environment manager

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
