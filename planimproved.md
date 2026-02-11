# แผนพัฒนาระบบ BTC DCA (Plan Improved)

อัปเดตล่าสุด: 2025-12-12  
สถานะระบบ: **LIVE**  

เอกสารนี้สรุปแผนพัฒนาแบบเป็นเฟสเพื่อกลับมาทำต่อภายหลัง โดยเน้นความปลอดภัยต่อระบบ live และเปิดใช้งานทีละขั้นด้วย feature flags

---

## สรุปสิ่งที่ทำแล้ว (Phase 0 / Phase 1 / Phase 1.1)

### Phase 0 — Baseline & Safety Net (**เสร็จแล้ว**)
- เพิ่ม endpoint อ่านอย่างเดียว `/api/health` ใน `app.py`
  - คืน JSON สถานะ DB, scheduler, PID/uptime, flags สำคัญ (`dry_run`, `use_testnet`)
- ปรับ `scripts/dca_tool.py`
  - `scheduler status --verbose` แสดง diagnostics + flags
  - เตือนเมื่อ start ในโหมด LIVE (ไม่เปลี่ยนพฤติกรรม)

### Phase 1 — Single-Instance + Distributed Dedupe (**เสร็จแล้ว / เปิดใช้แล้ว**)
- **Scheduler DB lock** (กัน `main.py` รันซ้ำ)
  - ใช้ MySQL `GET_LOCK`
  - เปิดใช้ด้วย `SCHEDULER_DB_LOCK_ENABLED=1`
  - ตั้งชื่อ/timeout ผ่าน env
- **Distributed action dedupe** (กันสั่งซื้อ/แจ้งเตือนซ้ำ)
  - ตาราง `action_dedupe` สร้างอัตโนมัติเมื่อเปิด `DB_DEDUPE_ENABLED=1`
  - ก่อนทำ side‑effect ทุก action จะ claim `dedupe_key` ที่ DB
  - ถ้าซ้ำ → action ถูก SKIPPED และ log warning
- เพิ่ม log สำคัญ:
  - ตอน ensure table: `DB dedupe enabled: ensured action_dedupe table exists.`
  - ตอนโดน dedupe: `DB dedupe hit: skipping action ...`

### Phase 1.1 — Cleanup Dedupe Table (**เสร็จแล้ว / เปิดใช้แล้ว**)
- เพิ่ม cleanup job ใน `main.py` ลบแถวเก่ากว่า N วัน
  - ปลอดภัย ไม่ยุ่ง order path
  - เปิดใช้ด้วย `DEDUPE_CLEANUP_ENABLED=1`
  - Retention เริ่มต้น `DEDUPE_CLEANUP_DAYS=30`
  - Interval เริ่มต้น `DEDUPE_CLEANUP_INTERVAL_HOURS=6`
  - log เมื่อมีการลบ: `DB dedupe cleanup: deleted X rows older than 30 days.`

---

## Feature Flags ที่ใช้อยู่ตอนนี้

ใน `.env`:
- `SCHEDULER_DB_LOCK_ENABLED=1`
- `SCHEDULER_DB_LOCK_NAME=dca_scheduler`
- `SCHEDULER_DB_LOCK_TIMEOUT=1`
- `DB_DEDUPE_ENABLED=1`
- `DEDUPE_CLEANUP_ENABLED=1`
- `DEDUPE_CLEANUP_DAYS=30`
- `DEDUPE_CLEANUP_INTERVAL_HOURS=6`

Rollback เร็ว:
- ปิด dedupe: `DB_DEDUPE_ENABLED=0` แล้วรีสตาร์ท scheduler
- ปิด cleanup: `DEDUPE_CLEANUP_ENABLED=0` แล้วรีสตาร์ท scheduler
- ปิด lock: `SCHEDULER_DB_LOCK_ENABLED=0` แล้วรีสตาร์ท scheduler

---

## แผนงานถัดไป (ยังไม่ทำ)

### Phase 2 — แยก Execution Handlers ออกจาก `main.py` (1–2 สัปดาห์)
เป้าหมาย: ลดไฟล์ monolith, ลดความเสี่ยงเวลาแก้ logic live, เพิ่ม testability

งานย่อย:
1. สร้าง `services/action_handlers.py` หรือแยกเป็นไฟล์ตาม action type
2. ย้าย handler ทีละตัว:
   - `DCA_BUY`, `RESERVE_MOVE`, `HALF_SELL`, `RESERVE_BUY`, `ROTATION_FLIP`
3. เพิ่ม feature flag `HANDLERS_V2=0/1`
   - เริ่มจาก 0 (behavior เดิม)
4. เพิ่ม unit tests ครอบคลุม handler ที่ย้าย
5. เปิด `HANDLERS_V2=1` ใน dry‑run/staging ก่อน แล้วค่อยเปิด live

Acceptance:
- test suite ผ่าน
- dry‑run เปรียบเทียบ log/ผลลัพธ์ตรงกับเดิม

### Phase 3 — ทำ Config Source of Truth เดียว + Effective Config API (≈1 สัปดาห์)
เป้าหมาย: ลดความสับสน env vs DB, ให้ UI/engine เห็นค่าเดียวกัน

งานย่อย:
1. สร้าง `services/config_service.py` นิยาม precedence: DB override > env default
2. ย้าย default strategy metadata ออกจาก `app.py`
3. เพิ่ม `/api/config_effective` แสดงค่าที่ใช้จริง + source
4. feature flag `CONFIG_V2=0/1`

### Phase 4 — OKX Adapter Hardening + Paper/Staging (1–2 สัปดาห์)
เป้าหมาย: ทำให้ OKX live path มั่นใจเท่ากับ Binance

งานย่อย:
1. เพิ่ม integration tests แบบ simulated (`OKX_SIMULATED=1`)
2. unify live enable ผ่าน DB/strategy_state ไม่ใช่หลาย env
3. รัน paper/staging 1–2 สัปดาห์ก่อนเปิด live OKX เต็มรูปแบบ

### Phase 5 — Observability & Alerting (ต่อเนื่อง)
เป้าหมาย: ลด incident investigation แบบ manual

งานย่อย:
1. structured JSON logs พร้อม `request_id/dedupe_key/exchange/action_type`
2. metrics ง่าย ๆ: loop latency, last transition, failed action count
3. alert เมื่อพบ instance ซ้ำ/lock lost/DB lag

---

## แนวทาง rollout ทุกเฟส (มาตรฐานความปลอดภัย)
1. พัฒนา + unit tests  
2. รันใน dry‑run/staging  
3. เปิด feature flag แบบค่อยเป็นค่อยไป  
4. monitor 24–48 ชม.  
5. มี rollback path ชัดเจน (ปิด flag + รีสตาร์ท scheduler)

