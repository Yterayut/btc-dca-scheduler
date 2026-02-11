# 📦 Deployment Plan — BTC DCA Platform (Production v3.1)

อัพเดท: 2 ตุลาคม 2025

## 1. Pre-flight Checklist
- [ ] อัปเดตโค้ดเป็น release tag ล่าสุด (`git fetch --tags && git checkout v3.1.x`)
- [ ] ยืนยัน `.env.production` ครบถ้วน (DB, LINE, BINANCE, OKX, ADMIN_TOKEN, APP_ENCRYPTION_KEY, FEATURE_S4_ENABLED)
- [ ] รัน `pip install -r requirements.txt` บน staging/production
- [ ] รัน `pytest` + `scripts/backtest_cdc.py --dry-run` เพื่อตรวจ Guard/Anomaly
- [ ] หมุนคีย์ `APP_ENCRYPTION_KEY` ถ้าเก่ากว่า 90 วัน (`python scripts/rotate_encryption_key.py`)

## 2. Canary Rollout Strategy
1. **Deploy Canary Instance** (5% traffic)
   - ตั้ง env `CANARY_NODE=1` และเปิด `FEATURE_S4_ENABLED=0`
   - ใช้ systemd unit เฉพาะ (`scripts/systemd/btc-dca-web.service`/`scheduler`) พร้อมชื่อ suffix `-canary`
2. **Health Verification (15 นาทีแรก)**
   - `curl https://canary.example.com/health`
   - ตรวจ `app.log` และ `btc_purchase_log.log` ไม่มี error ใหม่
   - Dashboard `/api/strategies` ต้องไม่รวม S4 (เพราะ flag ปิด)
3. **Gradual Traffic Shift**
   - เพิ่มการ proxy จาก 5% → 25% → 50% ทุก 30 นาที หากไม่มี alert จาก LINE/Slack

## 3. Feature Flag — S4 Overlay
- เปิด/ปิดผ่าน env `FEATURE_S4_ENABLED`
- ขั้นตอนเปิดใช้งาน
  1. ตั้งค่า `FEATURE_S4_ENABLED=1` ใน `.env.production`
  2. `sudo systemctl restart btc-dca-web.service`
  3. ยืนยัน UI มีการ์ด S4 และ `/api/strategies` คืนค่า `s4_multi_leg`
- หากต้อง rollback: set flag กลับ 0 และ restart web service

## 4. Rollback Automation
- ใช้ systemd `Restart=always` + `StartLimitIntervalSec=60`
- สคริปต์ช่วย:
  - `scripts/deploy.sh --rollback <tag>` ( TODO: สคริปต์จะสร้างในเฟสถัดไป )
  - manual: `git checkout <prev_tag> && sudo systemctl restart btc-dca-*`
- เก็บ snapshot DB ก่อน deploy (`mysqldump -u ... strategy_state purchase_history sell_history > backup.sql`)

## 5. Monitoring & Alert
- LINE Alerts: spread guard, depth/twap guard, compliance anomaly (`notify_security_alert`)
- Compliance API: `/api/compliance_events?limit=50`
- แนะนำเพิ่ม Prometheus exporter ภายหลัง (เฟส 4)

## 6. Post-deploy Verification
- ตรวจสอบว่าสคริปต์ `loadComplianceEvents` ทำงาน (UI การ์ด Compliance Audit มีข้อมูล)
- `python scripts/backtest_cdc.py --dry-run --guard-report` เพื่อเทียบผลกับ staging
- ตรวจสอบ `compliance_audit_log` มีรายการใหม่พร้อม `metadata_encrypted=1`

## 7. Contact & Escalation
- Trading Ops: ops@btc-dca.example.com
- Backend On-call: +66-xxx-xxx-xxx (Telegram @btc-dca-oncall)
- Security: security@btc-dca.example.com

