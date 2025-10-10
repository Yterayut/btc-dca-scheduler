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
