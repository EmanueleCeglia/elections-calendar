from datetime import date

import pytest

from elections_calendar.dates import parse_period, split_rounds


@pytest.mark.parametrize(
    "text, year, start, end, precision",
    [
        ("18 April 2027", None, date(2027, 4, 18), date(2027, 4, 18), "day"),
        ("18 April", 2027, date(2027, 4, 18), date(2027, 4, 18), "day"),
        ("April 18, 2027", None, date(2027, 4, 18), date(2027, 4, 18), "day"),
        ("9–10 October", 2026, date(2026, 10, 9), date(2026, 10, 10), "day"),
        ("30 September - 1 October 2026", None, date(2026, 9, 30), date(2026, 10, 1), "day"),
        ("By 25 July", 2027, date(2027, 1, 1), date(2027, 7, 25), "deadline"),
        ("by October", 2027, date(2027, 1, 1), date(2027, 10, 31), "deadline"),
        ("No later than 23 March 2030", None, date(2030, 1, 1), date(2030, 3, 23), "deadline"),
        ("March", 2027, date(2027, 3, 1), date(2027, 3, 31), "month"),
        ("March 2028", None, date(2028, 3, 1), date(2028, 3, 31), "month"),
        ("late May", 2027, date(2027, 5, 21), date(2027, 5, 31), "range"),
        ("September-October 2028", None, date(2028, 9, 1), date(2028, 10, 31), "range"),
        ("Spring 2027", None, date(2027, 3, 1), date(2027, 5, 31), "season"),
        ("Autumn 2027", None, date(2027, 9, 1), date(2027, 11, 30), "season"),
        ("Q2 2027", None, date(2027, 4, 1), date(2027, 6, 30), "quarter"),
        ("first half of 2027", None, date(2027, 1, 1), date(2027, 6, 30), "half"),
        ("2027", None, date(2027, 1, 1), date(2027, 12, 31), "year"),
        ("TBD", 2027, date(2027, 1, 1), date(2027, 12, 31), "year"),
        ("2027-04-18", None, date(2027, 4, 18), date(2027, 4, 18), "day"),
    ],
)
def test_parse_period(text, year, start, end, precision):
    p = parse_period(text, year)
    assert p is not None
    assert (p.start, p.end, p.precision) == (start, end, precision)


def test_may_as_a_verb_is_not_a_month():
    assert parse_period("elections may be held", 2027) is None
    assert parse_period("May", 2027).precision == "month"


def test_unparseable_text():
    assert parse_period("", 2027) is None
    assert parse_period("Parliament", 2027) is None


@pytest.mark.parametrize(
    "text, first, second",
    [
        ("18 April 2027; 2 May 2027 if a second round is required", date(2027, 4, 18), date(2027, 5, 2)),
        ("25 October 2026 (run-off 1 November 2026)", date(2026, 10, 25), date(2026, 11, 1)),
        ("9-10 October 2026; runoff 16-17 October", date(2026, 10, 9), date(2026, 10, 16)),
        ("24 October 2026", date(2026, 10, 24), None),
    ],
)
def test_split_rounds(text, first, second):
    p, r2 = split_rounds(text, 2026)
    assert p.start == first
    assert r2 == second
