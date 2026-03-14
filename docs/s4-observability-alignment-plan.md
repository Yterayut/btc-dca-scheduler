# S4 Observability Alignment Plan
Date: 2026-03-12  
Status: Implemented (Phase A/B/C)

## Goal
ทำให้ `Daily Log (EOD analytics)` และ `Daily Heartbeat (runtime/production)` อ่านร่วมกันได้โดยไม่สับสน และตรวจจับ mismatch ได้อัตโนมัติ

## Current Issue
- EOD log อาจรายงาน `cdc_status=up` ที่ `asof_date` ย้อนหลัง (lag 1-2 วัน)
- Runtime รายงาน `cdc_status=down` แบบ near-real-time
- ผู้ใช้อ่านแล้วเข้าใจว่า logic ขัดกัน ทั้งที่จริงเป็นคนละ timestamp/data layer

## Target Outcomes
1. ทุกหน้ารายงานต้องระบุ `source layer` + `source timestamp` ชัดเจน
2. มี field `why_not_flip` ที่อ่านได้ทันทีว่า gate ไหน block
3. มี alert เมื่อ EOD vs runtime ขัดกันเกิน threshold ที่กำหนด
4. 90-day review ทำได้จากหน้า dashboard/API โดยไม่ query DB manual

## Scope
### In Scope
- Dashboard `/s4`
- API `/api/s4_shadow_swaps`
- EOD diagnostics (`s4_neutral_zone_eod`)
- Runtime metadata (`strategy_state.metadata_json.runtime`)

### Out of Scope
- เปลี่ยนกลยุทธ์ execution (ยังคง DCA-first + shadow log)
- เปลี่ยน CDC core algorithm

## Workstreams
## 1) Data Contract Alignment
เพิ่ม schema มาตรฐานสำหรับทั้งสองชั้น:

### EOD Analytics Fields (required)
- `layer`: `eod_analytics`
- `asof_date`
- `snapshot_ts_utc`
- `cdc_status_eod`
- `neutral_state_eod`
- `slope_pct_eod`
- `gap_pct_eod`
- `eod_lag_days`

### Runtime Fields (required)
- `layer`: `runtime_production`
- `runtime_ts_utc`
- `cdc_status_runtime`
- `active_asset_runtime`
- `signal_source_runtime`
- `last_confirmed_status`
- `confirm_progress`

## 2) Decision Transparency
เพิ่ม decision explanation ใน heartbeat/shadow:
- `decision`: `HOLD|SWAP_TO_BTC|SWAP_TO_XAU`
- `reason`: `gate_*`
- `next_unlock_condition` (string)
- `next_unlock_min_days` (int)

ตัวอย่าง:
- `reason=gate_cdc_up_required`
- `next_unlock_condition=cdc_status must be up for 3 consecutive days`
- `next_unlock_min_days=3`

## 3) Mismatch Detection
เพิ่ม logic ตรวจ mismatch รายวัน:
- `analytics_runtime_mismatch = (cdc_status_eod != cdc_status_runtime)`
- บันทึก severity:
  - `info`: mismatch และ `eod_lag_days > 0`
  - `warn`: mismatch ต่อเนื่อง >= 2 วัน
  - `critical`: mismatch ต่อเนื่อง >= 5 วัน

## 4) Dashboard UX
ใน `/s4` เพิ่ม section:
- `Signal Layers`
  - EOD analytics snapshot
  - Runtime snapshot
  - mismatch badge (`MATCH` / `MISMATCH`)
- `Why Not Flip`
  - gate reason
  - unlock condition
  - days since last swap / cooldown

## 5) API Enhancements
ขยาย `/api/s4_shadow_swaps`:
- query params:
  - `reason=heartbeat|plan|all`
  - `decision=HOLD|SWAP_TO_BTC|SWAP_TO_XAU`
  - `include_mismatch=true|false`
- response adds:
  - `analytics_runtime_mismatch`
  - `eod_asof_date`
  - `runtime_signal_ts`

## 6) Implementation Checklist
## Phase A (Quick Win, 1-2 days)
1. `main.py`: add `next_unlock_condition` + `next_unlock_min_days` into heartbeat metadata.
2. `app.py`: enrich `/api/s4_shadow_swaps` with layer/timestamp fields.
3. `app.py`: compute `analytics_runtime_mismatch` from latest EOD + runtime.
4. `templates/s4_status.html`: add `Signal Layers` card + `MATCH/MISMATCH` badge.
5. `templates/s4_status.html`: add `Why Not Flip` details from latest heartbeat gate.

## Phase B (Stability, 2-3 days)
1. Persist mismatch streak counter in runtime metadata.
2. Add warning/critical alerting thresholds with throttling.
3. Add API filters:
   - `reason=heartbeat|plan|all`
   - `decision=HOLD|SWAP_TO_BTC|SWAP_TO_XAU`
   - `include_mismatch=true|false`

## Phase C (Review, 1 day)
1. Build 30/60/90-day summary endpoint or report script.
2. Add operator checklist: explain current state in under 2 minutes.

## Deliverables
1. Dashboard shows both layers with timestamps and mismatch badge.
2. API returns gate reasoning + unlock condition.
3. Daily mismatch streak is visible and alertable.
4. 90-day review can be done without direct SQL.

## Acceptance Criteria
1. ผู้ใช้เห็นได้ทันทีว่า EOD กับ runtime ใช้คนละ snapshot เวลาใด
2. เมื่อ CDC ไม่ตรงกัน ระบบอธิบายได้ว่าเพราะ lag หรือเพราะ logic divergence
3. `/api/s4_shadow_swaps` filter ได้ตาม reason/decision
4. มี mismatch alert เมื่อ mismatch ต่อเนื่องถึงเกณฑ์

## Risks & Mitigations
- Risk: เพิ่ม field แล้ว dashboard ซับซ้อนเกินไป  
  Mitigation: default แสดงเฉพาะ summary + expandable detail
- Risk: mismatch alerts รบกวนมากเกิน  
  Mitigation: throttle alerts และใช้ severity ladder

## Implementation Notes
- Keep execution unchanged: `S4_SWAP_EXEC_ENABLED=false`
- Keep logging enabled: `S4_SHADOW_SWAP_LOG_ENABLED=true`
- ใช้ UTC เป็น canonical timestamp แล้วค่อย format ใน UI

## Recommended Next Action
เริ่ม Phase A ทันที: เพิ่ม `Signal Layers + mismatch badge + unlock condition` บน `/s4` ก่อน เพราะแก้ pain point ได้เร็วที่สุด

## Implementation Result (2026-03-12)
- `main.py`
  - เพิ่ม `next_unlock_condition` และ `next_unlock_min_days` ใน shadow heartbeat gate
  - เพิ่ม mismatch tracking (`analytics_runtime_mismatch`, `mismatch_streak_days`, `mismatch_severity`)
  - เพิ่ม throttled security alert เมื่อ mismatch ต่อเนื่องระดับ `warn/critical`
- `app.py`
  - `/s4` data model รองรับ `signal_layers` และ `why_not_flip`
  - `/api/s4_shadow_swaps` รองรับ filter: `reason`, `decision`, `include_mismatch`
  - เพิ่ม endpoint summary: `/api/s4_shadow_swaps_summary` (30/60/90-day)
- `templates/s4_status.html`
  - เพิ่มการ์ด `Signal Layers` พร้อม badge `MATCH/MISMATCH`
  - เพิ่มส่วน `Why Not Flip` พร้อม unlock condition/min days
  - เพิ่มคอลัมน์ diagnostics ในตาราง Shadow Swap
- Tests
  - เพิ่ม unit tests: `tests/test_s4_observability.py`
  - รันชุดทดสอบ S4 ที่เกี่ยวข้องผ่านครบ

## Operational Note
- การเปลี่ยนแปลงใน `main.py` และ `app.py` ต้อง restart ทั้ง scheduler และ web process เพื่อโหลดโค้ดใหม่
