from safety_rules import decide


def _d(**kw):
    base = {
        "threat": "unrest",
        "threat_distance": "same_street",
        "threat_trend": "stable",
        "shelter_secure": True,
        "exit_route_clear": True,
        "anyone_injured": False,
        "caller_can_move": True,
        "door_hot": False,
        "trust_police": True,
    }
    base.update(kw)
    return decide(**base)


def test_r1_injured():
    d = _d(anyone_injured=True)
    assert (d.action, d.rule, d.escalate, d.check_in_minutes) == (
        "CALL_AUTHORITIES",
        "R1",
        True,
        None,
    )


def test_r1_medical():
    assert _d(threat="medical").rule == "R1"


def test_r1_fire_trapped():
    d = _d(threat="fire", threat_distance="inside_building", caller_can_move=False)
    assert d.rule == "R1"


def test_r1_violence_inside():
    assert _d(threat="violence_nearby", threat_distance="inside_building").rule == "R1"


def test_r1_untrusted_police_crime():
    d = _d(threat="crime_followed", anyone_injured=True, trust_police=False)
    assert d.rule == "R1"
    assert "embassy" in d.next_step.lower()
    assert d.escalate


def test_r1_trusted_police_crime_uses_emergency_number():
    d = _d(threat="crime_followed", anyone_injured=True, trust_police=True)
    assert "embassy" not in d.next_step.lower()


def test_r2b_hot_door():
    d = _d(threat="fire", threat_distance="inside_building", door_hot=True)
    assert (d.action, d.rule, d.check_in_minutes, d.escalate) == (
        "SHELTER",
        "R2b",
        2,
        True,
    )


def test_r2_fire_can_move():
    d = _d(threat="fire", threat_distance="inside_building")
    assert (d.action, d.rule, d.check_in_minutes, d.escalate) == (
        "LEAVE",
        "R2",
        2,
        False,
    )
    assert "stairs" in d.next_step


def test_r3_approaching():
    d = _d(threat_trend="approaching")
    assert (d.action, d.rule, d.check_in_minutes) == ("LEAVE", "R3", 3)


def test_r3_crime_followed_script():
    d = _d(threat="crime_followed", threat_trend="approaching")
    assert d.rule == "R3"
    assert "busy, staffed business" in d.next_step


def test_r4_shelter_stable():
    d = _d()
    assert (d.action, d.rule, d.check_in_minutes) == ("SHELTER", "R4", 20)


def test_r4_approaching_but_no_exit():
    d = _d(threat_trend="approaching", exit_route_clear=False)
    assert d.rule == "R4"


def test_r5_not_secure_leave():
    d = _d(threat_distance="far", shelter_secure=False)
    assert (d.action, d.rule, d.check_in_minutes) == ("LEAVE", "R5", 3)


def test_r5_crime_vehicle_script():
    d = _d(threat="crime_vehicle", threat_distance="far", shelter_secure=False)
    assert d.rule == "R5"
    assert "don't confront" in d.next_step


def test_r6_lost_documents():
    d = _d(threat="lost_documents", threat_distance="far")
    assert (d.action, d.rule, d.check_in_minutes, d.escalate) == (
        "CALL_AUTHORITIES",
        "R6",
        None,
        False,
    )


def test_r6_crime_far():
    d = _d(threat="crime_followed", threat_distance="far")
    assert d.rule == "R6"


def test_r7_default():
    d = _d(threat="other", threat_distance="unknown", threat_trend="unknown")
    assert (d.action, d.rule, d.check_in_minutes) == ("SHELTER", "R7", 10)


def test_spoken_format():
    s = _d().spoken()
    assert s.startswith("SHELTER. Reason: ")
    assert "Suggested check-in: 20 minutes." in s
    assert "Suggested check-in" not in _d(anyone_injured=True).spoken()
