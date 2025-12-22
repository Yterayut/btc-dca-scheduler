# Systemd (Web/Scheduler) + Secure MySQL Backup

This repo includes ready-to-use templates under `scripts/systemd/` and a secure backup script `scripts/backup_mysql.sh`.

## 1) Systemd services (journald)

Templates:
- `scripts/systemd/dca-web.service`
- `scripts/systemd/dca-scheduler.service`

Assumptions (edit the unit files if different):
- Project path: `/home/oneclimate-dev/yterayut-project/DCA`
- Python venv: `/home/oneclimate-dev/yterayut-project/DCA/venv/bin/python`
- Env file: `/home/oneclimate-dev/yterayut-project/DCA/.env`
- Linux user: `oneclimate-dev`

Install (system services):
```bash
sudo cp scripts/systemd/dca-web.service /etc/systemd/system/dca-web.service
sudo cp scripts/systemd/dca-scheduler.service /etc/systemd/system/dca-scheduler.service
sudo systemctl daemon-reload

sudo systemctl enable --now dca-web.service
sudo systemctl enable --now dca-scheduler.service
```

Check status/logs:
```bash
systemctl status dca-web.service -l
journalctl -u dca-web.service -f

systemctl status dca-scheduler.service -l
journalctl -u dca-scheduler.service -f
```

## 2) Secure MySQL backup (no password in process args)

### 2.1 Create `~/.my.cnf` (chmod 600)

```bash
cp scripts/mysql-backup.my.cnf.example ~/.my.cnf
chmod 600 ~/.my.cnf
$EDITOR ~/.my.cnf
```

### 2.2 Run backup manually (sanity check)

Option A: source `.env` so `DB_NAME` is available:
```bash
set -a; source /home/oneclimate-dev/yterayut-project/DCA/.env; set +a
/home/oneclimate-dev/yterayut-project/DCA/scripts/backup_mysql.sh
```

Option B: set `DB_NAME` explicitly:
```bash
DB_NAME=btc_dca /home/oneclimate-dev/yterayut-project/DCA/scripts/backup_mysql.sh
```

Defaults:
- Output dir: `~/backups/mysql` (override with `BACKUP_DIR`)
- Retention: 30 days (override with `BACKUP_KEEP_DAYS`)
- Credentials file: `~/.my.cnf` (override with `MYSQL_DEFAULTS_FILE`)

### 2.3 Schedule backup via systemd timer (recommended)

Templates:
- `scripts/systemd/dca-mysql-backup.service`
- `scripts/systemd/dca-mysql-backup.timer` (runs daily at 03:00, persistent)

Install:
```bash
sudo cp scripts/systemd/dca-mysql-backup.service /etc/systemd/system/dca-mysql-backup.service
sudo cp scripts/systemd/dca-mysql-backup.timer /etc/systemd/system/dca-mysql-backup.timer
sudo systemctl daemon-reload

sudo systemctl enable --now dca-mysql-backup.timer
```

Verify:
```bash
systemctl list-timers | grep -n dca-mysql-backup || true
systemctl status dca-mysql-backup.timer -l
journalctl -u dca-mysql-backup.service -n 200 --no-pager
```

## 3) Optional shell aliases (QoL)

Add to `~/.bashrc`:
```bash
alias dcaweb='journalctl -u dca-web.service -f'
alias dcasched='journalctl -u dca-scheduler.service -f'
alias dcahealth='curl -s http://127.0.0.1:5001/api/health | python3 -m json.tool'
```

