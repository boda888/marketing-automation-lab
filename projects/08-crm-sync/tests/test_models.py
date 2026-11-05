from crmsync.models import (
    Outcome,
    RowOutcome,
    count_outcomes,
    is_fixture_email,
    normalise_email,
    property_value,
)


def test_normalise_email_drops_placeholders_and_non_addresses():
    assert normalise_email("  Ana@Northwind.Example ") == "ana@northwind.example"
    for bad in (None, "", "Unknown", "N/A", "not an email", "a@b"):
        assert normalise_email(bad) is None


def test_fixture_emails_are_flagged():
    assert is_fixture_email("ana@northwind.example")
    assert is_fixture_email("ana@sub.example.com")
    assert is_fixture_email("qa@shop.test")
    assert not is_fixture_email("ana@exampleco.io")


def test_property_value_never_blanks_a_field_but_keeps_zero_and_false():
    assert property_value(None) is None
    assert property_value("  ") is None
    assert property_value("Unknown") is None
    assert property_value(0) == "0"
    assert property_value(False) == "false"
    assert property_value(True) == "true"
    assert property_value(2.5) == "2.5"
    assert property_value(" Tier 1 ") == "Tier 1"


def test_count_outcomes_lists_every_state():
    counts = count_outcomes(
        [
            RowOutcome("1", "a@x.io", Outcome.CREATED),
            RowOutcome("2", "b@x.io", Outcome.FAILED),
            RowOutcome("3", "b@x.io", Outcome.FAILED),
        ]
    )
    assert counts == {
        "created": 1,
        "updated": 0,
        "upserted": 0,
        "failed": 2,
        "skipped": 0,
    }
