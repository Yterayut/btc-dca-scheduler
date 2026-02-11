# CDC / S4 Strategy Master Plan

## ✅ เสร็จแล้ว
- สร้างเลเยอร์กลยุทธ์ + orchestrator พร้อม idempotency
- รีแฟกเตอร์ CDC weekly DCA / transition และ admin reserve API ให้วิ่งผ่าน orchestrator
- เพิ่ม liquidity guard (spread check) ก่อน half-sell / reserve buy พร้อมแจ้งเตือน
- ยกเครื่องระบบแจ้งเตือน (LINE) ให้ออกในรูปแบบมาตรฐานพร้อม request/dedupe/CDC/time
- ร่าง schema/migration ใหม่และเตรียม staging rehearsal checklist
- ปรับ UI/UX กลยุทธ์: selector, allocation cards, help overlay และ log filter (templates/index.html)
- เพิ่ม depth / TWAP / notional cap guards ที่ reusable ผ่าน adapter layer (main.py, exchanges/*)
- Compliance & reporting: `compliance_audit_log`, `/api/compliance_events`, CSV export + UI การ์ดใหม่
- Security & key rotation: metadata encryption (`security_utils.py`), anomaly alert (`notify_security_alert`), script `scripts/rotate_encryption_key.py`
- Deployment plan พร้อม canary + feature flag S4 + rollback (docs/DEPLOYMENT_PLAN.md)
- UI notification/log integration: security/compliance log filter + table + LINE alerts
- Backtest & chaos tests: synthetic backtest harness (`scripts/backtest_cdc.py`) และ unit tests (`tests/test_guards.py`, orchestrator failure case)
- พัฒนา S4 BTC↔GOLD overlay: engine + rotation log + UI runtime พร้อม dry-run test (`strategies/s4.py`, `main.py`, `templates/index.html`, `tests/test_s4_strategy.py`)

## ⏳ ยังไม่ทำ
- (none)
