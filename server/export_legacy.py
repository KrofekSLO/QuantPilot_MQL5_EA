"""Export preserved v3 rows to CSV. Does not create v4 cursors or mix feed identities."""
import argparse
from contextlib import closing
import csv
import os
from pathlib import Path
import sqlite3


def export(database, destination):
    source = Path(database).resolve(strict=True)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as con:
        cursor = con.execute('SELECT * FROM bars ORDER BY source,symbol,timeframe,t')
        with open(destination, 'x', newline='', encoding='utf-8') as output:
            writer = csv.writer(output)
            writer.writerow([c[0] for c in cursor.description])
            writer.writerows(cursor)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default=os.getenv('QP_DB', '/data/quantpilot.db'))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    export(args.database, args.output)
