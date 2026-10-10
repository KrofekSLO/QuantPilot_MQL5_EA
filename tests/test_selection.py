"""Independent Python model of the MQL anchor/batch selection; NOT an MT5 runtime test."""
import pytest


def select(series, anchor, last_ack, size):
    # series is ascending and includes one current (unclosed) candle.
    if anchor not in series:
        return None
    shift = len(series)-1-series.index(anchor)
    if shift < 1:
        return None
    pending = shift-1 if last_ack else shift
    take = min(pending, size)
    if take == 0:
        return []
    requested = take+(1 if last_ack else 0)
    start_pos = shift-take if last_ack else shift-take+1
    oldest = len(series)-start_pos-requested
    copied = series[oldest:oldest+requested]
    assert copied[0] == anchor
    return copied[1:] if last_ack else copied


def test_full_bootstrap_then_continuous_catchup():
    series = list(range(3001))
    last = 0
    output = []
    # Zero is a model start time only; real MT5 timestamps are >1e9.
    first = True
    while True:
        bars = select(series, 0 if first else last, 0 if first else 1, 200)
        if not bars:break
        output.extend(bars)
        last = bars[-1]
        first = False
    assert output == series[:-1]


@pytest.mark.parametrize('size', [1, 2, 200, 500])
def test_weekend_and_long_outage_do_not_skip_bars(size):
    series = [100, 160, 220, 500000, 500060, 500120, 500180]
    ack = 160
    received = []
    while True:
        bars = select(series, ack, 1, size)
        if not bars:break
        received.extend(bars)
        ack = bars[-1]
    assert received == series[2:-1]


def test_absent_anchor_halts_instead_of_skipping():
    assert select([100, 160, 220], 40, 1, 200) is None


def test_new_minute_during_copy_fails_anchor_guard():
    original = list(range(11))
    anchor = 3
    shift = len(original)-1-original.index(anchor)
    selected = 2
    start_pos = shift-selected
    requested = selected+1
    changed = original+[11]
    oldest = len(changed)-start_pos-requested
    copied = changed[oldest:oldest+requested]
    assert copied[0] != anchor  # MQL refuses this snapshot and retries.
