import re
from datetime import datetime, timedelta, timezone

import pytest

from app import db, entries
from app.main import create_app
from tests.auth_helpers import login_client

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
NOTHING = "Nothing here yet"


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "empty.db")


@pytest.fixture
def client(db_path):
    return login_client(create_app(db_path, allowed_hosts=["testserver"]))


def add(db_path, n):
    c = db.connect(db_path)
    try:
        return [entries.create_entry(c, text=f"fake entry {i}", mood=None, energy=None,
                                     now=T0 + timedelta(minutes=i), tz="UTC",
                                     energy_is_approximate=False) for i in range(n)]
    finally:
        c.close()


def empty_div(html):
    m = re.search(r'<div id="empty"([^>]*)>', html)
    assert m, "empty-state container missing"
    return m.group(1)


def is_hidden(html):
    return " hidden" in empty_div(html)


def test_no_entries_shows_empty_state(client):
    html = client.get("/entries").text
    assert NOTHING in html and not is_hidden(html)


def test_deleting_last_remaining_entry_shows_empty_state(client, db_path):
    (eid,) = add(db_path, 1)
    assert is_hidden(client.get("/entries").text)
    assert client.delete(f"/entries/{eid}").status_code == 200
    html = client.get("/entries").text
    assert NOTHING in html and not is_hidden(html)
    assert "That is every entry" not in html


def test_deleting_while_more_exist_never_shows_empty_state(client, db_path):
    ids = add(db_path, 3)
    for eid in ids[:-1]:
        client.delete(f"/entries/{eid}")
        html = client.get("/entries").text
        assert is_hidden(html) and "fake entry" in html
    assert "That is every entry" in html


def test_page_with_older_link_hides_empty_state(client, db_path):
    add(db_path, 101)
    html = client.get("/entries").text
    assert "Older entries" in html and is_hidden(html)


def test_paging_cursor_survives_deletes(client, db_path):
    ids = add(db_path, 103)  # newest first: pages of 100, then 3
    first = client.get("/entries").text
    cursor = re.search(r'href="/entries\?before=([^"]+)"', first).group(1)
    # Delete the entry the cursor points at and some of the page above it.
    client.delete(f"/entries/{ids[3]}")  # the 100th newest, i.e. the cursor row
    for eid in ids[50:60]:
        client.delete(f"/entries/{eid}")
    page2 = client.get(f"/entries?before={cursor}", headers={"HX-Request": "true"}).text
    assert page2.count('class="entry"') == 3
    assert "That is every entry" in page2
    full = client.get(f"/entries?before={cursor}").text
    assert is_hidden(full)


def test_stale_cursor_with_nothing_older_is_not_called_nothing_yet(client, db_path):
    ids = add(db_path, 101)
    cursor = re.search(r'href="/entries\?before=([^"]+)"', client.get("/entries").text).group(1)
    client.delete(f"/entries/{ids[0]}")  # the only entry older than the cursor
    html = client.get(f"/entries?before={cursor}").text
    assert NOTHING not in html
    assert "No older entries here." in html and not is_hidden(html)
    assert 'href="/entries"' in html


def test_script_syncs_empty_state_with_list(client):
    html = client.get("/entries").text
    assert "MutationObserver" in html and ".btn-older" in html
    assert "htmx:afterSwap" not in html
