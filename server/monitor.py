"""Console status check. Exit 0 = expected feeds healthy, 1 = warning, 2 = API/config error.
No external notifications are sent. Closed sessions are not inferred as missing candles.
"""
import json
import os
import sys
import urllib.request


def evaluate(stats, source, symbols, stall_seconds, now):
    rows = {(r['source'], r['symbol']): r for r in stats['streams']}
    messages = []
    for symbol in symbols:
        row = rows.get((source, symbol))
        if row is None:
            messages.append(f'{symbol}: vir ni registriran')
            continue
        if row['stale']:
            messages.append(f'{symbol}: ni svežega signala delovanja')
        if row['status'] not in ('ok', 'backfill'):
            messages.append(f"{symbol}: {row['status']}")
        elif row['status'] == 'backfill' and now - row['last_ingested'] > stall_seconds:
            messages.append(f'{symbol}: uvoz zaostaja in ne napreduje')
    return messages


def main():
    source = os.getenv('QP_EXPECTED_SOURCE', 'quantpilot-main')
    symbols = [s.strip() for s in os.getenv('QP_EXPECTED_SYMBOLS', '').split(',') if s.strip()]
    key = os.getenv('QP_API_KEY', '')
    if not symbols or not key:
        print('Napaka konfiguracije: nastavite QP_EXPECTED_SYMBOLS in QP_API_KEY.')
        return 2
    try:
        base = os.getenv('QP_MONITOR_URL', 'http://127.0.0.1:8000').rstrip('/')
        request = urllib.request.Request(base+'/v2/stats', headers={'X-API-Key':key})
        with urllib.request.urlopen(request, timeout=10) as response:
            stats = json.load(response)
        messages = evaluate(stats, source, symbols, int(os.getenv('QP_STALL_SECONDS', '300')),
                            stats['server_utc_epoch'])
    except Exception:
        print('Preverjanje API-ja ni uspelo; preverite strežnik, konfiguracijo in API-ključ.')
        return 2
    if messages:
        for message in messages:
            print(message)
        return 1
    print(f'OK: {len(symbols)} pričakovanih simbolov, vir {source}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
