"""QuantPilot v4: authenticated, transactional M1 ingestion.
Times are raw broker/MT5 times, not asserted UTC. One source identifies one feed.
"""
import hmac
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

DB = os.getenv('QP_DB', '/data/quantpilot.db')
API_KEY = os.getenv('QP_API_KEY', '')
if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', API_KEY):
    raise RuntimeError('QP_API_KEY must be 32-128 ASCII letters, digits, _ or -')
STALE_SECONDS = int(os.getenv('QP_STALE_SECONDS', '180'))
app = FastAPI(title='QuantPilot Collector v4', docs_url=None, redoc_url=None,
              openapi_url=None)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    # FastAPI's default handler echoes invalid values: NaN/Inf then breaks JSON serialization.
    return JSONResponse(status_code=422, content={'detail': [
        {key: err[key] for key in ('loc', 'msg', 'type') if key in err}
        for err in exc.errors()]})


@app.exception_handler(sqlite3.Error)
async def database_error(request, exc):
    return JSONResponse(status_code=503, content={'detail': 'Database unavailable; retry'},
                        headers={'Retry-After': '2'})


@contextmanager
def connect(write=False):
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    con = sqlite3.connect(DB, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=5000')
    try:
        if write:
            con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


with connect() as db:
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS qp_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO qp_meta VALUES (?,?)', ('db_id', uuid.uuid4().hex))
    db.execute('''CREATE TABLE IF NOT EXISTS streams_v4 (
        source TEXT NOT NULL, symbol TEXT NOT NULL, broker TEXT NOT NULL,
        digits INTEGER NOT NULL, point REAL NOT NULL, start_t INTEGER NOT NULL DEFAULT 0,
        last_t INTEGER NOT NULL DEFAULT 0, latest_closed_t INTEGER NOT NULL DEFAULT 0,
        last_seen INTEGER NOT NULL, last_ingested INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'waiting_history',
        PRIMARY KEY(source,symbol))''')
    db.execute('''CREATE TABLE IF NOT EXISTS bars_v4 (
        source TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
        t INTEGER NOT NULL, o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL,
        c REAL NOT NULL, volume INTEGER NOT NULL, real_volume INTEGER NOT NULL,
        spread INTEGER NOT NULL, atr14 REAL, rsi14 REAL, ema20 REAL, ema50 REAL, ema200 REAL,
        received_at INTEGER NOT NULL, PRIMARY KEY(source,symbol,timeframe,t))''')
    db.execute('''CREATE TABLE IF NOT EXISTS quotes_v4 (
        source TEXT NOT NULL, symbol TEXT NOT NULL, time_msc INTEGER NOT NULL,
        bid REAL NOT NULL, ask REAL NOT NULL, received_at INTEGER NOT NULL,
        PRIMARY KEY(source,symbol,time_msc))''')


def auth(x_api_key: Annotated[str | None, Header()] = None):
    # Encode before comparison: non-ASCII attacker headers must return 401, not 500.
    if not x_api_key or not hmac.compare_digest(x_api_key.encode('utf-8'), API_KEY.encode('ascii')):
        raise HTTPException(401, 'Unauthorized')


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


Timestamp = Annotated[int, Field(strict=True, ge=1000000000, le=4102444800)]
Cursor = Annotated[int, Field(strict=True, ge=0, le=4102444800)]
Positive = Annotated[FiniteFloat, Field(gt=0)]
Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.-]{1,80}$')]


class Identity(Model):
    source: Identifier
    symbol: str = Field(min_length=1, max_length=80, pattern=r'^[^\x00-\x1f\x7f]+$')
    broker: str = Field(min_length=1, max_length=100, pattern=r'^[^\x00-\x1f\x7f]+$')
    timeframe: Literal['M1'] = 'M1'
    digits: int = Field(strict=True, ge=0, le=12)
    point: Positive


class StateRequest(Identity):
    initial_start_t: Cursor = 0


class Bar(Model):
    t: Timestamp
    o: Positive
    h: Positive
    l: Positive
    c: Positive
    volume: int = Field(strict=True, ge=0, le=9223372036854775807)
    real_volume: int = Field(strict=True, ge=0, le=9223372036854775807)
    spread: int = Field(strict=True, ge=0, le=2147483647)
    atr14: Annotated[FiniteFloat, Field(ge=0)] | None = None
    rsi14: Annotated[FiniteFloat, Field(ge=0, le=100)] | None = None
    ema20: Positive | None = None
    ema50: Positive | None = None
    ema200: Positive | None = None

    @model_validator(mode='after')
    def valid_ohlc(self):
        if self.l > min(self.o, self.c) or self.h < max(self.o, self.c) or self.h < self.l:
            raise ValueError('invalid OHLC')
        if self.t % 60:
            raise ValueError('M1 timestamp must align to a minute')
        return self


class Quote(Model):
    time_msc: int = Field(strict=True, ge=1000000000000, le=4102444800000)
    bid: Positive
    ask: Positive

    @model_validator(mode='after')
    def valid_quote(self):
        if self.ask < self.bid:
            raise ValueError('ask below bid')
        return self


class Session(Identity):
    db_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class Batch(Session):
    expected_last_t: Cursor
    latest_closed_t: Timestamp
    bars: list[Bar] = Field(min_length=1, max_length=500)
    quote: Quote | None = None

    @model_validator(mode='after')
    def ascending(self):
        if any(a.t >= b.t for a, b in zip(self.bars, self.bars[1:])):
            raise ValueError('bars must be strictly ascending without duplicates')
        if self.bars[-1].t > self.latest_closed_t:
            raise ValueError('bar newer than latest closed bar')
        return self


class Heartbeat(Session):
    latest_closed_t: Cursor = 0
    status: Literal['ok', 'backfill', 'waiting_history', 'history_anchor_missing',
                    'indicator_wait', 'invalid_bar', 'disconnected', 'history_changed'] = 'ok'
    quote: Quote | None = None


def db_id(con):
    return con.execute("SELECT value FROM qp_meta WHERE key='db_id'").fetchone()[0]


def stream(con, identity):
    row = con.execute('SELECT * FROM streams_v4 WHERE source=? AND symbol=?',
                      (identity.source, identity.symbol)).fetchone()
    if row is not None and (row['broker'] != identity.broker or row['digits'] != identity.digits
                           or not math.isclose(row['point'], identity.point, rel_tol=1e-10, abs_tol=0)):
        raise HTTPException(409, 'Source/feed metadata changed: use a distinct source identifier')
    return row


def session(con, item):
    if item.db_id != db_id(con):
        raise HTTPException(409, 'Database identity changed: refresh state')
    row = stream(con, item)
    if row is None:
        raise HTTPException(409, 'Stream missing: refresh state')
    return row


def store_quote(con, item, ts):
    if item.quote is not None:
        q = item.quote
        con.execute('INSERT OR IGNORE INTO quotes_v4 VALUES (?,?,?,?,?,?)',
                    (item.source, item.symbol, q.time_msc, q.bid, q.ask, ts))


def store_bars(con, item, ts):
    con.executemany('''INSERT INTO bars_v4 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source,symbol,timeframe,t) DO UPDATE SET
        o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,volume=excluded.volume,
        real_volume=excluded.real_volume,spread=excluded.spread,
        atr14=COALESCE(excluded.atr14,bars_v4.atr14),
        rsi14=COALESCE(excluded.rsi14,bars_v4.rsi14),
        ema20=COALESCE(excluded.ema20,bars_v4.ema20),
        ema50=COALESCE(excluded.ema50,bars_v4.ema50),
        ema200=COALESCE(excluded.ema200,bars_v4.ema200),received_at=excluded.received_at''',
        [(item.source, item.symbol, 'M1', b.t, b.o, b.h, b.l, b.c, b.volume,
          b.real_volume, b.spread, b.atr14, b.rsi14, b.ema20, b.ema50, b.ema200, ts)
         for b in item.bars])


@app.get('/health')
def health():
    try:
        with connect() as con:
            con.execute('SELECT 1 FROM qp_meta LIMIT 1').fetchone()
        return {'ok': True, 'version': '4.00'}
    except sqlite3.Error:
        raise HTTPException(503, 'Database unavailable') from None


@app.post('/v2/mt5/state', dependencies=[Depends(auth)])
def state(item: StateRequest):
    ts = int(time.time())
    if item.initial_start_t and (item.initial_start_t < 1000000000 or item.initial_start_t % 60):
        raise HTTPException(422, 'Invalid initial M1 start time')
    with connect(write=True) as con:
        row = stream(con, item)
        if row is None:
            con.execute('''INSERT INTO streams_v4
                (source,symbol,broker,digits,point,start_t,last_seen) VALUES (?,?,?,?,?,?,?)''',
                (item.source, item.symbol, item.broker, item.digits, item.point, item.initial_start_t, ts))
        elif row['start_t'] == 0 and item.initial_start_t:
            con.execute('UPDATE streams_v4 SET start_t=? WHERE source=? AND symbol=?',
                        (item.initial_start_t, item.source, item.symbol))
        row = stream(con, item)
        result = {'accepted': True, 'db_id': db_id(con), 'start_t': row['start_t'],
                  'last_t': row['last_t'], 'initialized': row['start_t'] > 0}
    return result


@app.post('/v2/mt5/bars', dependencies=[Depends(auth)])
def ingest(item: Batch):
    ts = int(time.time())
    with connect(write=True) as con:
        row = session(con, item)
        if row['start_t'] == 0 or row['last_t'] != item.expected_last_t:
            raise HTTPException(409, 'Cursor mismatch: refresh state')
        if item.bars[0].t < row['start_t'] or item.bars[0].t <= row['last_t']:
            raise HTTPException(409, 'Batch precedes stream cursor')
        if row['last_t'] == 0 and item.bars[0].t != row['start_t']:
            raise HTTPException(409, 'First batch must begin at the fixed bootstrap start')
        store_bars(con, item, ts)
        store_quote(con, item, ts)
        last = item.bars[-1].t
        status = 'ok' if last == item.latest_closed_t else 'backfill'
        con.execute('''UPDATE streams_v4 SET last_t=?,latest_closed_t=?,last_seen=?,
            last_ingested=?,status=? WHERE source=? AND symbol=?''',
                    (last, item.latest_closed_t, ts, ts, status, item.source, item.symbol))
        result = {'accepted': True, 'db_id': db_id(con), 'last_t': last,
                  'accepted_count': len(item.bars)}
    return result  # Returned only after commit; interrupted ACK is recovered via /state.


@app.post('/v2/mt5/reconcile', dependencies=[Depends(auth)])
def reconcile(item: Batch):
    ts = int(time.time())
    with connect(write=True) as con:
        row = session(con, item)
        if row['last_t'] != item.expected_last_t:
            raise HTTPException(409, 'Cursor mismatch: refresh state')
        if item.bars[0].t < row['start_t'] or item.bars[-1].t > row['last_t']:
            raise HTTPException(409, 'Reconciliation is limited to the acknowledged history')
        store_bars(con, item, ts)
        # No advancement and no fake live-ingestion timestamp.
        result = {'accepted': True, 'db_id': db_id(con), 'last_t': row['last_t'],
                  'accepted_count': len(item.bars)}
    return result


@app.post('/v2/mt5/heartbeat', dependencies=[Depends(auth)])
def heartbeat(item: Heartbeat):
    ts = int(time.time())
    with connect(write=True) as con:
        row = session(con, item)
        # A stale client cannot claim 'ok' while an acknowledged backlog remains.
        status = item.status
        if status == 'ok' and item.latest_closed_t > row['last_t']:
            status = 'backfill'
        con.execute('''UPDATE streams_v4 SET latest_closed_t=?,last_seen=?,status=?
            WHERE source=? AND symbol=?''',
                    (item.latest_closed_t, ts, status, item.source, item.symbol))
        store_quote(con, item, ts)
        result = {'accepted': True, 'db_id': db_id(con), 'last_t': row['last_t']}
    return result


@app.get('/v2/stats', dependencies=[Depends(auth)])
def stats():
    now = int(time.time())
    with connect() as con:
        rows = con.execute('''SELECT s.*,COUNT(b.t) AS bars,MIN(b.t) AS first_raw_t,
            MAX(b.t) AS last_raw_t FROM streams_v4 s LEFT JOIN bars_v4 b
            ON b.source=s.source AND b.symbol=s.symbol GROUP BY s.source,s.symbol
            ORDER BY s.source,s.symbol''').fetchall()
        identity = db_id(con)
    items = []
    for row in rows:
        item = dict(row)
        item['heartbeat_age_seconds'] = max(0, now - row['last_seen'])
        item['stale'] = item['heartbeat_age_seconds'] > STALE_SECONDS
        item['reported_backlog_raw_seconds'] = max(0, row['latest_closed_t'] - row['last_t']) if row['last_t'] else None
        # Candle ages are broker-time deltas; never subtract server UTC from raw MT5 t.
        items.append(item)
    return {'db_id': identity, 'server_utc_epoch': now, 'streams': items}


@app.get('/v2/bars', dependencies=[Depends(auth)])
def read_bars(source: Identifier, symbol: str = Query(min_length=1, max_length=80),
              after: int = Query(default=0, ge=0), limit: int = Query(default=500, ge=1, le=5000)):
    with connect() as con:
        rows = con.execute('''SELECT * FROM bars_v4 WHERE source=? AND symbol=? AND t>?
            ORDER BY t LIMIT ?''', (source, symbol, after, limit)).fetchall()
    return {'bars': [dict(row) for row in rows], 'next_after': rows[-1]['t'] if rows else after}
