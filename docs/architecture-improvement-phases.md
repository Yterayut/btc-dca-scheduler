# Architecture Improvement Plan

Date: 2026-03-14

## Objective

เอกสารนี้สรุปผลสำรวจ codebase ปัจจุบันและแปลงเป็นแผนปรับปรุงแบบเป็น phase เพื่อให้ refactor ต่อได้โดยไม่เสีย momentum ของระบบ DCA/S4 ที่กำลังใช้งานอยู่

## Survey Summary

### Current shape

- `main.py` มีขนาด `3875` บรรทัด และยังเป็นศูนย์รวมของ runtime engine, scheduler, CDC/S4 domain logic, liquidity guards, exchange execution orchestration, DB helpers บางส่วน, notification wiring, และ health check
- `app.py` มีขนาด `4869` บรรทัด และยังรวม Flask routes, JSON shaping, admin/dashboard logic, strategy configuration, wallet/compliance exports, runtime flag logic, และการ import runtime functions จาก `main.py`
- service layer เริ่มถูกแยกออกแล้วและมีทิศทางดี:
  - `services/bootstrap.py`
  - `services/db.py`
  - `services/state.py`
  - `services/schedule_context.py`
  - `services/pnl.py`
  - `services/trading.py`
- exchange adapters ถูกแยกไว้พอสมควร แต่ `BitkubAdapter` และ `OkxAdapter` ยังมี runtime complexity สูง
- tests ครอบจุดสำคัญของ CDC/S4/observability/runtime บางส่วนแล้ว แต่ยังเน้น integration-style patching ผ่าน `main` และ `app`

### Key observations

1. `main.py` ยังเป็น orchestration monolith
   - มีทั้ง pure helpers, DB wrappers, runtime policy, execution path, CDC/S4 business rules และ infra side effects อยู่ในไฟล์เดียว
   - แม้จะแยก helper ออกมาแล้วหลายก้อน แต่ orchestration ระดับบนยังหนาแน่น

2. `app.py` ยังเป็น web monolith
   - route handlers จำนวนมากทำทั้ง validation, query DB, shape response, และ business logic ในตัว
   - มี coupling ข้ามไป `main.py` โดยตรง เช่น `from main import increment_reserve, increment_reserve_exchange`

3. runtime กับ web layer ยังพึ่งกันแน่น
   - `app.py` import จาก `main.py`
   - tests หลายไฟล์ patch ผ่าน `main` หรือ import `app` ทั้งก้อน
   - ผลคือ boundary ระหว่าง execution engine กับ web/admin surface ยังไม่ชัด

4. เอกสารเริ่มดีขึ้น แต่ docs หลักยังไม่สะท้อนระบบจริง
   - `README.md` ยังสั้นและยังไม่อธิบาย architecture, strategy modes, runtime services, migration path, หรือวิธีรัน test suite
   - codebase มี plan/result docs เยอะ แต่ยังไม่มี “single source of truth” สำหรับ architecture ปัจจุบัน

5. observability ฝั่ง Bitkub ยังบางกว่าฝั่ง CDC/OKX
   - มี logic fallback ที่ดีใน `services/trading.py`
   - แต่ runtime marker สำคัญสำหรับ `order_info`, `order_history`, และ balance-delta fallback ยังไม่เด่นพอสำหรับ production monitoring

6. service extraction ยังไม่เสร็จ
   - `services/trading.py` เริ่มเป็น execution service แล้ว
   - แต่ reserve-buy path และ orchestration ชั้นบนยังอยู่ใน `main.py`

7. data access pattern ยังไม่เป็น repository layer ชัดเจน
   - `services/state.py` และ `services/db.py` ดีขึ้นแล้ว
   - แต่ SQL ยังกระจายอยู่ในหลายจุดของ `main.py`, `app.py`, และ helpers

### File-size hotspots

- `app.py`: 4869 lines
- `main.py`: 3875 lines
- `notify.py`: 1830 lines
- `services/trading.py`: 611 lines
- `exchanges/okx.py`: 524 lines
- `exchanges/bitkub.py`: 320 lines
- `strategies/cdc.py`: 306 lines
- `strategies/s4_utils.py`: 298 lines

## Main Improvement Targets

### A. Runtime decomposition

เป้าหมายคือทำให้ `main.py` เหลือหน้าที่เป็น composition root และ scheduler entrypoint มากกว่าจะเป็นที่อยู่ของ logic ทั้งระบบ

### B. Web/API decomposition

เป้าหมายคือทำให้ `app.py` เหลือ Flask app assembly, routing registration, และ response glue โดยย้าย business/data shaping ออกไป

### C. Observability and runtime safety

เป้าหมายคือทำให้ execution paths โดยเฉพาะ Bitkub และ reserve flows monitor ได้ชัดขึ้น และตรวจ production issues ได้จาก logs/events โดยไม่ต้องเดา

### D. Documentation and operating model

เป้าหมายคือทำให้ developer คนถัดไปเข้าใจ architecture ปัจจุบัน, วิธีทดสอบ, และขั้นตอน rollout/refactor ได้จาก docs เดียว

## Phased Roadmap

## Phase 0: Baseline and Guardrails

### Goal

ตรึง baseline ให้ปลอดภัยก่อน refactor ก้อนใหญ่ต่อ

### Work

- เพิ่ม architecture snapshot ใน docs ให้ชัดว่าตอนนี้ service ไหนรับผิดชอบอะไร
- เพิ่ม test command matrix สำหรับ:
  - runtime core
  - web/API
  - notify/flex
  - S4/observability
- กำหนด import rule ชั่วคราว:
  - `app.py` ไม่ควรเพิ่ม dependency ใหม่ไปหา execution internals ใน `main.py`
  - services ใหม่ต้องไม่มี dependency ย้อนกลับไป `app.py`
- ระบุ log markers ที่ถือเป็น production-critical events

### Exit criteria

- มี architecture doc ปัจจุบัน 1 ฉบับ
- มี test matrix ใน docs
- มี runtime marker checklist สำหรับ DCA buy, reserve buy, half sell, Bitkub fill fallback

## Phase 1: Finish Runtime Extraction from `main.py`

### Goal

ลด `main.py` ให้เหลือ orchestration + wiring จริง ๆ

### Work

- ย้าย `execute_reserve_buy` ออกไป `services/trading.py` หรือ `services/reserve.py`
- ย้าย `execute_reserve_buy_exchange` ออกไป service เดียวกัน
- พิจารณาแยก liquidity/guard functions ไป `services/guards.py`
  - `assess_liquidity`
  - `evaluate_depth_guard`
  - `evaluate_twap_guard`
  - `evaluate_notional_cap`
- แยก scheduler-specific functions ไป `services/scheduler_runtime.py`
  - dedupe cleanup
  - scheduler lock
  - heartbeat helpers
- คง wrapper compatibility ใน `main.py` ชั่วคราวเพื่อลด test churn

### Why this phase first

- เป็น continuation ของ refactor ที่ทำมาแล้ว
- เสี่ยงต่ำกว่าการผ่า `app.py`
- จะลด coupling ของ tests กับ monolithic runtime ได้ชัดที่สุด

### Exit criteria

- `main.py` ไม่ถือ execution path หลักของ reserve/half-sell/buy อีกต่อไป
- guard logic ถูกย้ายเป็นกลุ่มชัดเจน
- tests เดิมยังผ่านโดยไม่ต้อง rewrite ใหญ่

## Phase 2: Split Web Layer out of `app.py`

### Goal

แยก web concerns ออกจาก business/data logic

### Work

- แยก route groups เป็น blueprint/module ตาม domain:
  - `routes/admin.py`
  - `routes/strategy.py`
  - `routes/s4.py`
  - `routes/exports.py`
  - `routes/health.py`
- ย้าย response shaping และ data assembly ออกจาก route handler ไป service/presenter layer
- ย้าย `_build_s4_status_data()` ไป service ที่ dedicated
- ย้าย DB query helpers ใน `app.py` ไป repository/service ที่ชัดเจน
- ลด `from main import ...` ให้เหลือผ่าน service interface กลาง

### Risk

- route handlers ใน `app.py` เยอะมากและกระจายหลาย concern
- ถ้าแยกไม่เป็นก้อนอาจทำให้ regression สูง

### Exit criteria

- `app.py` เหลือ app/bootstrap/blueprint registration เป็นหลัก
- ไม่มี route ขนาดใหญ่ที่ทำทั้ง query + business rule + response formatting ในฟังก์ชันเดียว
- dependency จาก web ไป runtime ผ่าน service layer มากกว่าผ่าน `main.py`

## Phase 3: Repository and Persistence Cleanup

### Goal

ทำให้ data access pattern เสถียรและ test ได้ดีขึ้น

### Work

- สร้าง repository layer สำหรับตารางหลัก:
  - `strategy_state`
  - `purchase_history`
  - `sell_history`
  - `reserve_log`
  - `strategy_fee_totals`
  - `strategy_rotation_log`
- แยก SQL ที่ซ้ำหรือกระจายอยู่ใน `main.py` และ `app.py`
- กำหนด convention สำหรับ transaction boundary
- เพิ่ม lightweight DTO/typed dict สำหรับ state records และ event payloads

### Expected benefit

- ลด SQL scatter
- ลด duplicated cursor boilerplate
- ทำให้ test mock ได้ในระดับ repository แทน patch runtime ทั้งก้อน

### Exit criteria

- SQL write path หลักอยู่ใน repository/services ชัดเจน
- route handlers และ runtime orchestration ไม่เขียน SQL โดยตรงเกินจำเป็น

## Phase 4: Notification and Presentation Cleanup

### Goal

ลดความซับซ้อนใน `notify.py` และทำให้ channel formatting ทดสอบง่ายขึ้น

### Work

- แยก `notify.py` ออกเป็น:
  - channel transport
  - payload builders
  - strategy/event-specific message formatters
- ทำ notification schema กลางสำหรับ event ประเภท:
  - weekly DCA buy
  - reserve buy
  - half sell
  - liquidity block
  - S4 rotation
  - security alert
- ลด string formatting logic แบบกระจาย
- ให้ Flex message builders ใช้ structure กลางมากขึ้น

### Exit criteria

- `notify.py` ไม่เป็น mixed transport + formatting monolith
- tests แยกตรวจ formatter ได้โดยไม่ต้องแตะ network code

## Phase 5: Exchange Reliability and Observability

### Goal

ทำให้ runtime behavior ฝั่ง exchange โดยเฉพาะ Bitkub และ OKX debug ได้จาก telemetry

### Work

- เพิ่ม structured log markers สำหรับ:
  - Bitkub `order_info` success/failure
  - Bitkub `order_history` success/failure
  - Bitkub balance-delta fallback hit
  - reserve-buy skipped/executed reasons
  - half-sell skipped/executed reasons
- ทำ event payload schema ให้สม่ำเสมอ across exchanges
- เพิ่ม tests เฉพาะ path ที่ยังบาง:
  - reserve buy success/skip matrix
  - Bitkub fill resolution matrix
  - OKX/Bitkub fee normalization
- พิจารณา retry/backoff policy ให้ชัดใน adapters ที่ critical

### Exit criteria

- production issue trace ได้จาก log/event โดยไม่ต้องอ่านหลายไฟล์
- มี test ครอบ exchange-specific fallbacks ที่สำคัญ

## Phase 6: Strategy Boundary Cleanup

### Goal

ทำให้ CDC และ S4 เป็น domain modules ที่ชัด ไม่อิง runtime monolith เกินไป

### Work

- ค่อย ๆ ย้าย CDC transition/weekly orchestration ให้ใช้ `strategies/cdc.py` เป็น primary domain source
- ลด logic S4 ที่ฝังใน `main.py` โดยแยกไป `strategies/s4.py` หรือ service layer เฉพาะ
- ทำ strategy execution contract ให้สม่ำเสมอขึ้นระหว่าง:
  - decision
  - action
  - execution
  - observability

### Exit criteria

- business decisions อยู่ใน strategy/domain modules มากกว่า runtime entry file
- `main.py` ไม่ถือ domain rules รายละเอียดของ S4/CDC จำนวนมาก

## Phase 7: Documentation and Developer Experience

### Goal

ทำให้ onboarding และ maintenance เร็วขึ้น

### Work

- อัปเดต `README.md` ให้ครอบ:
  - architecture overview
  - local setup
  - runtime modes
  - test commands
  - exchange support matrix
- สร้าง `docs/architecture-current.md`
- สร้าง `docs/testing-guide.md`
- สร้าง `docs/runtime-operations.md`
- สรุป migration path จาก monolith เดิมไป service-oriented structure

### Exit criteria

- คนใหม่อ่าน docs หลักแล้วรันระบบและเข้าใจ flow ได้
- docs กลายเป็น source of truth มากกว่าโน้ตกระจาย

## Recommended Execution Order

1. Phase 0
2. Phase 1
3. Phase 5
4. Phase 2
5. Phase 3
6. Phase 4
7. Phase 6
8. Phase 7

เหตุผล:

- ควรปิด runtime extraction และ observability ก่อน เพราะกระทบ production behavior โดยตรง
- ควรผ่า `app.py` หลัง runtime boundary ชัดขึ้นแล้ว
- documentation ใหญ่ควรทำหลัง shape ใหม่เริ่มนิ่ง ไม่เช่นนั้น docs จะ stale เร็ว

## Concrete Next Actions

### Next 3 refactors

1. ย้าย `execute_reserve_buy` ออกจาก `main.py`
2. ย้าย `execute_reserve_buy_exchange` ออกจาก `main.py`
3. ย้าย liquidity/guard helpers ไป service ใหม่

### Next 3 test improvements

1. เพิ่ม tests สำหรับ reserve-buy skip/execute matrix
2. เพิ่ม tests สำหรับ Bitkub fill resolution paths ทุกแบบ
3. เพิ่ม tests สำหรับ route/service boundary ของ S4 status data

### Next 3 docs improvements

1. อัปเดต `README.md`
2. เพิ่ม `docs/testing-guide.md`
3. เพิ่ม `docs/runtime-operations.md`

## Non-goals

- ไม่ rewrite ระบบทั้งหมดในครั้งเดียว
- ไม่เปลี่ยน strategy behavior พร้อมกับ refactor structure ใน PR เดียว
- ไม่รีบเปลี่ยน tests ทั้งหมดจาก patch-based approach จนกว่า service boundaries จะนิ่ง

## Success Metrics

- `main.py` ต่ำกว่า 2500 lines
- `app.py` ต่ำกว่า 2500 lines
- execution paths หลักอยู่ใน services/repositories แยกชัด
- route handlers ส่วนใหญ่ยาวไม่เกิน 100-150 lines
- มี test coverage สำหรับ exchange fallback และ reserve/half-sell paths ครบกว่าปัจจุบัน
- docs หลักสะท้อน architecture จริง

## Suggested Tracking Format

ใช้ issue หรือ checklist แยกตาม phase โดยแต่ละ item ต้องระบุ:

- scope
- files touched
- regression risk
- tests required
- rollback plan

เอกสารนี้ควรใช้เป็น roadmap ระดับสถาปัตยกรรม ไม่ใช่เป็นรายการ commit ย่อยแบบบรรทัดต่อบรรทัด
