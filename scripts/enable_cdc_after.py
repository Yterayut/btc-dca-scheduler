#!/usr/bin/env python3
import os
import sys
import time
import argparse
from datetime import datetime, timedelta


def load_env_file(path: str = '.env') -> None:
    try:
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


def set_cdc_enabled(val: int) -> None:
    import MySQLdb
    conn = MySQLdb.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USER'),
        passwd=os.getenv('DB_PASSWORD'),
        db=os.getenv('DB_NAME'),
        charset='utf8'
    )
    cur = conn.cursor()
    cur.execute("UPDATE strategy_state SET cdc_enabled=%s WHERE mode='cdc_dca_v1'", (1 if val else 0,))
    conn.commit()
    cur.close(); conn.close()


def parse_args():
    p = argparse.ArgumentParser(description='Enable CDC at a scheduled time or after delay')
    p.add_argument('--at', dest='at', help='Time HH:MM (Asia/Bangkok) to enable today (or tomorrow if past)')
    p.add_argument('--after-minutes', dest='after', type=int, help='Enable after N minutes from now')
    return p.parse_args()


def main():
    load_env_file()
    args = parse_args()
    if not args.at and not args.after:
        print('Usage: enable_cdc_after.py --at HH:MM | --after-minutes N')
        sys.exit(1)

    # Compute target ts (Asia/Bangkok)
    try:
        import pytz
        tz = pytz.timezone('Asia/Bangkok')
    except Exception:
        tz = None

    now = datetime.now(tz) if tz else datetime.now()
    if args.after:
        target = now + timedelta(minutes=args.after)
    else:
        hh, mm = [int(x) for x in args.at.split(':', 1)]
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)

    wait_s = max(0, int((target - now).total_seconds()))
    print(f'[enable_cdc_after] Now={now} Target={target} Wait={wait_s}s')
    try:
        time.sleep(wait_s)
    except KeyboardInterrupt:
        print('[enable_cdc_after] Cancelled')
        sys.exit(130)

    set_cdc_enabled(1)
    print('[enable_cdc_after] cdc_enabled set to 1')


if __name__ == '__main__':
    main()

