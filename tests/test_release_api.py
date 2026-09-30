"""End-to-end API behavior for the device-release evaluation endpoint."""


def _event(rev, parent, start, end, value=None, withdrawn=False, key=None,
           batch="B1", sensor="S1", unit="C"):
    return {
        "batch_id": batch,
        "sensor_id": sensor,
        "revision_id": rev,
        "parent_id": parent,
        "idempotency_key": key or f"k-{rev}",
        "start_ns": start,
        "end_ns": end,
        "value": value,
        "unit": unit,
        "withdrawn": withdrawn,
    }


def _post(client, event, batch="B1"):
    return client.post(f"/api/v1/batches/{batch}/events", json=event)


def _release(client, batch="B1", start=0, end=300, min_duration_ns=1,
             sensors=None, **overrides):
    body = {
        "start_ns": start,
        "end_ns": end,
        "min_duration_ns": min_duration_ns,
        "sensors": sensors
        if sensors is not None
        else [{"sensor_id": "S1", "min_value": 0.0, "max_value": 4.0, "unit": "C"}],
    }
    body.update(overrides)
    return client.post(f"/api/v1/batches/{batch}/release", json=body)


def test_release_single_sensor_full_window(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0))
    r = _release(client)
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] == "B1"
    assert (body["window_start_ns"], body["window_end_ns"]) == (0, 300)
    assert body["min_duration_ns"] == 1
    assert body["releasable"] is True
    assert body["release_windows"] == [
        {"start_ns": 0, "end_ns": 300, "duration_ns": 300}
    ]
    assert body["rejected_intervals"] == []
    assert len(body["timeline_hash"]) == 64


def test_release_requires_all_sensors_simultaneously(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0, sensor="S1", key="k1"))
    _post(
        client,
        _event("w1", "ROOT", 50, 250, value=10.0, sensor="S2", unit="mm", key="k2"),
    )
    body = _release(
        client,
        sensors=[
            {"sensor_id": "S1", "min_value": 0.0, "max_value": 4.0, "unit": "C"},
            {"sensor_id": "S2", "min_value": 5.0, "max_value": 15.0, "unit": "mm"},
        ],
    ).json()
    # Release only where both sensors qualify at the same time.
    assert body["releasable"] is True
    assert [(w["start_ns"], w["end_ns"], w["duration_ns"]) for w in body["release_windows"]] == [
        (50, 250, 200)
    ]
    rejected = body["rejected_intervals"]
    assert [(i["start_ns"], i["end_ns"], i["reason"]) for i in rejected] == [
        (0, 50, "sensors_disqualified"),
        (250, 300, "sensors_disqualified"),
    ]
    for interval in rejected:
        assert interval["sensors"] == [
            {
                "sensor_id": "S2",
                "reason": "uncovered",
                "value": None,
                "unit": None,
                "missing_ancestors": [],
            }
        ]


def test_release_lists_every_failing_sensor_sorted(client):
    _post(client, _event("v1", "ROOT", 0, 100, value=9.0, sensor="S1", key="k1"))
    # S2 exists but covers nothing inside the query window: uncovered there.
    _post(client, _event("w1", "ROOT", 200, 300, value=1.0, sensor="S2", key="k2"))
    body = _release(
        client,
        end=100,
        sensors=[
            {"sensor_id": "S2", "min_value": 0.0, "max_value": 4.0, "unit": "C"},
            {"sensor_id": "S1", "min_value": 0.0, "max_value": 4.0, "unit": "C"},
        ],
    ).json()
    assert body["releasable"] is False
    [interval] = body["rejected_intervals"]
    assert interval["reason"] == "sensors_disqualified"
    # Reasons are listed per sensor, sorted by sensor_id (not request order).
    assert [(s["sensor_id"], s["reason"]) for s in interval["sensors"]] == [
        ("S1", "out_of_range"),
        ("S2", "uncovered"),
    ]
    assert interval["sensors"][0]["value"] == 9.0
    assert interval["sensors"][0]["unit"] == "C"


def test_release_out_of_range_splits_release_windows(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0))
    _post(client, _event("v2", "v1", 100, 200, value=3.5, key="k2"))
    body = _release(client, sensors=[
        {"sensor_id": "S1", "min_value": 0.0, "max_value": 3.0, "unit": "C"}
    ]).json()
    assert [(w["start_ns"], w["end_ns"]) for w in body["release_windows"]] == [
        (0, 100),
        (200, 300),
    ]
    [interval] = body["rejected_intervals"]
    assert (interval["start_ns"], interval["end_ns"], interval["duration_ns"]) == (
        100,
        200,
        100,
    )
    assert interval["reason"] == "sensors_disqualified"
    assert interval["sensors"] == [
        {
            "sensor_id": "S1",
            "reason": "out_of_range",
            "value": 3.5,
            "unit": "C",
            "missing_ancestors": [],
        }
    ]


def test_release_unit_mismatch(client):
    _post(client, _event("v1", "ROOT", 0, 100, value=2.0, unit="C"))
    body = _release(
        client,
        end=100,
        sensors=[{"sensor_id": "S1", "min_value": 0.0, "max_value": 4.0, "unit": "F"}],
    ).json()
    assert body["release_windows"] == []
    [interval] = body["rejected_intervals"]
    assert interval["reason"] == "sensors_disqualified"
    assert interval["sensors"][0]["reason"] == "unit_mismatch"
    # The actual calibrated payload is reported for diagnosis.
    assert interval["sensors"][0]["value"] == 2.0
    assert interval["sensors"][0]["unit"] == "C"


def test_release_withdrawn_and_uncovered_spans(client):
    _post(client, _event("v1", "ROOT", 0, 100, value=2.0))
    _post(client, _event("w1", "v1", 40, 60, withdrawn=True, key="kw"))
    body = _release(client, end=150).json()
    assert [(w["start_ns"], w["end_ns"]) for w in body["release_windows"]] == [
        (0, 40),
        (60, 100),
    ]
    rejected = body["rejected_intervals"]
    assert [(i["start_ns"], i["end_ns"], i["sensors"][0]["reason"]) for i in rejected] == [
        (40, 60, "withdrawn"),
        (100, 150, "uncovered"),
    ]


def test_release_blocked_takes_precedence_over_grounded_coverage(client):
    # Grounded v1 covers [0,100) with an in-range value, but the blocked
    # orphan revision covers [20,80): backfilling "ghost" could change the
    # fold winner there, so the span waits for backfill instead of releasing.
    _post(client, _event("v1", "ROOT", 0, 100, value=2.0))
    _post(client, _event("orphan", "ghost", 20, 80, value=9.0, key="ko"))
    body = _release(client, end=100).json()
    assert [(w["start_ns"], w["end_ns"]) for w in body["release_windows"]] == [
        (0, 20),
        (80, 100),
    ]
    [interval] = body["rejected_intervals"]
    assert (interval["start_ns"], interval["end_ns"]) == (20, 80)
    assert interval["reason"] == "sensors_disqualified"
    assert interval["sensors"] == [
        {
            "sensor_id": "S1",
            "reason": "blocked",
            "value": None,
            "unit": None,
            "missing_ancestors": ["ghost"],
        }
    ]


def test_release_invalid_cycle_revisions_do_not_participate(client):
    _post(client, _event("v1", "ROOT", 0, 100, value=2.0))
    _post(client, _event("c1", "c2", 0, 50, value=9.0, key="kc1"))
    _post(client, _event("c2", "c1", 0, 50, value=9.0, key="kc2"))
    body = _release(client, end=100).json()
    # The parent cycle is excluded entirely: it neither blocks nor qualifies.
    assert body["release_windows"] == [
        {"start_ns": 0, "end_ns": 100, "duration_ns": 100}
    ]
    assert body["rejected_intervals"] == []


def test_release_duration_threshold(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0))
    _post(client, _event("v2", "v1", 100, 160, value=5.0, key="k2"))
    # Qualified runs: [0,100) = 100ns (too short) and [160,300) = 140ns.
    body = _release(client, min_duration_ns=120).json()
    assert body["releasable"] is True
    assert [(w["start_ns"], w["end_ns"], w["duration_ns"]) for w in body["release_windows"]] == [
        (160, 300, 140)
    ]
    rejected = body["rejected_intervals"]
    assert [(i["start_ns"], i["end_ns"], i["reason"]) for i in rejected] == [
        (0, 100, "duration_too_short"),
        (100, 160, "sensors_disqualified"),
    ]
    # A too-short qualified run carries no per-sensor failure reasons.
    assert rejected[0]["duration_ns"] == 100
    assert rejected[0]["sensors"] == []


def test_release_duration_threshold_exact_boundary(client):
    _post(client, _event("v1", "ROOT", 0, 100, value=2.0))
    # A run whose duration equals the threshold exactly is releasable.
    body = _release(client, end=100, min_duration_ns=100).json()
    assert body["release_windows"] == [
        {"start_ns": 0, "end_ns": 100, "duration_ns": 100}
    ]
    body = _release(client, end=100, min_duration_ns=101).json()
    assert body["release_windows"] == []
    assert body["rejected_intervals"][0]["reason"] == "duration_too_short"


def test_release_merges_adjacent_spans(client):
    # Same out-of-range payload on both sides of a revision boundary merges.
    _post(client, _event("v1", "ROOT", 0, 50, value=9.0, key="k1"))
    _post(client, _event("v2", "v1", 50, 100, value=9.0, key="k2"))
    body = _release(client, end=100).json()
    assert [(i["start_ns"], i["end_ns"]) for i in body["rejected_intervals"]] == [
        (0, 100)
    ]
    # Adjacent qualified spans across revision boundaries merge into one run.
    _post(client, _event("v3", "v2", 0, 100, value=2.0, key="k3"))
    body = _release(client, end=100).json()
    assert body["release_windows"] == [
        {"start_ns": 0, "end_ns": 100, "duration_ns": 100}
    ]


def test_release_does_not_merge_different_reason_payloads(client):
    _post(client, _event("v1", "ROOT", 0, 50, value=9.0, key="k1"))
    _post(client, _event("v2", "v1", 50, 100, value=8.0, key="k2"))
    body = _release(client, end=100).json()
    # Both out_of_range but with different observed values: kept separate.
    assert [
        (i["start_ns"], i["end_ns"], i["sensors"][0]["value"])
        for i in body["rejected_intervals"]
    ] == [(0, 50, 9.0), (50, 100, 8.0)]


def test_release_window_clips_coverage_to_query_range(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0))
    body = _release(client, start=50, end=150).json()
    assert body["release_windows"] == [
        {"start_ns": 50, "end_ns": 150, "duration_ns": 100}
    ]
    assert body["rejected_intervals"] == []


def test_release_half_open_boundary(client):
    # Two abutting fragments share no instant: [0,100) and [100,200).
    _post(client, _event("v1", "ROOT", 0, 100, value=2.0, key="k1"))
    _post(client, _event("v2", "v1", 100, 200, value=9.0, key="k2"))
    body = _release(client, start=0, end=200).json()
    assert [(w["start_ns"], w["end_ns"]) for w in body["release_windows"]] == [(0, 100)]
    assert [(i["start_ns"], i["end_ns"]) for i in body["rejected_intervals"]] == [
        (100, 200)
    ]


def test_release_unknown_batch_or_sensor_404(client):
    assert _release(client).status_code == 404
    _post(client, _event("v1", "ROOT", 0, 10, value=1.0, sensor="SX", key="kx"))
    r = _release(client)
    assert r.status_code == 404
    assert "S1" in r.json()["detail"] and "B1" in r.json()["detail"]
    # The known sensor evaluates fine; an unknown second sensor is reported.
    assert _release(client, end=10, sensors=[
        {"sensor_id": "SX", "min_value": 0.0, "max_value": 4.0, "unit": "C"}
    ]).status_code == 200
    r = _release(client, end=10, sensors=[
        {"sensor_id": "SX", "min_value": 0.0, "max_value": 4.0, "unit": "C"},
        {"sensor_id": "S1", "min_value": 0.0, "max_value": 4.0, "unit": "C"},
    ])
    assert r.status_code == 404
    assert "S1" in r.json()["detail"]


def test_release_invalid_params_422(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0))
    # Empty / inverted query window.
    assert _release(client, start=300, end=300).status_code == 422
    assert _release(client, start=200, end=100).status_code == 422
    assert _release(client, start=-1).status_code == 422
    # Non-positive duration threshold.
    assert _release(client, min_duration_ns=0).status_code == 422
    # Empty sensor list.
    assert _release(client, sensors=[]).status_code == 422
    # Duplicate sensor ids.
    dup = [{"sensor_id": "S1", "min_value": 0.0, "max_value": 4.0, "unit": "C"}]
    assert _release(client, sensors=dup + dup).status_code == 422
    # Inverted bounds.
    inverted = [{"sensor_id": "S1", "min_value": 4.0, "max_value": 0.0, "unit": "C"}]
    assert _release(client, sensors=inverted).status_code == 422
    # Missing required fields / unknown extra field.
    assert client.post("/api/v1/batches/B1/release", json={}).status_code == 422
    assert _release(client, tolerance=0.1).status_code == 422


def test_release_non_finite_bounds_422(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0))
    # NaN/Infinity are not valid JSON floats for the test client, so post the
    # raw (non-strict) JSON the way lenient clients would.
    for bad in ("NaN", "Infinity", "-Infinity"):
        raw = (
            '{"start_ns":0,"end_ns":300,"min_duration_ns":1,'
            '"sensors":[{"sensor_id":"S1","min_value":%s,'
            '"max_value":4.0,"unit":"C"}]}' % bad
        )
        r = client.post(
            "/api/v1/batches/B1/release",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 422, bad


def test_release_deterministic_across_arrival_order(client):
    events = [
        _event("r", "ROOT", 0, 300, value=2.0, key="kr"),
        _event("a", "r", 0, 150, value=3.0, key="ka"),
        _event("b", "r", 100, 250, value=9.0, key="kb"),
        _event("w", "a", 120, 180, withdrawn=True, key="kw"),
        _event("orphan", "ghost", 200, 220, value=1.0, key="ko"),
    ]

    def run(order):
        client.post("/admin/reset")
        for e in order:
            assert _post(client, e).status_code == 200
        return _release(client, min_duration_ns=10).json()

    first = run(events)
    assert run(list(reversed(events))) == first
    assert run([events[2], events[4], events[0], events[3], events[1]]) == first
    # And the folded input hash is independent of the query parameters.
    assert (
        _release(client, min_duration_ns=999).json()["timeline_hash"]
        == first["timeline_hash"]
    )


def test_release_is_read_only(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=2.0))
    before = client.get("/api/v1/batches/B1/result").json()
    assert _release(client).status_code == 200
    after = client.get("/api/v1/batches/B1/result").json()
    assert after == before


def test_openapi_lists_release_endpoint(client):
    spec = client.get("/openapi.json").json()
    path = spec["paths"]["/api/v1/batches/{batch_id}/release"]["post"]
    ref = path["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/ReleaseResponse")
    props = spec["components"]["schemas"]["ReleaseRequest"]["properties"]
    assert {"start_ns", "end_ns", "min_duration_ns", "sensors"} <= set(props)
    sensor_props = spec["components"]["schemas"]["ReleaseSensorRequirement"][
        "properties"
    ]
    assert {"sensor_id", "min_value", "max_value", "unit"} <= set(sensor_props)
