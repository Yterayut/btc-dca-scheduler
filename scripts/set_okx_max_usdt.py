#!/usr/bin/env python3
import os
import sys


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


def main():
    if len(sys.argv) != 2:
        print('Usage: set_okx_max_usdt.py <amount>')
        sys.exit(1)
    try:
        amount = float(sys.argv[1])
        if amount <= 0:
            raise ValueError
    except Exception:
        print('Error: amount must be a number > 0')
        sys.exit(2)

    load_env_file()
    import MySQLdb
    conn = MySQLdb.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USER'),
        passwd=os.getenv('DB_PASSWORD'),
        db=os.getenv('DB_NAME'),
        charset='utf8'
    )
    cur = conn.cursor()
    cur.execute("UPDATE strategy_state SET okx_max_usdt=%s WHERE mode='cdc_dca_v1'", (amount,))
    conn.commit()
    cur.close(); conn.close()
    print(f'okx_max_usdt set to {amount:.2f} USDT')


if __name__ == '__main__':
    main()

