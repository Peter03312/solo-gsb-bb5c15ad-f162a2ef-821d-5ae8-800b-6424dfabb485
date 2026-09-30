"""Engine unit tests for the device-release evaluation."""

from app.engine import SensorRequirement, evaluate_release
from app.storage import BatchStore, StoredEvent


def _ev(rev, parent, start=0, end=10, value=1.0, withdrawn=False,
        batch="B", sensor="S", unit="u", key=None):
    return StoredEvent(
        batch_id=batch,
        sensor_id=sensor,
        revision_id=rev,
        parent_id=parent,
        idempotency_key=key or f"k-{rev}",
        start_ns=start,
        end_ns=end,
        value=None if withdrawn else float(value),
        unit=unit,
        withdrawn=withdrawn,
    )


def _store(*events):
    store = BatchStore()
    for event in events:
        store.events[event.revision_id] = event
        store.revision_sensor[event.revision_id] = event.sensor_id
    return store


def _req(sensor="S", lo=0.0, hi=10.0, unit="u"):
    return SensorRequirement(sensor_id=sensor, min_value=lo, max_value=hi, unit=unit)


def test_evaluate_release_unknown_sensor_returns_none():
    store = _store(_ev("v1", "ROOT", 0, 100, sensor="SX"))
    assert evaluate_release("B", 0, 100, 1, [_req()], store) is None


def test_evaluate_release_half_open_boundaries():
    # Abutting fragments [0,10) and [10,20): the second fragment's start is
    # not covered by the first, so the qualified run splits exactly at 10.
    store = _store(
        _ev("v1", "ROOT", 0, 10, value=1.0),
        _ev("v2", "v1", 10, 20, value=99.0),
    )
    doc = evaluate_release("B", 0, 20, 1, [_req()], store)
    assert [(w["start_ns"], w["end_ns"]) for w in doc["release_windows"]] == [(0, 10)]
    assert [(i["start_ns"], i["end_ns"]) for i in doc["rejected_intervals"]] == [
        (10, 20)
    ]


def test_evaluate_release_hash_tracks_state_not_query_params():
    store = _store(_ev("v1", "ROOT", 0, 100, value=1.0))
    base = evaluate_release("B", 0, 100, 1, [_req()], store)
    # Query parameters (window, bounds, unit, duration) never affect the hash.
    same = evaluate_release("B", 10, 50, 7, [_req(lo=2.0, hi=3.0, unit="x")], store)
    assert same["timeline_hash"] == base["timeline_hash"]
    # A changed folded state does.
    other = evaluate_release(
        "B", 0, 100, 1, [_req()], _store(_ev("v1", "ROOT", 0, 100, value=2.0))
    )
    assert other["timeline_hash"] != base["timeline_hash"]


def test_evaluate_release_blocked_waits_for_backfill_even_when_value_in_range():
    # Grounded coverage with an in-range value still loses to a blocked
    # revision covering the same span: backfill could change the fold winner.
    store = _store(
        _ev("v1", "ROOT", 0, 100, value=1.0),
        _ev("orphan", "ghost", 0, 100, value=2.0),
    )
    doc = evaluate_release("B", 0, 100, 1, [_req()], store)
    assert doc["release_windows"] == []
    [interval] = doc["rejected_intervals"]
    assert interval["reason"] == "sensors_disqualified"
    assert interval["sensors"][0]["reason"] == "blocked"
    assert interval["sensors"][0]["missing_ancestors"] == ["ghost"]
