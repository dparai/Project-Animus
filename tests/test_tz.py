import pytest

from app import tz


def test_default_when_unset(conn):
    assert tz.get_timezone(conn) == tz.DEFAULT_TZ == "America/Los_Angeles"


def test_set_valid_persists_and_reports_change(conn):
    assert tz.set_timezone(conn, "Asia/Tokyo") is True
    assert tz.get_timezone(conn) == "Asia/Tokyo"
    assert tz.set_timezone(conn, "Asia/Tokyo") is False


@pytest.mark.parametrize("bad", ["Not/AZone", ""])
def test_invalid_name_rejected_and_value_kept(conn, bad):
    tz.set_timezone(conn, "Asia/Tokyo")
    with pytest.raises(ValueError):
        tz.set_timezone(conn, bad)
    assert tz.get_timezone(conn) == "Asia/Tokyo"
