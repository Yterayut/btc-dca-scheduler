Project Memory — BTC DCA Dashboard

Updated: 2026-03-12

Summary
- Stack: Python 3 + Flask + Flask-SocketIO + MySQL + Binance SDK.
- Entrypoint: app.py (serves UI and JSON APIs on port 5001).
- Templates: templates/index.html (new UI), templates/admin.html.
- Scheduler/worker: main.py (trading engine, health server via env HEALTH_CHECK_PORT).
- Ops hardening (Dec-2025): scheduler DB lock + distributed dedupe + dedupe cleanup + S4 hardening gates + S4 OKX execution hardening.

2026-03-12 — S4 Observability Alignment (PRD rollout)

Done
- Implemented `docs/s4-observability-alignment-plan.md` Phase A/B/C in code.
- Added helper module `strategies/s4_observability.py`:
  - `parse_bool`, `normalize_reason_filter`, `derive_shadow_decision`
  - `next_unlock_from_gate_reason`, `mismatch_severity`
- `main.py` updates:
  - shadow gate now returns `next_unlock_condition` + `next_unlock_min_days`
  - runtime mismatch tracking: `analytics_runtime_mismatch`, `mismatch_streak_days`, `mismatch_severity`
  - throttled `notify_security_alert` for mismatch severity `warn/critical`
  - heartbeat metadata now includes mismatch + unlock diagnostics
- `app.py` updates:
  - `/s4` builder now exposes `signal_layers` (EOD vs runtime) + `why_not_flip`
  - `/api/s4_shadow_swaps` now supports filters:
    - `reason=all|heartbeat|plan`
    - `decision=ALL|HOLD|SWAP_TO_BTC|SWAP_TO_XAU`
    - `include_mismatch=true|false`
  - added `/api/s4_shadow_swaps_summary` (30/60/90-day operator summary)
- `templates/s4_status.html` updates:
  - new `Signal Layers` card with `MATCH/MISMATCH` badge
  - new `Why Not Flip` section (gate reason + unlock condition)
  - shadow table now shows decision/unlock/mismatch columns
- Test updates:
  - added `tests/test_s4_observability.py`
  - adjusted `tests/test_notify_holdings.py` assertion for CDC label variants
- Verification:
  - `py_compile` pass
  - targeted S4 tests pass
  - full suite pass: `84 passed`
- Runtime reload:
  - `systemctl restart` required root, so processes were reloaded via `pkill` and service auto-restart
  - confirmed new endpoints/UI live on port `5001`

To do (next)
- Observe shadow heartbeat daily for 90-day window and review `gate_*` blocking distribution.
- Add CSV export endpoint (or script) for `/api/s4_shadow_swaps_summary` for monthly ops reporting.
- Add integration tests for `/api/s4_shadow_swaps` filtering and `/s4` signal-layer rendering.
- Monitor mismatch alerts for noise; tune throttle intervals if alerts are too frequent.
- Keep execution unchanged: `S4_SWAP_EXEC_ENABLED=false`, run shadow-only until next decision review.

2026-03-12 (later) — Unlock Fallback Fix + Live Validation

Done
- Fixed `/s4` and `/api/s4_shadow_swaps` to compute `next_unlock_condition` and `next_unlock_min_days` even when old heartbeat rows do not contain these fields.
- Added fallback calculation from gate reason via `next_unlock_from_gate_reason(...)` in `app.py`.
- Live page now correctly shows:
  - `Why Not Flip: gate_cdc_up_required`
  - `Next Unlock: 3 day(s)`
  - `cdc_status must be up for 3 consecutive days`
- Verified shadow table unlock column now matches gate policy.
- Regression checks re-run successfully after fix:
  - targeted S4 tests: pass
  - full suite: `84 passed`

To do (next refinement)
- Decide mismatch severity policy for lagged analytics:
  - current behavior can escalate to `CRITICAL` at streak >= 5 even when `eod_lag_days > 0`.
  - consider cap (`INFO/WARN`) while lag > 0, and reserve `CRITICAL` for lag=0 divergence.
- After policy decision, update `mismatch_severity(...)` and add explicit unit tests for lag-aware escalation.

2026-03-13 — Mismatch Alert Noise Fix + Mobile Codex Session QoL

Done
- Fixed mismatch streak inflation bug in `main.py`:
  - old logic incremented streak every scheduler tick (~5m)
  - new logic counts by EOD date key only (`daily_eod` mode + `mismatch_last_counted_date`)
- Updated severity policy in `strategies/s4_observability.py`:
  - when `eod_lag_days > 0`, severity is capped to `INFO/WARN` (no `CRITICAL` from lagged snapshot mismatch)
  - `CRITICAL` reserved for sustained mismatch with fresh analytics (`lag=0`)
- Added/updated tests in `tests/test_s4_observability.py` for new lag-aware severity rules.
- Added streak debug visibility:
  - runtime now records `mismatch_streak_event`
  - `/s4` Signal Layers shows `Event: ...` (e.g., `mismatch_detected`, `match_recovered_reset`, etc.)
- Live validation after reload:
  - `/s4` now shows realistic streak (e.g., `1d`) instead of inflated values (`254d+`)
  - mismatch severity no longer escalates to false critical on lagged EOD snapshots
- Full regression pass after change: `85 passed`.

Mobile/SSH workflow improvement (operator QoL)
- Added `dca` function in `~/.bashrc`:
  - attaches existing tmux session `codex-dca` or creates new session in `~/yterayut-project/DCA` and starts `codex`
- Verified from mobile terminal: entering `dca` opens/restores Codex session successfully.

To do (next)
- Optionally refine `mismatch_streak_event` vocabulary for operator readability (map internal tokens -> friendly labels).
- Add integration test coverage for `/s4` rendering of `Event:` line and mismatch lifecycle transitions.
- Keep monitoring LINE security alerts for 1-2 weeks to confirm noise level is acceptable after lag-aware severity cap.

2025-12-22 — Ops Completion: Systemd + Heartbeat + S4 Status + Restore Drill

Highlights
- Systemd services (journald): `scripts/systemd/dca-web.service`, `dca-scheduler.service`, `dca-mysql-backup.service` + timer; enabled and running.
- Secure backups: `scripts/backup_mysql.sh` + `~/.my.cnf` (chmod 600) + daily timer; verified manual backup output to `~/backups/mysql`.
- Daily heartbeat: scheduler sends LINE 08:00–08:15 ICT, deduped by `action_dedupe` key `heartbeat:YYYY-MM-DD`, includes S4 status + gates + last flip + portfolio.
- Flex heartbeat: verified Flex delivery works for Daily Heartbeat (LINE Flex allowlist includes `heartbeat`).
- S4 CLI status: `venv/bin/python scripts/dca_tool.py s4 status` shows holdings, FIFO cost basis, per‑asset PnL, total PnL, gates, last status/error.
- S4 web page: `/s4` added with readable time formatting, color coding, auto‑refresh + manual refresh; shows per‑asset PnL and confirm progress (Day X/Y).
- S4 error hygiene: `last_error` cleared on successful/NOOP ticks; HOLD reasons stored separately (no stale error noise).
- Confirm progress: now counts consecutive daily signal streak (capped by confirm days) for accurate “X / Y” display.
- Restore drill: created `btc_dca_test`, restored latest gzip backup, verified `purchase_history` count 75 vs 75, dropped test DB.

Ops helpers
- Aliases in `~/.bashrc`: `dcaweb`, `dcasched`, `dcastatus`, `dcahealth`, `dcas4`, `dcabackup`.
- Restore script: `scripts/restore_drill.sh` auto‑creates test DB, restores latest backup, compares counts, drops test DB.

Git
- Commit: `fb1360c` “Release v1.0: S4 Hardening, Dashboard, Systemd, Backup verified”
- Tag: `v1.0-stable` pushed to `origin` (GitHub `Yterayut/btc-dca-scheduler`)

Guideline — Daily Monitoring Window (S4/OKX)
- Do not change scheduler config: the loop ticks every ~5 minutes and naturally picks up new daily candles when data updates.
- Daily close anchor: crypto 1D close at 00:00 UTC (07:00 ICT).
- Recommended human check window: 07:15–08:00 ICT (feed settled + bot ticked).
- Heartbeat 08:00–08:15 ICT is the official daily summary (confirm/flip status stabilized).

2025-12-22 — Restore Drill (Backup Verification)

What we did
- Ran full restore drill from latest gzip backup into `btc_dca_test`.
- Verified `purchase_history` row counts match live DB (75 vs 75).
- Dropped test DB after verification.

2025-12-18 — Ops/Idempotency Hardening (Phase 0/1/1.1)

Highlights
- Phase 0: เพิ่ม endpoint diagnostics แบบ read-only `GET /api/health` ใน `app.py` (JSON เท่านั้น) คืน PID/uptime, DB status, scheduler pid/health, และ flags สำคัญ (dry_run/testnet).
- Phase 0: ปรับ `scripts/dca_tool.py`:
  - `scheduler status --verbose` แสดง pid/alive/health/flags
  - เตือนชัดเจนเมื่อ start ในโหมด LIVE (`DRY_RUN=0`)
- Phase 1: เพิ่ม scheduler single-instance lock ระดับ DB ใน `main.py` ผ่าน MySQL `GET_LOCK` (เปิดใช้ด้วย `SCHEDULER_DB_LOCK_ENABLED=1`).
- Phase 1: เพิ่ม distributed action idempotency table `action_dedupe` (เปิดใช้ด้วย `DB_DEDUPE_ENABLED=1`):
  - ทุก action path ที่มี side-effect (weekly DCA buy/skip, reserve buy, half-sell) จะ `claim_dedupe_key` ที่ DB ก่อนทำจริง
  - ถ้า key ซ้ำจะ skip และ log `DB dedupe hit...`
- Phase 1.1: เพิ่ม cleanup job ลบ `action_dedupe` ที่เก่ากว่า N วัน (default 30 วัน) แบบ best-effort (เปิดด้วย `DEDUPE_CLEANUP_ENABLED=1` และตั้ง `DEDUPE_CLEANUP_DAYS=30`).

Operational notes / incidents resolved
- พบ scheduler start ใหม่ crash ทันทีจาก type hint `MySQLdb.connection` → แก้โดยเปลี่ยน type hint ให้ไม่อ้าง `MySQLdb.connection` (ป้องกัน import-time crash).
- เคลียร์ปัญหา “scheduler รันซ้ำ” ใน production: พบ `main.py` เก่า (PID 3763587) ยังรันอยู่และยึด `HEALTH_CHECK_PORT=8001`; kill ตัวเก่าแล้วรีสตาร์ทด้วย `scripts/dca_tool.py` ให้เหลือตัวเดียว (pid ล่าสุดเปลี่ยนตามการรีสตาร์ท).
- หมายเหตุ: เครื่องบางตัวไม่มี `rg` (ripgrep) ให้ใช้ `grep` แทนเวลาเช็ค process/log.

Key env flags (Dec-2025)
- Scheduler lock: `SCHEDULER_DB_LOCK_ENABLED=1`, `SCHEDULER_DB_LOCK_NAME=dca_scheduler`, `SCHEDULER_DB_LOCK_TIMEOUT=1`
- DB dedupe: `DB_DEDUPE_ENABLED=1`
- Dedupe cleanup: `DEDUPE_CLEANUP_ENABLED=1`, `DEDUPE_CLEANUP_DAYS=30`, `DEDUPE_CLEANUP_INTERVAL_HOURS=6`

Docs added
- `planimproved.md`: roadmap/phase plan เพื่อกลับมาทำต่อ
- `docs/s4_flow_diagram.md`: decision tree / forensic doc ของ S4 policy + execution flow

2025-12-18 — S4 Hardening Gates + OKX Execution Hardening (Production Policy)

Scope
- ใช้กับ S4 บน OKX spot เท่านั้น (BTC-USDT ↔ XAUT-USDT) และไม่กระทบ flow CDC/weekly_dca/reserve_buy/half_sell.
- ทุกอย่างอยู่หลัง feature flags; ปิด flag → behavior เดิม 100%.

S4 Hardening Gates (เปิดด้วย `S4_HARDENING_ENABLED=1`)
- okx_ratio เป็น PRIMARY 100% สำหรับ “flip decision”:
  - okx_ratio missing/invalid/stale/parse ไม่ได้ → HOLD (ไม่ fallback ไป binance_cdc เพื่อ flip)
  - TTL guard: `S4_RATIO_TTL_MINUTES=30`
- 2-day confirmation: `S4_CONFIRM_DAYS=2` (นับต่อวัน 1D และต้องเป็นวันติดกันจริง; scheduler tick 5 นาทีไม่ทำให้ผ่านเร็วขึ้น)
- Cooldown hard lock: `S4_COOLDOWN_DAYS=3`
- Circuit breaker: `S4_MAX_FLIPS_30D=2` นับเฉพาะ flips ที่สำเร็จจริง (`executed_ok=true` ใน `strategy_rotation_log.metadata_json`)
- Alerts + logs:
  - HOLD ส่ง LINE alert แบบ throttle และ log `S4 HOLD | reason=...`
  - วิธี monitor: `egrep -n "S4 HOLD|S4 EXEC CHECK|OKX order|executed_ok" scheduler.out`

S4 Execution Hardening (OKX only; เปิดด้วย `S4_EXEC_HARDENING_ENABLED=1`)
- Symbol-aware spread guard จาก top-of-book ต่อ symbol:
  - `S4_MAX_SPREAD_PCT_BTC=0.60`, `S4_MAX_SPREAD_PCT_XAUT=0.50`
  - ถ้า spread ไม่ผ่าน → HOLD `reason=s4_spread_guard` + alert
- Limit-first execution wrapper (default 45s): `S4_LIMIT_FIRST_SECONDS=45`
  - SELL leg: limit sell @ ask (timeout → cancel)
  - BUY leg: limit buy @ bid (timeout → cancel)
  - ถ้า sell/buy ไม่ fill เลย → HOLD (`s4_sell_unfilled` / `s4_buy_unfilled`)
- IOC fallback เป็น opt-in (default ปิด): `S4_IOC_FALLBACK_ENABLED=0` (เปิดทีหลังเมื่อมีหลักฐานว่า limit-first unfilled บ่อยแต่ spread ผ่าน)
- OKX adapter เพิ่ม capability ใน `exchanges/okx.py`:
  - limit/IOC order + poll status + cancel on timeout
  - logs: `OKX order placed/timeout/canceled...`

State update hardening (executed_ok-gated)
- นิยาม `executed_ok` เป็น “แหล่งความจริงเดียว” และบันทึกไว้ใน `executed_meta`.
- `runtime.last_flip_at` และ `runtime.active_asset` จะถูก set เฉพาะเมื่อ `executed_ok=true` (กัน cooldown ติดจาก partial/unfilled/abort).

Timing-based ops (หลัง daily close วันถัดไป)
- เป้าหมาย: ยืนยันว่าระบบ “เริ่ม flip attempt” หรือ “ยัง HOLD อย่างมีเหตุผล” ด้วยหลักฐานจาก log
- รันคำสั่งนี้หลัง daily close ของวันถัดไป (หรือเมื่อคาดว่า CDC/ratio จะเปลี่ยน):
  - `egrep -n "S4 HOLD|S4 EXEC CHECK|OKX order|executed_ok|last_flip_at|active_asset" scheduler.out | tail -n 260`
- การตีความอย่างย่อ:
  - เห็น `S4 HOLD | reason=confirm_pending` → ยังไม่ flip attempt (รอยืนยัน 2 วันติด)
  - เห็น `S4 EXEC CHECK ...` → เริ่ม flip attempt (เข้า execution hardening แล้ว)
  - เห็น `OKX order placed/timeout/canceled` → เริ่ม lifecycle ของ limit-first/IOC (ถ้าเปิด)
  - เห็น `executed_ok=true` → flip สำเร็จจริง และหลังจากนั้นเท่านั้นจึงควรเห็น `last_flip_at/active_asset` เปลี่ยน

2025-11-10 — Scheduler Dupes & S4 Flex Confirmation
- พบ error `Scheduler error: '>' not supported between instances of 'str' and 'int'` ระหว่าง S4 DCA เพราะ process เก่า (PID 3250973 เริ่ม Oct-20) ยังรันโค้ดก่อน `_order_id_payload` เลยเทียบ `order_id > 0` แล้ว crash ระหว่างส่ง Flex; ส่งผลให้ schedule #27 ยิง order เสร็จแต่ไม่ log last_run.
- ตรวจ `ps -ef | grep main.py` พบ scheduler รันซ้ำ 2 ตัว (3250973 และ 3576342) ทำให้ 17:30 มีคำสั่งซื้อซ้ำคนละ process; หยุดทั้งคู่ (`kill 3250973 3576342`) แล้วยืนยันไม่มี process เหลือก่อนให้ผู้ใช้ start ใหม่ด้วยเทอร์มินัลเอง (PID 3763587).
- หลัง restart เดี่ยว, รันซื้อจริง (XAUT 5 USDT ที่ 18:05, schedule #28) แล้วได้รับ LINE Flex “S4 DCA Buy” ครบทุก field (order id 3028968790636503040, fee, holdings) ยืนยันว่าฟังก์ชัน Flex/allowlist `s4_dca` ทำงานปกติ.

2025-10-19 — Flex Notifications & Live Mode Toggle
- ยืนยันการตั้งค่า LINE Flex `LINE_USE_FLEX=1` พร้อม allowlist `weekly_dca,reserve_buy,half_sell,s4_dca,s4_rotation` ทำให้ Weekly DCA, Reserve Buy, Half Sell และ S4 alerts แสดงเป็น Flex; บันทึกว่าฟังก์ชันบางตัว (security alert, scheduler status) ยังเป็นข้อความธรรมดา
- เห็นตัวอย่าง Flex ใหม่ “Bitcoin Dashboard” (dark theme) ส่งสำเร็จ พร้อมค่าตลาด BTC/MVRV/Fear & Greed ใช้ธีมเดียวกับ Flex dashboard ก่อนหน้า
- ปรับ `.env` ให้ `DRY_RUN=0` เพื่อเปิดโหมดเทรดจริง และรีสตาร์ท `app.py` กับ `main.py` หลังเปลี่ยนค่าแล้ว (ต้องระวังคำสั่งสั่งซื้อจริงตั้งแต่นี้ไป)
- เพิ่ม `scripts/dca_tool.py` เป็น utility CLI รวมคำสั่ง start/stop/status scheduler, preview Flex ตัวอย่าง และเช็ก balance (`venv/bin/python scripts/dca_tool.py …`)

2025-10-20 — OKX Cap & Flex Stabilisation
- OKX per-order cap: `/api/okx_config` now accepts 0 (unlimited) and rejects negatives; `api_strategy_state` and `evaluate_notional_cap` respect explicit 0 instead of falling back to env defaults.
- Strategy UI: “OKX Max per order (USDT)” shows “Current: Unlimited” when cap=0 and allows saving blank/0 values; validation updated to permit zero and keep positive numbers unchanged.
- Database patched so `strategy_state.okx_max_usdt` = 0, matching `.env`; restart `app.py` + scheduler ensured new logic ran everywhere.
- Flex notifications verified live: allowlist `weekly_dca,reserve_buy,half_sell,s4_dca,s4_rotation` confirmed, S4 DCA dry-run test hit Flex path (no fallback to plain text).
- Resolved duplicate scheduler process that caused double S4 orders (two `main.py` instances). Used `scripts/dca_tool.py scheduler stop/start` to relaunch single PID (3250973 as of 2025-10-20 16:00) and confirmed only one app.py instance (3250242).

Recent Fixes (2025-10-19)
- Observed multiple Flask servers binding to :5001, causing intermittent old UI and 404 HTML responses.
- Stopped duplicate processes and added a single-instance lock to app.py to prevent multiple servers.
- Ensured all /api/* routes return JSON on errors to avoid “Unexpected token '<'” in frontend:
  - Added JSON 404/405 handlers for /api/*.
  - Added catch‑all route /api/<path> → JSON 404.
  - Added trailing-slash aliases for POST /api/strategy_toggle and /api/strategy_update.

Verification Checklist and Results
- GET /api/strategy_state → 200 JSON (fields: cdc_enabled, sell_percent, reserve_usdt, etc.).
- POST /api/strategy_update {sell_percent} → 200 JSON; value persisted in DB (tested 60 → reverted to 50).
- POST /api/strategy_toggle {enabled} → 200 JSON; toggles cdc_enabled (tested false → reverted true).
- GET /api/cdc_action_zone → 200 JSON with status up/down.
- GET /api/analytics → 200 JSON with series and summary.
- GET /api/wallet → 200 JSON with balances and valuation.
- GET /api/sell_history and /api/reserve_log → 200 JSON arrays.
- GET /api/does-not-exist → 404 JSON (not HTML).

Current Key Settings (from /api/strategy_state at verification)
- cdc_enabled: true
- sell_percent_binance: 100 (global sell_percent currently 55; OKX leg 0)
- last_cdc_status: down

How to Run Locally/Server
- Activate venv and run: venv/bin/python app.py (binds 0.0.0.0:5001)
- app.py now uses file lock web.lock to avoid duplicate instances.
- Trading engine (scheduler/health): python main.py (optional; provides health text at HEALTH_CHECK_PORT, default 8001).

Important Endpoints
- UI: /
- Admin: /admin
- Health: /health (JSON)
- API JSON:
  - GET /api/wallet
  - GET /api/analytics
  - GET /api/cdc_action_zone
  - GET /api/strategy_state
  - POST /api/strategy_update, /api/strategy_update/
  - POST /api/strategy_toggle, /api/strategy_toggle/
  - GET /api/sell_history
  - GET /api/reserve_log
  - POST/GET /api/sync_trades (plus /api/sync_trades/)
  - POST /api/sync_trades_range
  - GET /api/sync_trades_progress
  - POST /api/reconcile_trades

Notes for Future Work
- If deploying via systemd/supervisor/docker, ensure only one app.py instance is launched to avoid UI switching.
- Consider serving /static/favicon.ico to remove 404s seen in logs.
- Add cache headers: index.html should be no-cache while static assets can be cached aggressively.
- If frontend ever moves to separate Nginx, keep proxy consistent and forward /socket.io correctly for SocketIO.

Troubleshooting Tips
- If frontend shows “Unexpected token '<'”, check that /api/* returns JSON (curl and verify Content-Type: application/json). The catch-all handler should prevent HTML responses.
- To confirm the active server: ss -ltnp | rg 5001 and ps -fp <pid>.
- Logs: app.log (web), web.out (process stdout), btc_purchase_log.log (engine).

2025-12-22 — CDC Signal Source Labeling (Heartbeat + UI + S4 Notifications)

What changed
- Heartbeat payload now includes `signal_source` from S4 runtime (fallback binance_cdc if only CDC state exists).
- Heartbeat messaging shows `CDC Signal: UP/DOWN (SOURCE)` instead of plain CDC value to avoid confusion with action-zone transitions.
- S4 notifications updated to show CDC Signal with source:
  - `notify_s4_dca_buy` and `notify_s4_rotation` render `CDC Signal: UP/DOWN (SOURCE)`.
  - Source label mapping: `okx_ratio` → “OKX BTC/XAUT”, `binance_cdc` → “Binance BTCUSDT”.
- UI updated to display CDC source labels consistently:
  - Summary “S4 CDC Signal” card source uses mapped label.
  - S4 meta row shows mapped signal source.

Notes
- Mapping is label-only; logic still uses okx_ratio primary when S4 hardening enabled and falls back to binance_cdc otherwise.

2026-01-20 — S4 Confirm/Flip Fix + Dedupe for S4 DCA

What changed
- Fixed S4 confirmation logic so flips occur only after confirmation: added `runtime.last_confirmed_status`/`last_confirmed_at` and use this for rotation decisions; `last_cdc_status` still tracks latest signal for UI.
- Cleared stale `runtime.last_hold_reason`/`last_hold_detail` when no HOLD gate triggers in a tick to prevent UI showing old `confirm_pending`.
- Added DB dedupe for S4 DCA buys in `execute_s4_dca` with key `s4_dca:YYYY-MM-DD:schedule_id` to prevent duplicate buys even if multiple schedulers run.
- Added LINE alert when S4 DCA is skipped by dedupe (throttled 6h per schedule via `_s4_should_alert`).

Operational notes
- Root cause of duplicate S4 DCA buys: multiple `main.py` processes running. Fix by using **one** supervisor.
- Recommended: run scheduler via systemd only (service `dca-scheduler`) and avoid `dca_tool start` to prevent double processes.

2026-01-28 — S4 Neutral Zone Phase 1/1.5 (Log-only) + Spec + Daily Flex

Highlights
- Spec finalized and stored at `docs/strategies/s4-neutral-zone-spec.md` (also copied to `docs/s4-neutral-zone-spec.md`).
- New pure helper `strategies/s4_neutral_zone.py` computes neutral/weak/btc/gold state from EMA12/EMA26 (log-only; no behavior changes).
- OKX ratio series + EMA helpers added in `strategies/s4_utils.py` to support neutral calculations.
- `main.py` logs neutral state changes + daily EOD summary (log-only) and persists runtime metrics.
- DB tables created for log collection (manual creation + auto-ensure in `main.py`):
  - `s4_neutral_zone_eod` (daily closed candle summary)
  - `s4_neutral_zone_state_changes` (event-driven state transitions)
- Added report script `scripts/s4_neutral_zone_report.py` for EOD + state-change summaries (CSV export supported) and auto-loads `.env`.
- Added SQL quick-check script `scripts/s4_neutral_zone_queries.py`.
- Added dashboard/CSV generator `scripts/s4_neutral_zone_dashboard.py` (writes `static/s4_neutral_zone_dashboard.html` + CSVs in `log/`).
- Added daily + weekly LINE Flex notifiers:
  - `scripts/s4_neutral_zone_daily_notify.py`
  - `scripts/s4_neutral_zone_weekly_notify.py`
  - cron:
  - `20 8 * * * .../venv/bin/python scripts/s4_neutral_zone_daily_notify.py >> log/s4_neutral_zone_daily_notify.log 2>&1`
  - `20 9 * * 1 .../venv/bin/python scripts/s4_neutral_zone_weekly_notify.py >> log/s4_neutral_zone_weekly_notify.log 2>&1`
- Created backups:
  - `backup_files/s4-neutral-zone-spec_20260128.tar.gz`
  - `backup_files/dca_full_backup_20260128.tar.gz`

Operational notes
- Tables may be missing until `main.py` runs or manual SQL is executed; report script requires DB_* envs.
- For ad-hoc table creation, use explicit `load_dotenv(dotenv_path=".env")` when running via stdin.
- Phase status: collecting logs only; next step is wait 30–90 days before Phase 0.5 (threshold selection).

2026-01-29 — S4 Neutral Zone Live Monitoring Status (Checklist)

Summary
- Phase 1 + 1.5 running live: EOD log + Flex daily/weekly + dashboard/CSV are working.
- Dashboard shows first real row: `2026-01-27 gold_signal` with EMA gap 4.6117% and slope -3.8224 (CDC down, asset GOLD).
- State change log is still empty (expected early; no state flips yet).

Monitoring checklist (during 30–90 day collection window)
- Weekly: confirm EOD rows are present (no missing dates).
- Weekly: state values always in 4-state enum (neutral/weak/btc/gold).
- Watch for red flags:
  - No EOD rows for >2 days → cron/scheduler issue.
  - neutral_zone ~0% for weeks → thresholds too tight (fix later in Phase 0.5).
  - neutral_zone >60% → thresholds too wide.
  - frequent state changes intraday → logic issue (should be daily close).

Next steps
- Wait for 30+ EOD rows and 5–10 state changes.
- Then move to Phase 0.5 (threshold selection) using CSV exports.

2026-02-02 — S4 Neutral Zone Ops: Lag, Backfill, Cron, Dedupe Fix

Highlights
- Added EOD lag indicator: `eod_lag_days` stored in DB and shown in Flex + dashboard.
- Daily/weekly Flex now show **Alert: EOD lag > 1 day** and theme switches to warning when lag > 1.
- Cron schedule adjusted (Bangkok):
  - Daily Flex 09:20, dashboard refresh 09:25, weekly Flex (Mon) 09:30.
- Added daily backfill cron (09:35) to fill last 2 UTC days via `scripts/s4_neutral_zone_backfill.py`.
- Backfill executed for 2026-01-29 → 2026-01-31; OKX data not yet available for 2026-02-01/02 at the time.
- Added `Updated (BKK)` to Flex cards to avoid confusion with stale screenshots.
- Updated `run_s4_tick` to log OKX series regardless of active exchange (log-only Phase 1.5).

Operational fixes
- S4 DCA dedupe moved **after** basic checks (balance/price) to avoid false skip when funds are insufficient.
- Dedupe key for 2026-02-02 cleared manually to allow a fresh S4 DCA test.

Notes
- If OKX daily candle lags, EOD lag increases but logic remains correct; flip timing would be delayed once Phase 2 is enabled.
- Scheduler log source is systemd journal; `scheduler.out` can be stale.


2025-10-19 — LINE Dashboard Dark Theme & Flex Test

What changed
- ปรับ `dashboard-edit20092025.py` ให้ใช้ดีไซน์โหมดมืด: นิยามพาเลตสี `DARK_THEME` และอัปเดตฟังก์ชันสร้าง Flex summary/alert ให้เรียกใช้สีหลัก-รอง, สีบวก/ลบ, และ separator ใหม่
- เพิ่ม CLI helper `send_test_flex()` และอาร์กิวเมนต์ `--send-test-flex` สำหรับส่งข้อความ Flex ทดสอบโดยไม่ต้องรัน APScheduler
- ปรับสี severity ในการแจ้งเตือนความผันผวน/EMA ให้ยึดตามพาเลตใหม่ เพื่อความสอดคล้องของโทนมืด
- ทดสอบด้วย `python3 -m compileall dashboard-edit20092025.py` และ `python3 dashboard-edit20092025.py --send-test-flex` ได้ Flex preview ผ่าน LINE สำเร็จ

Notes / follow-up
- หากต้องการปรับเฉดสีเพิ่มเติม ให้แก้ที่ dict `DARK_THEME`
- พิจารณาเพิ่มพาเลต footer หรือ Flex ประเภทอื่นในอนาคต หากมี use case เพิ่ม

2025-09-21 — OKX Integration, UI/Exports, Live Tests

What we implemented
- Multi‑exchange architecture
  - Added Exchange Adapter layer with unified API (price/balance/filters/market buy+sell).
  - BinanceAdapter (live + dry_run) and OkxAdapter (live + dry_run; quote sizing with tgtCcy fallback to qty; per‑order cap).
  - Factory `exchanges/factory.py` to get adapter by `strategy_state.exchange`.
- Database migrations (idempotent via app.py)
  - `strategy_state.exchange` (global switch), `sell_percent`, `okx_max_usdt` (per‑order cap for OKX).
  - Add `exchange` column to `purchase_history` and `sell_history` (now always recorded).
  - New `okx_trades` table for fills history.
- Engine (main.py)
  - All trading paths now go through adapter (purchase_btc, execute_half_sell, execute_reserve_buy).
  - Respects `strategy_state.okx_max_usdt` when exchange=okx.
  - CDC signal still sourced from Binance (as decided) to keep logic consistent.
  - DRY_RUN: for Binance/OKX handled in adapters; OKX DRY_RUN skips balance check.
- Web/API
  - New endpoints: `/api/strategy_exchange` (ADMIN), `/api/okx_config` (ADMIN), `/api/okx_trades_sync`, `/api/okx_trades`, `/api/okx_trades_analytics`.
  - Export CSV endpoints:
    - `/api/purchase_history_export?exchange=all|binance|okx`
    - `/api/sell_history_export?exchange=all|binance|okx`
    - `/api/binance_trades_export?limit=N`
    - `/api/okx_trades_export?limit=N`
  - `/api/strategy_state` now returns `exchange`, `sell_percent`, `okx_max_usdt`, and flags.
- UI (templates/index.html)
  - Strategy: exchange selector (ADMIN_TOKEN), Sell on RED (%) with inline Current, OKX Max per order (USDT) with inline Current.
  - Header badges: EXCHANGE (colorized: BINANCE=yellow, OKX=blue) + TESTNET/DRY_RUN.
  - Analytics: added “OKX Trades (BTC‑USDT)” section with Sync, summary cards (Total Buys/Sells, Position, Avg Price, Unrealized, Realized) and trades table.
  - Purchase History: export CSV (filter by exchange). Strategy → Sell History: export CSV. Trades sections: export CSV.
  - ADMIN_TOKEN remembered in sessionStorage (with Clear Token button) to reduce prompts.

Live tests performed
- Binance (live): one‑off market buy ~10 USDT OK; recorded in `purchase_history` with `exchange='binance'` and sent LINE notification.
- OKX (DRY_RUN): scheduled buy 10 USDT succeeded; recorded with `exchange='okx'`.
- OKX (live): Resolved 401 by using RFC3339/ISO timestamp in headers and correct signing; executed live market buy ~10 USDT OK; recorded with `exchange='okx'` and sent LINE.

Important learnings / fixes
- OKX private API 401 root causes: incorrect timestamp/sig or missing headers; solved by ISO8601 UTC ms timestamp and signing path+query. Added optional `x-simulated-trading` header via `OKX_SIMULATED=1`.
- DRY_RUN logic must bypass balance check for OKX testing; implemented.
- Single‑instance guard: when encountering “Another instance appears to be running”, stop existing process (use web.pid) or clear stale `web.lock` only after confirming no process on :5001.
- Always record `exchange` in histories to enable filtered analytics/exports.

Operational notes
- Production flags:
  - Disable dry runs: `DRY_RUN=0`, `STRATEGY_DRY_RUN=0`.
  - Binance live: `USE_BINANCE_TESTNET=0`, `BINANCE_TESTNET=0`.
- OKX live: `OKX_LIVE_ENABLED=1`; per‑order cap from DB `okx_max_usdt` (0 = unlimited).
- Admin endpoints require `ADMIN_TOKEN` (set in .env). The web UI prompts for this token; it is cached in browser sessionStorage (user can clear).
- Exports: History and Trades CSV available from UI and endpoints listed above.

Open follow‑ups / ideas
- Add UI for OKX analytics charts (PnL timeline) similar to Binance performance chart.
- Optional TTL for ADMIN_TOKEN in session (e.g., auto‑expire after 30 min).
- Add time‑range filters to all export endpoints (start/end ISO8601).
- Improve OKX fee conversions for non‑USDT assets when present (current implementation treats non‑USDT, non‑BTC fees as 0 in OKX analytics; extend to convert via price snaps if needed).


2025-09-26 — Reserve Management & Wallet UI Enhancements

Highlights
- `/api/wallet` now returns per-exchange snapshots (`binance`, `okx`) plus totals; front page shows USDT/BTC/Portfolio/Reserve cards for each exchange.
- Added manual reserve transfer capability:
  * Backend helpers `increment_reserve` / `_exchange` accept reason/note and ignore non-positive amounts.
  * New admin endpoint `POST /api/reserve_transfer` (requires `ADMIN_TOKEN`). Validates token, amount, exchange, and optionally checks free balance when not testnet/dry-run; logs reserve updates and returns latest reserves.
  * UI on Strategy tab includes “Move to Reserve (USDT)” inputs for Binance/OKX; prompts for ADMIN_TOKEN, handles error cases (missing/invalid token, insufficient balance), refreshes strategy state + wallet after success.
- `/api/strategy_update` & UI now allow Sell on RED (%) up to 100% (was capped at 90%).
- Wallet grid rearranged: top row OKX metrics, second row Binance, plus total reserve card; reserve badges in header reflect latest values.

Operational Notes
- Reserve transfers only adjust internal ledger; actual USDT free balances on exchanges remain until trades execute (explained to user).
- API returns specific errors: `admin_token_not_configured`, `invalid_admin_token`, `amount_must_be_positive`, `insufficient_<exchange>_balance`.
- Frontend clears cached ADMIN_TOKEN when server reports token invalid.


2025-10-14 — Dashboard Production Layout & S4 Activation

Highlights
- Rebuilt dashboard header into “Summary Grid” showing Scheduler status, **S4 CDC Signal**, Exposure, Capital Pool, and Recent Alerts; badges (env, reserves) remain surfaced.
- Wallet section collapsed into single card with “Hide small balances” toggle and “Show all” button; wallet items auto-hide when under configurable thresholds while keeping CDC controls visible.
- S4 Signal Deck reorganized into two-column overview (metrics + guards) with allocation list below; guard list now easier to scan and runtime notes rendered inline.
- Reserve & Compliance logs moved into accordion panels under History tab to reduce clutter on landing view.
- Default metadata for `s4_multi_leg` set to `active`; also ran helper script to update DB `strategy_state` row (`metadata_json.status` + `strategy_status`) so UI stops showing “BETA”.
- Summary card and CDC card share color classes; script refresh automatically colors S4 CDC status (green UP / red DOWN) and labels source using runtime (`snapshot.source` / `signal_source` fallback) rather than always `binance_cdc`.

Operational Notes
- Utility script in venv (`venv/bin/python …`) loads `.env.production`, updates `strategy_state` row; safe to reuse if status drifts.
- Frontend websocket listener now updates header total via `summary-total-amount`; scheduler badge relies on `scheduler_status` API heartbeat.

Future Ideas
- Consider pinning important badges (e.g., DRY_RUN) in Scheduler card with clearer severity colors.
- Chart follow-up: mini exposure chart to pair with summary percentages.


2025-10-14 — S4 DCA Schedules & CDC Status Parity

Highlights
 - Schedules tableและฟอร์มเพิ่ม/แก้ไข รองรับ `exchange_mode='s4'` เพื่อให้ผู้ใช้กำหนด DCA รายสัปดาห์สำหรับ S4 แยกจาก CDC (`Add New Schedule` มีตัวเลือก “S4 (CDC BTC↔GOLD)” และ Active table แสดง badge “S4 Auto (active asset)”).
 - Scheduler (`main.py`) ตีความ `exchange_mode='s4'` แล้วเรียก `execute_s4_dca()` ด้วยยอดที่ตั้งไว้ เติมสินทรัพย์ฝั่งที่ S4 ถืออยู่ ณ ตอนนั้นตาม exchange ที่ตั้งใน config.
 - อัปเดต UI ให้แจกแจง split amounts เฉพาะ CDC; โหมด S4 แสดงหมายเหตุว่าระบบเลือกฝั่งซื้ออัตโนมัติจาก runtime.
 - ปรับสถานะการ์ด CDC ให้ใช้ข้อมูล `strategy_state.last_cdc_status` โดยตรง (เลิก fetch /api/cdc_action_zone แยก) ทำให้สีในแดชบอร์ดตรงกับ DB/engine เสมอ.
 - Guard Rails ใน S4 Signal Deck เปลี่ยนเป็นสถานะ `CONFIGURED` และเพิ่มสีใหม่ ลดความงงจากค่า default pending/planning.

Operational Notes
- ต้องรีสตาร์ท `app.py` หลังขยาย ENUM ใน DB เพื่อโหลดโค้ดใหม่ ไม่เช่นนั้นฟอร์มจะมอง mode `s4` ว่า invalid.
- Schedule โหมด S4 ใช้ `purchase_amount` ล้วน ๆ (ไม่ใช้ binance_amount/okx_amount); CDC ยังใช้ split ตามค่าเดิมได้.

Future Ideas
- ปรับ scheduler ให้ skip S4 DCA ในวันที่ full rotation เกิดขึ้น เพื่อลดคำสั่งซ้ำ.
- แยกหน้าจอจัดการ schedule ของ S4 ออกมาโดยเฉพาะ พร้อมกราฟสรุปการเติมทุน overlay.

2025-10-17 — Production Environment Flip & Strategy Audit

What changed
- Swapped `.env.production` into place as the active `.env`, disabling both `STRATEGY_DRY_RUN` and `DRY_RUN` for true live execution.
- Restarted `app.py` and `main.py` via the project venv to ensure the new environment variables loaded cleanly.
- Verified `/api/strategy_state` now reports `dry_run:false` and confirmed scheduler/web logs (`scheduler.out`, `web.out`) for healthy restarts.
- Reviewed live CDC & S4 strategy state in MySQL (`strategy_state`, `schedules`) after the flip to document active guard rails, sell policies, and S4 rotation config (OKX, 100% flip targets).

Next steps / reminders
- Monitor balances/guards before the next CDC transition since half-sell and reserve-buy will now place real orders.
- Restore `.env.dev-backup-20251017` if dry-run mode is required for future testing.

2025-10-17 — CDC Weekly Skip Analysis

Findings
- Verified Line alerts at 13:50 and 15:30 ICT tie to schedules `id=21` and `id=22`, both Binance-only DCA entries that now push funds into reserve when CDC=down.
- Confirmed `reserve_log` rows (#4, #5) reflect the +8.00 and +36.90 USDT increments and explain why no market orders hit `purchase_history`.
- Documented that once CDC flips back to up, `decide_weekly_dca` will emit real buys again and reserve balances will deploy.

Reminder
- Consider toggling `cdc_enabled` or adjusting schedule modes if buys should continue even during CDC red periods.

2025-10-19 — S4 DCA Notification Context Refresh

Highlights
- Added helper `fetch_schedule_context` in `main.py` to look up schedule time + friendly label (prefers `line_channel`, falls back to JSON metadata) for use in S4 notifications.
- Expanded S4 DCA LINE alert formatting (`notify.py`) to the concise template:
  ```
  S4 DCA Buy
  Asset: … | Exchange: …
  Amount: … USDT
  Qty: … @ …
  Schedule: #N | CDC: …
  Mode: LIVE/DRY RUN | Order: …
  Holdings: …
  ```
  keeping fees/holdings appended when available.
- Database now includes `schedules.line_channel`; schedule #20 set to “BTC-Information” so alerts reference the intended label.

Notes
- Dry-run tests (execute_s4_dca with `DRY_RUN=1`) confirmed the message renders with schedule label and CDC status; unset the env afterward for live mode.
- If new schedules should surface labels, remember to fill `line_channel` or embed `slot_label` inside `metadata`.

2025-11-03 — S4 DCA Alert Hardening

Highlights
- เพิ่ม error handling ใน `execute_s4_dca`: จับ exception จาก `notify_s4_dca_buy`, log ระดับ error พร้อม fallback ข้อความธรรมดา ส่งผ่าน `send_line_message_with_retry`.
- สร้าง helper `_order_id_payload` เพื่อรองรับ `order_id` ที่กลับมาเป็น str/int (แก้ TypeError `'>' not supported between instances of 'str' and 'int'` เมื่อติดต่อ OKX จริง).
- ทดสอบด้วย dry-run ที่บังคับ Flex ล้มเหลว → fallback message ส่งสำเร็จ และ manual live run (`venv/bin/python -c ... execute_s4_dca(..., 5, 27)`) ได้ LINE Flex พร้อม order id/fee ครบ.

Operational notes
- Production scheduler เริ่มต้นใหม่แล้ว; manual live buy 5 USDT ยืนยันว่า Mode=LIVE และ order id แสดงถูกต้องบน Flex card.
- หาก LINE Flex ส่งไม่สำเร็จอีก จะเห็น log ERROR ใน `scheduler.out` พร้อมข้อความ fallback บน LINE.

2025-10-26 — LINE Flex Message Rollout

Highlights
- เปิดใช้ feature flag + allowlist (`LINE_USE_FLEX`, `LINE_FLEX_ALLOWLIST`) และสร้าง scaffolding: โมดูล `notifications/line_flex.py`, สคริปต์ preview, test suite (`tests/test_line_flex.py`).
- Weekly DCA buy/skip (ทั้ง global และ exchange) ส่ง Flex card ธีม success/warning; fallback เป็นข้อความเดิมเมื่อ flag ปิดหรือส่ง Flex ล้มเหลว.
- Reserve buy executed และ half-sell executed แสดง Flex card แยกธีม (`success`, `danger`) พร้อม holdings/meta ใน footer.
- S4 DCA buy และ S4 rotation แปลงเป็น Flex card ผ่าน theme `info`, footer แสดงคำสั่ง sell/buy และ realized notional.
- เพิ่ม unit tests ครอบ routing/fallback สำหรับทุก channel Flex ใหม่, Update release notes v2.2.0–v2.2.3 พร้อมแท็ก GitHub.

Operational notes
- ทดสอบ DRY_RUN ผ่าน notify ฟังก์ชัน → Flex card ขึ้นใน LINE แล้ว; staging/production ใช้ allowlist `weekly_dca,reserve_buy,half_sell,s4_dca,s4_rotation`.
- `memory.md` เก็บตัวอย่าง card เพื่อ reference สี/ข้อมูลก่อน iterate ต่อ.

2025-10-18 — OKX S4 DCA Fix & Notifications

What changed
- ขยาย `OkxAdapter` ให้รองรับสัญลักษณ์ `XAUT-USDT` ครบชุด (price, filters, market buy/sell, market data helpers) แก้ปัญหา S4 DCA โหมดทอง error.
- ปรับ `execute_s4_dca` ส่งข้อมูล order id + ค่าธรรมเนียมจาก adapter ไปที่ payload.
- อัพเดต `notify_s4_dca_buy` ให้แสดง notional, average price, qty, schedule, CDC status, mode/order, และค่าธรรมเนียมในข้อความ LINE.
- รีสตาร์ต scheduler ด้วย venv (`venv/bin/python main.py`) ยืนยันเริ่มทำงานใหม่ที่ 2025-10-18 11:51:27 และไม่มี error adapter อีก.
- จัดทำ `planHoldings.md` เก็บแผนเพิ่มการแสดงยอดคงเหลือกลยุทธ์ เพื่อกลับมาทำภายหลัง.

Insights / follow-up
- ตรวจรอบ S4 DCA ถัดไปเพื่อดูข้อความ LINE รูปแบบใหม่และบันทึก `purchase_history` พร้อมค่าธรรมเนียม.
- เมื่อกลับมา implement แผน holdings ให้เพิ่ม caching และบันทึกเวลาอัปเดตเพื่อหลีกเลี่ยง rate limit.

2025-10-18 — Holdings Snapshot & CDC Integration

What changed
- เพิ่มบริการกลาง `services/balance_service.py` สำหรับดึงยอดคงเหลือพร้อม TTL cache และ helper `get_adapters` ใน factory
- ผูก S4 + CDC runtime เรียก balance service: ข้อมูล holdings แปะใน metadata, LINE notification ทั้ง S4 DCA และ CDC weekly buy/skip มีบรรทัด Holdings พร้อมสถานะ cached/error
- เปิด REST `GET /api/strategy_holdings` (force refresh ผ่าน `?refresh=1`) คืน snapshot ต่อ exchange/asset + meta errors
- ปรับแดชบอร์ด (`templates/index.html`) แสดง “Holdings Snapshot” ในทั้ง CDC/S4 card และเรียก endpoint ทุก ~45 วิ; formatter ใหม่จัดการ stale/error UI
- เพิ่ม unit tests (`tests/test_balance_service.py`, `tests/test_notify_holdings.py`) ครอบ caching + การ render holdings line
- เก็บยอดค่าธรรมเนียมแบบ cumulative ด้วยตารางใหม่ `strategy_fee_totals` (auto update ผ่าน `record_fee_totals`) และเปิด `/api/fee_totals` คืน summary buy/sell ต่อ exchange/strategy
- ปรับ Strategy Dashboard UI ให้เรียงการ์ดตามกลยุทธ์/Exchange พร้อม summary card ใหม่ (CDC status, active exchange, capital pool, fee totals, alerts) และคำนวณ reserves จากข้อมูล per-exchange เพื่อให้ค่าที่แสดงตรงกับสถานะจริง
- Global summary/strategy cards เชื่อมข้อมูล real-time: `loadFeeTotals()` เติมยอดสะสม, recent alerts กรองตาม strategy, timeline/guard rail ใช้ log ล่าสุด และ holdings/fees รีเฟรชตาม schedule
- UI fallback ใช้ `/api/wallet` ที่มี extra assets (XAUT/PAXG) ช่วยแสดง holdings/fee/reserve card ให้ตรง ในกรณีที่ balance service ยังไม่ตอบ
- Fee cards แยกยอดแบบ per-strategy แล้ว (CDC vs S4) โดยอ่านจาก `strategy_fee_totals` → แจ้งเตือนและการ์ด OKX แสดง Fee จาก S4 DCA ล่าสุดถูกต้อง
- เคลียร์ schedules ที่ `is_active=0` ออกจาก DB → หน้า Active Schedules เหลือเฉพาะ job ที่ยังใช้งานจริง (id #3, #20)
- ปรับโครง UI Strategy Tab: มี Global Summary cards, การ์ด Holdings/Fees/Reserves ต่อ exchange และ activity timeline ที่ดึงจาก alerts/recent logs

Next steps / reminders
- พิจารณาแนบ holdings ใน endpoints อื่น (wallet/report) หรือ socket.io feed หากต้อง real-time มากขึ้น
- Monitor LINE ข้อความว่า holdings บรรทัดใหม่ไม่ยาวเกิน และ OKX/ Binance API rate ไม่โดนเกินเพราะ cache TTL = 30s

2026-02-12 — Bitkub PURE_DCA Fill Accuracy + Monitor Lessons

What changed
- เพิ่มโหมด `bitkub` ใน schedule flow ให้ซื้อ DCA BTC/THB ได้บน Bitkub และแสดงบนหน้า Active Schedules (mode badge + quote asset THB)
- แก้ปัญหาเคส “ซื้อจริงแต่ระบบแจ้ง not filled”:
  - เดิม adapter อาจตอบกลับเร็วเกินไป ทำให้ `executed_qty/cummulative_quote_qty` ยังไม่ครบ
  - เพิ่ม fallback ใน `purchase_on_exchange` ให้ infer จาก balance delta (BTC/THB ก่อน-หลังซื้อ) เฉพาะเมื่อ fill fields ไม่ครบ
- เพิ่ม logging/observability สำหรับการแจ้งเตือน LINE:
  - log success/fail/exception ของ weekly notify ชัดเจน
  - log Flex สำเร็จ (`Line flex sent successfully ...`)
- ทำให้รองรับ `order_id` จาก Bitkub ที่ไม่ใช่ตัวเลข:
  - เก็บ `order_id` ดิบไว้ใช้ใน notify/result
  - insert DB เป็น `NULL` ใน `purchase_history.order_id` เมื่อ parse เป็น int ไม่ได้ เพื่อกัน insert พัง
- เพิ่มการดึงราคา fill ให้ตรง exchange มากขึ้น:
  - เพิ่ม `BitkubAdapter.get_order_execution_symbol(...)` (ใช้ `/api/v3/market/order-info`)
  - ใน `purchase_on_exchange` จะ “ใช้ order-info ก่อน” (qty/avg_price/quote_spent/fee)
  - ใช้ balance-delta fallback เฉพาะตอน order-info ใช้ไม่ได้

Validated outcomes
- รอบ schedule `#37` (15 THB): trigger สำเร็จ, ซื้อจริง, LINE Flex ส่งสำเร็จ
- รอบ schedule `#39` (17 THB): trigger สำเร็จ และ log ยืนยันใช้ `Bitkub fill from order-info`
- ราคา/qty ใน LINE Flex ตรงกับข้อความจาก Bitkub มากขึ้นอย่างชัดเจน
- DB ยืนยันมี record ใน `purchase_history` สำหรับรอบที่ execute สำเร็จ

Issue discovered during monitoring
- พบเคส schedule พลาดรอบ (`#38 @ 13:25`) แม้ config ถูกต้อง
- สาเหตุ: job ตรวจทุก 30 วินาที แต่เงื่อนไข match อนุญาต time diff แค่ 15 วินาที
  - ตัวตรวจวิ่งที่วินาที `:22` และ `:52` จึงอาจพลาดนาทีเป้าหมาย

Key learnings
- สำหรับ exchange ที่ตอบรับคำสั่งเร็ว ควรแยก “accepted order” ออกจาก “filled execution” และมี fallback ที่ deterministic
- การ monitor ต้องดูทั้ง 3 จุดพร้อมกัน: scheduler log, LINE delivery log, และ DB write
- Scheduler timing window ต้องสัมพันธ์กับ polling interval ไม่เช่นนั้นจะเกิด false-miss แม้ผู้ใช้ตั้งเวลาถูกต้อง

Follow-ups
- ปรับ schedule matching ให้ไม่พลาดนาที (เช่น minute-based match หรือขยาย tolerance)
- เพิ่ม metric/alert สำหรับ “matched but not persisted”, “persisted but notify failed”, และ “missed window”

2026-03-07 — OKX_PURE_DCA Skip Root Cause + Email Notify Rollout

What changed
- Implemented SMTP email sending in `notify.py` and wired email notifications for successful trade events (in addition to LINE):
  - `notify_weekly_dca_buy`
  - `notify_half_sell_executed`
  - `notify_reserve_buy_executed`
  - `notify_s4_dca_buy`
  - `notify_s4_rotation`
- Email is best-effort by design: failures do not break existing buy/sell flow or LINE notifications.
- Added `.env.example` keys for email setup:
  - `EMAIL_NOTIFICATIONS_ENABLED`, `TRADE_NOTIFY_EMAIL`
  - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`
  - `SMTP_USE_TLS`, `SMTP_USE_SSL`, `EMAIL_FROM`
- Patched `send_email_notification()` fallback recipient lookup to include `TRADE_NOTIFY_EMAIL` (not just `EMAIL_TO`), fixing direct test call behavior.

Validation (email)
- Real send test succeeded: `EMAIL_TEST_RESULT True` after `.env` SMTP config was added.
- Compile/smoke checks passed; scheduler process restarted and running.

Operational issue investigated (OKX_PURE_DCA)
- User reported repeated misses for schedule `#46`:
  - `19:00`, `friday`, `10.00 USDT`, `okx_pure_dca`, active.
- Findings from DB/logs:
  - Scheduler matched at run time (`Matched schedule ID 46 at 19:00`).
  - No `purchase_history` row for `schedule_id=46`.
  - `action_dedupe` key was claimed for that run.
  - New detailed log showed explicit guard reason:
    - `DCA buy liquidity block (depth) ... reason=depth_insufficient`
    - detail included `min_notional` (~240,840) below threshold (1,000,000).

Root cause
- `okx_pure_dca` was routed through `purchase_on_exchange()` using the same liquidity guards as normal modes.
- This conflicted with expected semantics of “pure DCA buy every slot (ignore CDC-style gating)”.

Fix applied (targeted, minimal impact)
- In `purchase_on_exchange()` added conditional guard bypass when context marks mode as `okx_pure_dca`:
  - Bypass `depth_guard`, `twap_guard`, `notional_cap` only for this mode.
  - Keep existing behavior for all other modes unchanged.
- Added explicit warning logs for bypass decisions to preserve traceability.

Relevant code refs
- `main.py`: guard bypass gate + logs around lines `2166`, `2188`, `2213`, `2233`.
- `main.py`: detailed block reason logs added earlier around lines `2182`, `2202`, `2217`.
- `notify.py`: SMTP implementation + trade email hooks + recipient fallback.

Validation (OKX_PURE_DCA fix)
- Mock smoke test confirmed:
  - `okx_pure_dca` executes even when depth guard returns false.
  - normal `okx` mode still skips with `depth_insufficient` (unchanged behavior).
- Scheduler restarted after patch; `main.py` and `app.py` running.

Known environment context
- LINE channel frequently returns HTTP 429 (monthly limit reached), so relying on email notifications is now important for trade alerts.

Next session checklist
1. Monitor next `okx_pure_dca` schedule run and verify:
   - `purchase_history` insert present
   - LINE path may still 429, but email should send
   - log should show either executed order or explicit exchange-side failure reason
2. If exchange-side rejections appear (not guard-related), inspect OKX min notional / balance / API errors.
3. Consider documenting mode semantics in UI help text to clarify guard behavior differences.
