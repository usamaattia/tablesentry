from tablesentry.anomaly import detect


def test_needs_enough_history():
    assert detect(100, [100] * 6, sensitivity=4, min_history=7, min_scale=1) is None
    assert detect(100, [100] * 7, sensitivity=4, min_history=7, min_scale=1) is not None


def test_flags_a_spike_but_not_normal_noise():
    history = [1000, 1010, 990, 1005, 995, 1020, 985, 1000]
    assert not detect(1015, history, 4, 7, 1).is_anomaly
    v = detect(600, history, 4, 7, 1)
    assert v.is_anomaly and v.median == 1000 and v.score > 20


def test_flat_history_uses_the_floor():
    # A null rate that has always been exactly 0%: 0.3% is noise, 5% is not.
    assert not detect(0.3, [0.0] * 10, 4, 7, min_scale=0.5).is_anomaly
    assert detect(5.0, [0.0] * 10, 4, 7, min_scale=0.5).is_anomaly


def test_one_past_incident_does_not_blind_the_detector():
    # A mean/stdev detector would be desensitised by the earlier 0; median/MAD is not.
    history = [1000, 1005, 0, 995, 1010, 990, 1000, 1002]
    assert detect(500, history, 4, 7, 1).is_anomaly
