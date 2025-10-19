Project Memory — BTC DCA Dashboard

Updated: 2025-09-19

Summary
- Stack: Python 3 + Flask + Flask-SocketIO + MySQL + Binance SDK.
- Entrypoint: app.py (serves UI and JSON APIs on port 5001).
- Templates: templates/index.html (new UI), templates/admin.html.
- Scheduler/worker: main.py (trading engine, health server via env HEALTH_CHECK_PORT).

Recent Fixes (this session)
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
- sell_percent: 50
- last_cdc_status: up

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
  - OKX live: `OKX_LIVE_ENABLED=1`; per‑order cap from DB `okx_max_usdt` (default 10.00).
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
