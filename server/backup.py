"""Consistent SQLite online backup, including committed WAL data; no raw DB file copying."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile


def backup(database, directory, keep=14):
    source = Path(database).resolve(strict=True)
    destination = Path(directory).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.qp-backup-', suffix='.db', dir=destination)
    os.close(fd)
    target = destination / ('quantpilot-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.db')
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=20)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst, pages=256, sleep=0.1)
                # A WAL-mode source copies its journal setting; make the snapshot standalone.
                dst.execute('PRAGMA journal_mode=DELETE')
                if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed')
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        copies = sorted(destination.glob('quantpilot-*.db'), reverse=True)
        for old in copies[keep:]:
            old.unlink()
        return target
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default=os.getenv('QP_DB', '/data/quantpilot.db'))
    parser.add_argument('--directory', default='/backup')
    parser.add_argument('--keep', type=int, default=14)
    args = parser.parse_args()
    if args.keep < 1:
        parser.error('--keep must be positive')
    print(backup(args.database, args.directory, args.keep))
