import importlib.util
from pathlib import Path
import sqlite3


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1]/'server'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backup_includes_committed_wal_and_retention(tmp_path):
    module = load('backup')
    source = tmp_path/'source.db'
    # Keep the source connection open: rows remain in WAL during the backup.
    with sqlite3.connect(source) as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE samples (value INTEGER)')
        con.execute('INSERT INTO samples VALUES (42)')
        con.commit()
        output = module.backup(source, tmp_path/'backups', keep=1)
        with sqlite3.connect(output) as copied:
            assert copied.execute('SELECT value FROM samples').fetchone()[0] == 42
            assert copied.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        module.backup(source, tmp_path/'backups', keep=1)
    assert len(list((tmp_path/'backups').glob('quantpilot-*.db'))) == 1
    assert not list((tmp_path/'backups').glob('.qp-backup-*'))


def test_monitor_missing_stale_and_stalled_feeds():
    module = load('monitor')
    now = 10000
    row = dict(source='feed', symbol='EURUSD', stale=False, status='ok', last_ingested=1)
    stats = dict(streams=[row])
    assert module.evaluate(stats, 'feed', ['EURUSD'], 300, now) == []  # closed session is fine
    assert module.evaluate(stats, 'feed', ['EURUSD','XAUUSD'], 300, now)
    row['stale'] = True
    assert module.evaluate(stats, 'feed', ['EURUSD'], 300, now)
    row['stale'] = False
    row['status'] = 'backfill'
    assert module.evaluate(stats, 'feed', ['EURUSD'], 300, now)
    row['last_ingested'] = now
    assert module.evaluate(stats, 'feed', ['EURUSD'], 300, now) == []


def test_export_legacy_keeps_original_rows(tmp_path):
    module = load('export_legacy')
    database = tmp_path/'v3.db'
    with sqlite3.connect(database) as con:
        con.execute('CREATE TABLE bars (source TEXT,symbol TEXT,timeframe TEXT,t INTEGER)')
        con.execute("INSERT INTO bars VALUES ('Broker','EURUSD','M1',1700000040)")
    output = tmp_path/'old.csv'
    module.export(database, output)
    assert 'Broker,EURUSD,M1,1700000040' in output.read_text()
