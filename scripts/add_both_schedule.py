import os
from datetime import datetime, timedelta
from pytz import timezone
from dotenv import load_dotenv


def main():
    load_dotenv()
    import MySQLdb

    tz = timezone('Asia/Bangkok')
    now = datetime.now(tz)
    # schedule in next 2 minutes for safety
    target = now + timedelta(minutes=2)
    hhmm = target.strftime('%H:%M')
    day = target.strftime('%A').lower()  # e.g., 'sunday'

    db = MySQLdb.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USER'),
        passwd=os.getenv('DB_PASSWORD'),
        db=os.getenv('DB_NAME'),
        charset='utf8'
    )
    cur = db.cursor()

    # ensure exchange columns exist by best-effort ALTERs (idempotent)
    try:
        cur.execute("ALTER TABLE schedules ADD COLUMN exchange_mode ENUM('global','binance','okx','both') NOT NULL DEFAULT 'global' AFTER purchase_amount")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE schedules ADD COLUMN binance_amount DECIMAL(10,2) NULL AFTER exchange_mode")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE schedules ADD COLUMN okx_amount DECIMAL(10,2) NULL AFTER binance_amount")
    except Exception:
        pass

    # disable CDC to force buy action
    try:
        cur.execute("UPDATE strategy_state SET cdc_enabled=0 WHERE mode='cdc_dca_v1'")
    except Exception:
        pass

    # insert schedule (both mode) if not exists at the same time/day
    cur.execute(
        "SELECT id FROM schedules WHERE schedule_time=%s AND schedule_day=%s",
        (hhmm, day)
    )
    row = cur.fetchone()
    if row:
        sched_id = int(row[0])
    else:
        cur.execute(
            """
            INSERT INTO schedules (schedule_time, schedule_day, purchase_amount, is_active, exchange_mode, binance_amount, okx_amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (hhmm, day, 0.0, 1, 'both', 10.0, 10.0)
        )
        sched_id = cur.lastrowid
    db.commit()
    cur.close(); db.close()

    print(f"Added/Found schedule id={sched_id} at {day} {hhmm} (both: binance=10, okx=10)")


if __name__ == '__main__':
    main()

