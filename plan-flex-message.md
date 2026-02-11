# Flex Message Rollout Plan

## 1. เตรียมโครงสร้างพื้นฐาน
- [ ] กำหนดตัวแปรสภาพแวดล้อม `LINE_USE_FLEX=false` และ (ถ้าต้องการ) `LINE_FLEX_ALLOWLIST=` พร้อม wiring ใน loader/config
- [ ] สร้างโมดูล `notifications/line_flex.py` สำหรับ helper, builder (bubble/alt text), คอนสแตนต์สี/ไอคอน
- [ ] จัดทำสคริปต์/ยูทิลิตี `scripts/flex_preview.py` เพื่อ dump JSON แล้วนำไปตรวจใน LINE Flex Message Simulator

## 2. คุณภาพและการทดสอบ
- [ ] เพิ่มไฟล์เทสต์ `tests/test_line_flex.py` ครอบคลุมกรณี flag ปิด/เปิด และตรวจ schema พื้นฐานของ builder
- [ ] จัดการ snapshot/fixture ตัวอย่าง payload เพื่อจับการเปลี่ยน template ที่ไม่ตั้งใจ
- [ ] อัปเดต pipeline/CI ให้รันเทสต์ใหม่ และพิจารณา lint หรือ schema validation สำหรับ template

## 3. ปรับใช้กับแอปหลัก
- [ ] Refactor ฟังก์ชัน `notify_weekly_dca_buy`, `notify_weekly_dca_skipped`, `notify_weekly_dca_skipped_exchange` ให้เรียก Flex builder เมื่อ flag เปิด (fallback เป็นข้อความเดิมเมื่อปิด)
- [ ] ค่อย ๆ ขยายไปยัง notifications อื่น (reserve buy, half-sell, security alert, scheduler) ตาม allowlist ที่กำหนด
- [ ] ปรับระบบ logging/monitoring ให้เก็บ alt text + hash/size ของ payload เพื่อ debug ง่ายขึ้น

## 4. UX และเนื้อหา
- [ ] ออกแบบ layout/card สำหรับแต่ละประเภท แจ้งเตือน (โทนสี ไอคอน ส่วนหัว/ตาราง) ให้สอดคล้อง branding
- [ ] กำหนดกติกาการแสดง holdings (จำกัดจำนวนรายการ/ย่อข้อความ/ปุ่มดูเพิ่ม)
- [ ] อัปเดตเอกสารแนวทาง (alt text, limit ของ Flex, วิธีเพิ่ม template ใหม่)

## 5. Deployment & การควบคุม
- [ ] ทดสอบบน LINE Bot test/staging token ก่อนเปิดใช้งานจริง
- [ ] สร้างแผน rollout (เปิดเฉพาะบางประเภท → monitor → ขยาย) พร้อม checklist
- [ ] ติดตามเมตริก: error rate ของ LINE API, latency ในการสร้าง payload, ขนาดข้อความเฉลี่ย
- [ ] เตรียมแผน rollback เร็ว (ปิด flag / ปรับ allowlist / fallback เป็นข้อความธรรมดา)

## 6. Housekeeping
- [ ] ทบทวนข้อความเดิม เพื่อให้ alt text/fallback ครอบคลุมข้อมูลสำคัญและไม่ซ้ำซ้อน
- [ ] อัปเดต README / developer docs เรื่องการใช้งาน Flex และเครื่องมือ preview
- [ ] ประเมินการ retire โค้ด LINE legacy (`line_notify.py`, script เก่า) หากไม่ใช้ เพื่อลดความสับสน
