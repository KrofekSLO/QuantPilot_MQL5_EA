import importlib.util
import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

KEY = 'a' * 64
T = 1700000040  # minute aligned
HEADERS = {'X-API-Key': KEY}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('QP_API_KEY', KEY)
    monkeypatch.setenv('QP_DB', str(tmp_path / 'qp.db'))
    spec = importlib.util.spec_from_file_location('qp_test', Path(__file__).parents[1] / 'server/app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with TestClient(module.app) as client:
        yield client, module


def identity(**extra):
    return dict(source='test-feed', symbol='EURUSD', broker='Demo-Server', timeframe='M1',
                digits=5, point=0.00001, **extra)


def init(client, start=T):
    response = client.post('/v2/mt5/state', json=identity(initial_start_t=start), headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def bar(t=T, **changes):
    result = dict(t=t, o=1.1, h=1.2, l=1.0, c=1.15, volume=17, real_volume=0, spread=12,
                  atr14=0.001, rsi14=52.3, ema20=1.11, ema50=1.12, ema200=1.13)
    result.update(changes)
    return result


def batch(state, bars=None, **changes):
    result = identity(db_id=state['db_id'], expected_last_t=state['last_t'],
                      latest_closed_t=T+600, bars=bars or [bar()], quote=None)
    result.update(changes)
    return result


def post(client, payload, path='/v2/mt5/bars'):
    return client.post(path, json=payload, headers=HEADERS)


def test_ingest_indicators_metadata_and_read(api):
    client, _ = api
    state = init(client)
    response = post(client, batch(state, [bar(), bar(T+60)]))
    assert response.status_code == 200, response.text
    assert response.json()['accepted_count'] == 2
    assert response.json()['last_t'] == T+60
    rows = client.get('/v2/bars', params={'source':'test-feed', 'symbol':'EURUSD'}, headers=HEADERS).json()
    assert len(rows['bars']) == 2
    assert rows['bars'][0]['rsi14'] == 52.3
    stats = client.get('/v2/stats', headers=HEADERS).json()['streams'][0]
    assert stats['digits'] == 5 and stats['point'] == 0.00001
    assert stats['bars'] == 2 and stats['status'] == 'backfill'


def test_ack_loss_retry_and_restart_uses_server_cursor(api):
    client, _ = api
    state = init(client)
    payload = batch(state)
    assert post(client, payload).status_code == 200  # Imagine the ACK is lost.
    assert post(client, payload).status_code == 409
    resumed = init(client, 0)
    assert resumed['last_t'] == T
    assert post(client, batch(resumed, [bar(T+60)])).status_code == 200
    assert client.get('/v2/stats', headers=HEADERS).json()['streams'][0]['bars'] == 2


def test_partial_bootstrap_cannot_skip_fixed_start(api):
    client, _ = api
    state = init(client)
    assert post(client, batch(state, [bar(T+120)])).status_code == 409
    assert init(client, 0)['last_t'] == 0
    assert post(client, batch(state)).status_code == 200


def test_state_start_is_fixed_and_never_moves_forward(api):
    client, _ = api
    waiting = init(client, 0)
    assert waiting['initialized'] is False
    assert init(client)['start_t'] == T
    assert init(client, T+600)['start_t'] == T


def test_long_outage_and_weekend_gap_not_rejected(api):
    client, _ = api
    state = init(client)
    assert post(client, batch(state)).status_code == 200
    state = init(client, 0)
    later = T + 7*86400
    assert post(client, batch(state, [bar(later)], latest_closed_t=later)).status_code == 200
    assert init(client, 0)['last_t'] == later


@pytest.mark.parametrize('changes', [dict(h=0.5), dict(o=-1), dict(spread=-1), dict(real_volume=-1),
                                     dict(rsi14=101), dict(ema20=0), dict(t=T+1)])
def test_bad_candle_rejected_without_cursor_move(api, changes):
    client, _ = api
    state = init(client)
    assert post(client, batch(state, [bar(**changes)])).status_code == 422
    assert init(client, 0)['last_t'] == 0


@pytest.mark.parametrize('literal', ['NaN', 'Infinity', '-Infinity'])
def test_non_finite_json_prices_rejected(api, literal):
    client, _ = api
    state = init(client)
    import json
    body = json.dumps(batch(state)).replace('"h": 1.2', '"h": '+literal)
    response = client.post('/v2/mt5/bars', content=body,
                           headers={**HEADERS, 'Content-Type':'application/json'})
    assert response.status_code == 422, response.text
    assert init(client, 0)['last_t'] == 0


def test_invalid_quote_and_omitting_quote(api):
    client, _ = api
    state = init(client)
    invalid = dict(time_msc=T*1000, bid=0, ask=0)
    assert post(client, batch(state, quote=invalid)).status_code == 422
    assert post(client, batch(state, quote=None)).status_code == 200


def test_valid_quote_stored_without_duplicate(api):
    client, module = api
    state = init(client)
    q = dict(time_msc=T*1000, bid=1.1, ask=1.10012)
    assert post(client, batch(state, quote=q)).status_code == 200
    hb = identity(db_id=state['db_id'], latest_closed_t=T, status='ok', quote=q)
    assert post(client, hb, '/v2/mt5/heartbeat').status_code == 200
    with module.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM quotes_v4').fetchone()[0] == 1


def test_reconcile_repairs_missing_and_corrects_candle_without_advancing(api):
    client, module = api
    state = init(client)
    assert post(client, batch(state, [bar(), bar(T+60), bar(T+120)])).status_code == 200
    with module.connect(write=True) as con:
        con.execute('DELETE FROM bars_v4 WHERE t=?', (T+60,))
    state = init(client, 0)
    repair = batch(state, [bar(T, c=1.16), bar(T+60), bar(T+120)])
    assert post(client, repair, '/v2/mt5/reconcile').status_code == 200
    assert post(client, repair, '/v2/mt5/reconcile').status_code == 200
    assert init(client, 0)['last_t'] == T+120
    with module.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM bars_v4').fetchone()[0] == 3
        assert con.execute('SELECT c FROM bars_v4 WHERE t=?', (T,)).fetchone()[0] == 1.16


def test_repair_cannot_extend_cursor_and_null_preserves_indicators(api):
    client, module = api
    state = init(client)
    assert post(client, batch(state)).status_code == 200
    state = init(client, 0)
    assert post(client, batch(state, [bar(T+60)]), '/v2/mt5/reconcile').status_code == 409
    assert post(client, batch(state, [bar(atr14=None)]), '/v2/mt5/reconcile').status_code == 200
    with module.connect() as con:
        assert con.execute('SELECT atr14 FROM bars_v4').fetchone()[0] == 0.001


def test_old_database_identity_rejected(api):
    client, module = api
    old = init(client)
    with module.connect(write=True) as con:
        con.execute("UPDATE qp_meta SET value=? WHERE key='db_id'", ('b'*32,))
        con.execute('DELETE FROM streams_v4')
    assert post(client, batch(old)).status_code == 409
    fresh = init(client)
    assert fresh['db_id'] != old['db_id'] and fresh['last_t'] == 0
    assert post(client, batch(fresh)).status_code == 200


def test_auth_required_every_private_route(api):
    client, _ = api
    for route in ['/v2/stats', '/v2/bars?source=test-feed&symbol=EURUSD']:
        assert client.get(route).status_code == 401
    for route in ['/v2/mt5/state', '/v2/mt5/bars', '/v2/mt5/reconcile', '/v2/mt5/heartbeat']:
        assert client.post(route, json={}).status_code == 401
    assert client.get('/health').status_code == 200
    assert client.get('/openapi.json').status_code == 404


def test_same_source_changed_feed_fails_and_other_source_is_separate(api):
    client, _ = api
    state = init(client)
    changed = identity(initial_start_t=T)
    changed['broker'] = 'Other-Server'
    assert post(client, changed, '/v2/mt5/state').status_code == 409
    changed['source'] = 'second-feed'
    other = post(client, changed, '/v2/mt5/state').json()
    assert post(client, batch(state)).status_code == 200
    assert other['last_t'] == 0


def test_transaction_rollback_on_storage_failure(api):
    client, module = api
    state = init(client)
    with module.connect(write=True) as con:
        con.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON bars_v4
            WHEN NEW.t > 1700000040 BEGIN SELECT RAISE(ABORT,'test failure'); END""")
    assert post(client, batch(state, [bar(), bar(T+60)])).status_code == 503
    assert init(client, 0)['last_t'] == 0
    with module.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM bars_v4').fetchone()[0] == 0


def test_two_writers_cannot_advance_same_cursor_twice(api):
    client, _ = api
    state = init(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _:post(client, batch(state)).status_code, range(2)))
    assert sorted(outcomes) == [200, 409]


def test_duplicate_or_descending_and_future_bar_rejected(api):
    client, _ = api
    state = init(client)
    for bars in [[bar(), bar()], [bar(T+60), bar()], [bar(T+660)]]:
        assert post(client, batch(state, bars)).status_code == 422


def test_status_and_staleness_are_not_fake_live_data(api):
    client, module = api
    state = init(client)
    assert post(client, batch(state)).status_code == 200
    payload = identity(db_id=state['db_id'], latest_closed_t=T+60, status='ok', quote=None)
    assert post(client, payload, '/v2/mt5/heartbeat').status_code == 200
    with module.connect(write=True) as con:
        con.execute('UPDATE streams_v4 SET last_seen=last_seen-1000')
    row = client.get('/v2/stats', headers=HEADERS).json()['streams'][0]
    assert row['status'] == 'backfill' and row['stale'] is True
    assert row['reported_backlog_raw_seconds'] == 60


def test_startup_preserves_v3_tables_and_v4_database_identity(api):
    client, module = api
    original = init(client)
    with module.connect(write=True) as con:
        con.execute('CREATE TABLE bars (source TEXT,symbol TEXT,timeframe TEXT,t INTEGER)')
        con.execute("INSERT INTO bars VALUES ('LegacyBroker','EURUSD','M1',1700000040)")
        con.execute('CREATE TABLE quotes (bid REAL)')
        con.execute('INSERT INTO quotes VALUES (1.1)')
    spec = importlib.util.spec_from_file_location('qp_restart', Path(__file__).parents[1] / 'server/app.py')
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    with TestClient(restarted.app) as after:
        assert init(after, 0)['db_id'] == original['db_id']
    with restarted.connect() as con:
        assert con.execute('SELECT source FROM bars').fetchone()[0] == 'LegacyBroker'
        assert con.execute('SELECT bid FROM quotes').fetchone()[0] == 1.1
