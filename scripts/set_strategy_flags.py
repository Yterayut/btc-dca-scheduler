import os
import sys
import MySQLdb
from dotenv import load_dotenv


def main():
    load_dotenv()

    db = MySQLdb.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USER'),
        passwd=os.getenv('DB_PASSWORD'),
        db=os.getenv('DB_NAME'),
        charset='utf8'
    )
    cur = db.cursor()

    # Args: cdc=on|off, exchange=binance|okx
    cdc = None
    exch = None
    for a in sys.argv[1:]:
        if a.startswith('cdc='):
            cdc = a.split('=',1)[1].strip().lower() in ('1','on','true','yes')
        if a.startswith('exchange='):
            exch = a.split('=',1)[1].strip().lower()

    if cdc is not None:
        cur.execute("UPDATE strategy_state SET cdc_enabled=%s WHERE mode='cdc_dca_v1'", (1 if cdc else 0,))
        print(f"cdc_enabled set to {int(cdc)}")

    if exch in ('binance','okx'):
        # Ensure column exists (best-effort)
        try:
            cur.execute("ALTER TABLE strategy_state ADD COLUMN exchange VARCHAR(16) NULL")
        except Exception:
            pass
        cur.execute("UPDATE strategy_state SET exchange=%s WHERE mode='cdc_dca_v1'", (exch,))
        print(f"exchange set to {exch}")

    db.commit()
    cur.close(); db.close()

    print('Done.')


if __name__ == '__main__':
    main()

