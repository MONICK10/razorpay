from engine import reference


def test_repair_handles_dropped_separator():
    assert reference.repair("INV0087", ["INV-0087", "INV-0091"]) == ("INV-0087", 1.0)


def test_repair_handles_case():
    assert reference.repair("inv-0087", ["INV-0087"]) == ("INV-0087", 1.0)


def test_repair_handles_o_zero_confusion():
    inv_id, score = reference.repair("INV-O087", ["INV-0087"])
    assert inv_id == "INV-0087"
    assert score == 1.0


def test_repair_handles_truncation():
    inv_id, score = reference.repair("INV-008", ["INV-0087"])
    assert inv_id == "INV-0087"
    assert score >= 0.9


def test_repair_refuses_when_ambiguous():
    # Both candidates normalize close enough that neither clears the bar
    # uniquely -- must return None rather than guess.
    inv_id, score = reference.repair("INV-009", ["INV-0091", "INV-0099"])
    assert inv_id is None


def test_repair_refuses_with_no_candidates():
    assert reference.repair("INV-0087", []) == (None, 0.0)


def test_find_reference_candidates_requires_word_boundary():
    # "INV-0035X" must NOT count as an exact hit on "INV-0035" -- that's a
    # corrupted reference for L3 to repair, not an L1 exact match.
    found = reference.find_reference_candidates(
        "IMPS/Acme/INV-0035X", ["INV-0035"])
    assert found == []


def test_find_reference_candidates_exact_hit():
    found = reference.find_reference_candidates(
        "NEFT/Acme/INV-0035 SETTLEMENT", ["INV-0035", "INV-0099"])
    assert found == ["INV-0035"]
