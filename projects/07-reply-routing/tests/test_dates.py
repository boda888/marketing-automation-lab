from datetime import date

import pytest
from replyrouting import extract_return_date

RECEIVED = date(2026, 11, 12)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I will be back on Monday 23 November.", date(2026, 11, 23)),
        ("Returning on November 30th, 2026", date(2026, 11, 30)),
        ("I am away until 2026-12-04", date(2026, 12, 4)),
        ("back in the office on 24.11.2026", date(2026, 11, 24)),
        ("away till 7 Dec", date(2026, 12, 7)),
        ("back on 24/11", date(2026, 11, 24)),
        ("back 11/24/26", date(2026, 11, 24)),
        ("available again from the 3rd of December", date(2026, 12, 3)),
        # Year rolls over when the day already passed this year.
        ("back on 5 January", date(2027, 1, 5)),
    ],
)
def test_return_dates(text, expected):
    assert extract_return_date(text, RECEIVED).value == expected


def test_back_beats_until_regardless_of_order():
    text = "Out of office until 22 November. I am back on 24 November."
    assert extract_return_date(text, RECEIVED).value == date(2026, 11, 24)


def test_from_is_the_start_not_the_return():
    text = "I am on leave from 10 November until 25 November"
    assert extract_return_date(text, RECEIVED).value == date(2026, 11, 25)


def test_time_of_day_is_not_a_date():
    r = extract_return_date("back at 9.30 tomorrow", RECEIVED)
    assert r.value is None


def test_ambiguous_numeric_is_not_guessed():
    r = extract_return_date("back on 03/04/2027", RECEIVED)
    assert r.value is None
    assert r.note.startswith("ambiguous")


def test_invalid_calendar_date():
    r = extract_return_date("back on 31 February", RECEIVED)
    assert r.value is None
    assert r.note.startswith("invalid")


def test_date_without_cue_is_ignored():
    r = extract_return_date("Our conference is on 3 December.", RECEIVED)
    assert r.value is None
    assert r.note == "no date after a return cue"


def test_date_in_next_sentence_is_ignored():
    r = extract_return_date("I am back soon. The office closes 24 December.", RECEIVED)
    assert r.value is None
