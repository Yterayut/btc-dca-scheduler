# Current Architecture Snapshot

Date: 2026-03-14

## Purpose

เอกสารนี้สรุปสถาปัตยกรรมปัจจุบันของระบบในรูปที่ใช้งานได้ทันทีสำหรับ refactor และ review โดยไม่พยายาม idealize โครงสร้างเกินจริง

## High-Level Shape

ระบบปัจจุบันแบ่งออกเป็น 5 ชั้นหลัก

1. Runtime engine
   - [main.py](/home/oneclimate-dev/yterayut-project/DCA/main.py)
   - รับผิดชอบ scheduler/runtime orchestration, CDC/S4 execution paths, guards, health checks, และ wiring ข้าม service

2. Web/admin surface
   - [app.py](/home/oneclimate-dev/yterayut-project/DCA/app.py)
   - รับผิดชอบ Flask routes, dashboard/admin APIs, status pages, exports, และการเรียก runtime helpers บางส่วน

3. Domain strategies
   - [strategies/cdc.py](/home/oneclimate-dev/yterayut-project/DCA/strategies/cdc.py)
   - [strategies/s4.py](/home/oneclimate-dev/yterayut-project/DCA/strategies/s4.py)
   - [strategies/s4_utils.py](/home/oneclimate-dev/yterayut-project/DCA/strategies/s4_utils.py)
   - [strategies/s4_observability.py](/home/oneclimate-dev/yterayut-project/DCA/strategies/s4_observability.py)

4. Infrastructure/services
   - [services/bootstrap.py](/home/oneclimate-dev/yterayut-project/DCA/services/bootstrap.py)
   - [services/db.py](/home/oneclimate-dev/yterayut-project/DCA/services/db.py)
   - [services/state.py](/home/oneclimate-dev/yterayut-project/DCA/services/state.py)
   - [services/schedule_context.py](/home/oneclimate-dev/yterayut-project/DCA/services/schedule_context.py)
   - [services/pnl.py](/home/oneclimate-dev/yterayut-project/DCA/services/pnl.py)
   - [services/trading.py](/home/oneclimate-dev/yterayut-project/DCA/services/trading.py)
   - [services/balance_service.py](/home/oneclimate-dev/yterayut-project/DCA/services/balance_service.py)

5. Exchange and notification adapters
   - [exchanges/binance.py](/home/oneclimate-dev/yterayut-project/DCA/exchanges/binance.py)
   - [exchanges/okx.py](/home/oneclimate-dev/yterayut-project/DCA/exchanges/okx.py)
   - [exchanges/bitkub.py](/home/oneclimate-dev/yterayut-project/DCA/exchanges/bitkub.py)
   - [notify.py](/home/oneclimate-dev/yterayut-project/DCA/notify.py)
   - [notifications/line_flex.py](/home/oneclimate-dev/yterayut-project/DCA/notifications/line_flex.py)

## Runtime Flow

### CDC / Weekly DCA

- scheduler ตัดสินใจ action ผ่าน strategy/runtime logic
- execution buy path หลักอยู่ใน [services/trading.py](/home/oneclimate-dev/yterayut-project/DCA/services/trading.py)
- `main.py` ยังเป็นจุดประกอบ dependency และ orchestration ชั้นบน
- DB writes หลักไปที่ `purchase_history`, `strategy_fee_totals`, และ compliance events

### CDC Transition

- เมื่อสถานะ CDC เปลี่ยน:
  - down: สร้าง half-sell actions
  - up: สร้าง reserve-buy actions
- half-sell execution logic จริงย้ายไป [services/trading.py](/home/oneclimate-dev/yterayut-project/DCA/services/trading.py) แล้ว
- reserve-buy execution ยังอยู่ใน [main.py](/home/oneclimate-dev/yterayut-project/DCA/main.py)

### S4

- signal/decision helpers อยู่ใน `strategies/s4*`
- status/dashboard data จำนวนหนึ่งยังถูกประกอบใน [app.py](/home/oneclimate-dev/yterayut-project/DCA/app.py)
- execution hardening และ observability บางส่วนยังผูกกับ runtime ใน [main.py](/home/oneclimate-dev/yterayut-project/DCA/main.py)

## Current Dependency Reality

### Stable directions

- services -> exchanges
- runtime -> services
- app -> services
- tests -> main/app/services

### Problematic directions

- [app.py](/home/oneclimate-dev/yterayut-project/DCA/app.py) -> [main.py](/home/oneclimate-dev/yterayut-project/DCA/main.py)
- tests หลายชุด patch ผ่าน `main` เป็นหลัก
- SQL ยังอยู่ทั้งใน `main.py`, `app.py`, และ service บางตัว

## Current Service Ownership

### bootstrap

- env flag parsing
- required env loading
- Binance client construction

### db

- DB connection factory
- transaction context

### state

- strategy state load/save
- metadata persistence
- fee totals
- rotation journaling

### schedule_context

- schedule context fetch สำหรับ runtime actions

### pnl

- FIFO open lots
- realized PnL computation

### trading

- reserve increment helpers
- purchase execution
- half-sell execution

### balance_service

- holdings fetch with short-lived cache

## Hotspots

- [app.py](/home/oneclimate-dev/yterayut-project/DCA/app.py): 4869 lines
- [main.py](/home/oneclimate-dev/yterayut-project/DCA/main.py): 3875 lines
- [notify.py](/home/oneclimate-dev/yterayut-project/DCA/notify.py): 1830 lines
- [services/trading.py](/home/oneclimate-dev/yterayut-project/DCA/services/trading.py): 611 lines

## Known Architectural Debt

1. Runtime orchestration ยังไม่แยกจาก execution/service layer สมบูรณ์
2. Web layer ยังรวม route, query, formatting, และ business logic
3. Notification layer ยังรวม transport กับ message formatting
4. Repository boundary ยังไม่ชัด
5. Observability ของ Bitkub ยังบางกว่าฝั่ง OKX/CDC

## Short-Term Rules

1. หลีกเลี่ยงเพิ่ม dependency ใหม่จาก [app.py](/home/oneclimate-dev/yterayut-project/DCA/app.py) ไป internal functions ใน [main.py](/home/oneclimate-dev/yterayut-project/DCA/main.py)
2. service ใหม่ต้องไม่ import กลับเข้า `app.py`
3. execution logic ใหม่ให้ลง services ก่อน ไม่ใส่ตรงใน `main.py`
4. route ใหม่ใน web layer ควรย้าย data assembly ออกไป helper/service ตั้งแต่แรก
