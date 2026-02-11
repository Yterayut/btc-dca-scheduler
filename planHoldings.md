## แผนเพิ่มยอดคงเหลือ (Holdings) ต่อกลยุทธ์

### 1. ขอบเขตงาน
- แสดงยอดคงเหลือของสินทรัพย์หลัก (BTC, XAUT ฯลฯ) สำหรับแต่ละกลยุทธ์ (เช่น CDC, S4) และแต่ละ exchange (Binance, OKX)
- รองรับการใช้งานทั้งในข้อความแจ้งเตือน (LINE) และหน้าเว็บแดชบอร์ด

### 2. ออกแบบบริการดึงยอดคงเหลือ
- สร้าง `services/balance_service.py`
  - ฟังก์ชัน `fetch_balances(exchanges: list[str], assets: list[str], cache_ttl: int = 30)`
  - ใช้ exchange adapter ปัจจุบัน (`BinanceAdapter`, `OkxAdapter`) เรียก `get_balance`
  - มี in-memory cache (dict + timestamp) เพื่อลดการเรียก API ซ้ำ
  - รองรับการรวมผลลัพธ์เป็นโครงสร้างเดียว เช่น
    ```python
    {
        "binance": {"BTC": {"free": 0.123, "locked": 0.002}},
        "okx": {"BTC": {...}, "XAUT": {...}}
    }
    ```
- จัดการ error / rate limit: หากดึงไม่สำเร็จให้คืนค่าล่าสุดพร้อม flag `stale` และเวลาที่ได้ข้อมูลครั้งสุดท้าย

### 3. การผสานกับกลยุทธ์
- **S4 (`run_s4_tick`, `execute_s4_dca`)**
  - เรียก balance service เพื่อให้ได้ BTC/XAUT ปัจจุบัน
  - เก็บลง `metadata['runtime']['holdings'] = {...}`
  - ส่งต่อไปกับ payload ของ `notify_s4_dca_buy` เพื่อให้ข้อความ LINE มีข้อมูล holdings
- **CDC / Weekly DCA (`run_loop_scheduler`)**
  - ก่อนส่ง LINE หรือบันทึก log ให้เรียก balance service สำหรับสินทรัพย์ที่ใช้ซื้อ (BTC)
  - แนบข้อมูลลง notification และ/หรือ dashboard API

### 4. การแสดงผล
- **LINE Notify**
  - เพิ่มบรรทัดใน `notify_s4_dca_buy` และ `notify_weekly_dca_buy` เช่น
    ```
    Holdings: BTC 0.145200 | XAUT 0.018430
    ```
  - หากข้อมูล stale ให้ใส่ `(cached 45s)` ต่อท้าย
- **Web UI**
  - เพิ่ม endpoint JSON (`/api/strategy_holdings`) ที่อ่านจาก balance service
  - ฝั่ง frontend แสดงใน widget ของแต่ละกลยุทธ์, อัปเดตตาม polling เดิมหรือผ่าน Socket.IO

### 5. ทดสอบและตรวจสอบ
- Unit test สำหรับ balance service (mock adapter)
- Integration test เบื้องต้น: รัน scheduler ใน dry-run mode และตรวจสอบ log/notification
- ตรวจสอบ rate limit และ latency ของ OKX/Binance หลังเปิดใช้งานจริง

### 6. งานถัดไป (เมื่อกลับมาทำ)
1. สร้าง balance service + cache
2. เชื่อมต่อ S4 runtime → ส่ง holdings ไปที่ notification
3. เชื่อมต่อ CDC runtime → รวม holdings ในการแจ้งเตือน
4. เปิด endpoint สำหรับ UI (ถ้าต้องการ)
5. เพิ่มการทดสอบและเอกสารประกอบ

### 7. รายละเอียดไฟล์และจุดเชื่อมสำคัญ
- `services/balance_service.py` (ไฟล์ใหม่): โครงร่าง class-less module + in-memory cache (`_CACHE: dict[tuple[str, ...], CachedItem]`)
- `exchanges/__init__.py` หรือ `exchanges/factory.py`: เพิ่ม helper คืน adapter map เพื่อใช้ใน balance service
- `main.py`: จุดเรียก S4 (`execute_s4_dca`) + CDC (`run_loop_scheduler`) เพิ่มโหลด holdings และแนบใน metadata/log
- `notifications/line.py`: ปรับ `notify_s4_dca_buy`, `notify_weekly_dca_buy` รับพารามิเตอร์ holdings และจัด format
- `app.py` หรือ `routes/api.py`: สร้าง endpoint `/api/strategy_holdings` (ใช้ balance service ที่ cache ไว้)
- `templates/index.html` + JS: เพิ่ม widget/section แสดง holdings ต่อ strategy พร้อม fallback เมื่อ stale

### 8. การตั้งค่าและ deployment
- เพิ่ม environment vars หากต้องการ override `BALANCE_CACHE_TTL` หรือ timeout ต่อ exchange
- ตรวจสอบ `requirements.txt` ว่าไม่ต้องเพิ่ม lib เพิ่มเติม (ใช้โค้ดเดิมของ adapter)
- หากใช้งานใน production ให้ตั้ง cron/healthcheck ตรวจสอบว่า balance service ไม่โยน exception ต่อเนื่อง
- เตรียม toggle (`ENABLE_STRATEGY_HOLDINGS`) เพื่อเปิดใช้ UI/api ทีละส่วนได้ หากต้องการ rollout แบบค่อยเป็นค่อยไป

### 9. Checklist ก่อนปิดงาน
- Tests: unit (balance_service), integration (scheduler dry-run + endpoint)
- Docs: อัปเดต `memory.md` หรือ README ส่วน Notification/UI
- Observability: เพิ่ม log debug เมื่อ cache miss/hit เพื่อตรวจสอบภายหลัง
- Verification: ทดลองแจ้งเตือนจริงใน dry-run และเช็กว่า UI แสดงค่าที่ตรงกับ API

### 10. สถานะล่าสุด (2025-10-18)
- ✅ ข้อ 1-2: balance service + cache เสร็จพร้อม unit test / notify render; S4 runtime ใช้ holdings แล้ว
- ✅ ข้อ 3-4: CDC weekly buy/skip แนบ holdings, เปิด `/api/strategy_holdings` และแดชบอร์ดมี widget อัปเดตทุก ~45s
- ✅ ข้อ 5: เติม unit tests (`tests/test_balance_service.py`, `tests/test_notify_holdings.py`) และสรุปไว้ใน `memory.md`
- ℹ️ รอ integration test เต็มระบบ + ตรวจสอบ rate limit จาก production หลัง deploy
