import os
import asyncio
from datetime import datetime
from pytz import timezone
from dotenv import load_dotenv


def main():
    # Force dry run for safety
    os.environ['DRY_RUN'] = '1'
    os.environ['STRATEGY_DRY_RUN'] = '1'
    load_dotenv()

    import sys
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    import MySQLdb
    import main as engine

    # Ensure engine uses dry run
    try:
        engine.DRY_RUN = True
    except Exception:
        pass

    # Snapshot current cdc_enabled and disable to force buy
    db = MySQLdb.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USER'),
        passwd=os.getenv('DB_PASSWORD'),
        db=os.getenv('DB_NAME'),
        charset='utf8'
    )
    cur = db.cursor()
    cur.execute("SELECT cdc_enabled FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
    row = cur.fetchone()
    prev_cdc = int(row[0]) if row and row[0] is not None else 1
    try:
        cur.execute("UPDATE strategy_state SET cdc_enabled = 0 WHERE mode='cdc_dca_v1'")
        db.commit()
    except Exception:
        pass
    finally:
        cur.close(); db.close()

    # Run gate with both-mode amounts (dry run)
    now = datetime.now(timezone('Asia/Bangkok'))
    schedule_id = None
    result = asyncio.run(engine.gate_weekly_dca(now, schedule_id, 0.0, {
        'exchange_mode': 'both',
        'binance_amount': 10.0,
        'okx_amount': 10.0,
    }))

    # Restore cdc_enabled
    db = MySQLdb.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USER'),
        passwd=os.getenv('DB_PASSWORD'),
        db=os.getenv('DB_NAME'),
        charset='utf8'
    )
    cur = db.cursor()
    try:
        cur.execute("UPDATE strategy_state SET cdc_enabled = %s WHERE mode='cdc_dca_v1'", (prev_cdc,))
        db.commit()
    except Exception:
        pass
    finally:
        cur.close(); db.close()

    print("Test result:", result)


if __name__ == '__main__':
    main()
