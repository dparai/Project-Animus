import re
from datetime import datetime, timedelta, timezone

import pytest

from app import db, entries
from app.main import create_app
from tests.auth_helpers import login_client


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "entries-page.db")


@pytest.fixture
def client(db_path):
    return login_client(create_app(db_path, allowed_hosts=["testserver"]))


def _add(db_path, text, when="2026-10-31T01:32:00", mood=None, energy=None, approx=False):
    c = db.connect(db_path)
    try:
        return entries.create_entry(
            c,
            text=text,
            mood=mood,
            energy=energy,
            now=datetime.fromisoformat(when).replace(tzinfo=timezone.utc),
            tz="America/Los_Angeles",
            energy_is_approximate=approx,
        )
    finally:
        c.close()


def _row(db_path, entry_id):
    c = db.connect(db_path)
    try:
        return c.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    finally:
        c.close()


def test_list_shows_entries_newest_first(client, db_path):
    _add(db_path, "oldest entry", "2026-10-01T10:00:00")
    _add(db_path, "newest entry", "2026-10-30T10:00:00")
    _add(db_path, "middle entry", "2026-10-15T10:00:00")
    html = client.get("/entries").text
    assert html.index("newest entry") < html.index("middle entry") < html.index("oldest entry")


def test_list_shows_local_date_and_time_in_stored_timezone(client, db_path):
    _add(db_path, "evening", "2026-10-31T01:32:00")  # 18:32 Pacific on Friday 30 October
    html = client.get("/entries").text
    assert "Friday 30 October, 6:32 PM" in html


def test_list_shows_mood_and_energy_honestly(client, db_path):
    _add(db_path, "precise", mood="good", energy=7)
    _add(db_path, "imported", when="2026-10-30T01:32:00", mood="low", energy=5, approx=True)
    _add(db_path, "bare", when="2026-10-29T01:32:00")
    html = client.get("/entries").text
    assert "Good" in html and "about 5" in html
    precise, imported, bare = html.split('<li id="entry-')[1:]
    assert "about" not in precise and "about 5" in imported
    assert "No mood" in bare and "not set" in bare.lower()


def test_empty_state(client):
    html = client.get("/entries").text
    assert "Nothing here yet. Your first entry will show up on this page." in html


def test_html_in_text_is_escaped(client, db_path):
    client.post("/entries", data={"text": "<script>alert(1)</script>"})
    html = client.get("/entries").text
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)" not in html
    assert _row_text(db_path) == "<script>alert(1)</script>"


def _row_text(db_path):
    c = db.connect(db_path)
    try:
        return entries.list_entries(c)[0]["text"]
    finally:
        c.close()


def test_edit_fragment_prefills_and_escapes(client, db_path):
    eid = _add(db_path, 'say "hi" <b>x</b>', mood="okay", energy=4)
    r = client.get(f"/entries/{eid}/edit")
    assert r.status_code == 200
    assert "<textarea" in r.text and "&lt;b&gt;x&lt;/b&gt;" in r.text
    assert "<b>x</b>" not in r.text
    assert f'hx-put="/entries/{eid}"' in r.text


def test_edit_updates_row_and_fts(client, db_path):
    eid = _add(db_path, "before change", mood="good", energy=3)
    r = client.put(f"/entries/{eid}", data={"text": "after zebra change", "mood": "sad", "energy": "9"})
    assert r.status_code == 200
    assert "after zebra change" in r.text and f'id="entry-{eid}"' in r.text
    row = _row(db_path, eid)
    assert (row["text"], row["mood"], row["energy_score"]) == ("after zebra change", "sad", 9)
    c = db.connect(db_path)
    try:
        hits = [x["rowid"] for x in c.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'zebra'")]
        old = c.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'before'").fetchall()
    finally:
        c.close()
    assert hits == [eid] and old == []


def test_edit_can_clear_mood_and_energy(client, db_path):
    eid = _add(db_path, "keep text", mood="good", energy=3)
    r = client.put(f"/entries/{eid}", data={"text": "keep text", "mood": "", "energy": ""})
    assert r.status_code == 200
    row = _row(db_path, eid)
    assert row["mood"] is None and row["energy_score"] is None


def test_edit_blank_text_422_keeps_old_text(client, db_path):
    eid = _add(db_path, "original words", mood="good", energy=3)
    r = client.put(f"/entries/{eid}", data={"text": "   ", "mood": "good", "energy": "3"})
    assert r.status_code == 422
    assert "text/html" in r.headers["content-type"]
    assert "Write something" in r.text
    assert _row(db_path, eid)["text"] == "original words"


def test_edit_bad_energy_and_mood_422(client, db_path):
    eid = _add(db_path, "x words")
    assert client.put(f"/entries/{eid}", data={"text": "ok", "energy": "11"}).status_code == 422
    assert client.put(f"/entries/{eid}", data={"text": "ok", "energy": "abc"}).status_code == 422
    assert client.put(f"/entries/{eid}", data={"text": "ok", "mood": "ecstatic"}).status_code == 422
    assert _row(db_path, eid)["text"] == "x words"


def test_changing_energy_clears_approximate_flag(client, db_path):
    eid = _add(db_path, "imported", energy=5, approx=True)
    client.put(f"/entries/{eid}", data={"text": "imported", "energy": "5"})
    assert _row(db_path, eid)["energy_is_approximate"] == 1
    client.put(f"/entries/{eid}", data={"text": "imported", "energy": "6"})
    assert _row(db_path, eid)["energy_is_approximate"] == 0


def test_row_fragment_for_cancel(client, db_path):
    eid = _add(db_path, "cancel me")
    r = client.get(f"/entries/{eid}")
    assert r.status_code == 200 and "cancel me" in r.text and "<html" not in r.text


def test_delete_removes_row(client, db_path):
    keep = _add(db_path, "keep this")
    gone = _add(db_path, "delete this")
    r = client.delete(f"/entries/{gone}")
    assert r.status_code == 200 and r.text == ""
    assert _row(db_path, gone) is None
    assert _row(db_path, keep) is not None


def test_delete_button_asks_for_confirmation(client, db_path):
    eid = _add(db_path, "some words")
    html = client.get("/entries").text
    assert f'hx-delete="/entries/{eid}"' in html
    assert 'hx-confirm="Delete this entry?"' in html


def test_unknown_id_404(client, db_path):
    assert client.get("/entries/999").status_code == 404
    assert client.get("/entries/999/edit").status_code == 404
    assert client.put("/entries/999", data={"text": "hello"}).status_code == 404
    assert client.delete("/entries/999").status_code == 404
    c = db.connect(db_path)
    assert entries.list_entries(c) == []
    c.close()


def test_header_links_between_write_and_entries(client):
    assert 'href="/entries"' in client.get("/").text
    assert 'href="/"' in client.get("/entries").text


# ---- paging (keyset by created_at, id) ----

def _bulk(db_path, n, when="2026-10-01T10:00:00", same_time=False):
    c = db.connect(db_path)
    try:
        base = datetime.fromisoformat(when).replace(tzinfo=timezone.utc)
        for i in range(n):
            now = base if same_time else base + timedelta(minutes=i)
            entries.create_entry(c, text=f"fake entry {i:03d}", mood=None, energy=None,
                                 now=now, tz="America/Los_Angeles")
    finally:
        c.close()


def _texts(html):
    return re.findall(r"fake entry (\d{3})", html)


def _more_href(html):
    m = re.search(r'href="(/entries\?before=[^"]+)"', html)
    return m.group(1).replace("&amp;", "&") if m else None


def test_page_one_has_100_and_older_link(client, db_path):
    _bulk(db_path, 105)
    html = client.get("/entries").text
    assert len(_texts(html)) == 100
    assert _texts(html)[0] == "104" and _texts(html)[-1] == "005"
    assert "Older entries" in html
    assert _more_href(html)
    assert "Showing the 100 most recent" not in html


def test_page_two_has_remaining_5_and_no_link(client, db_path):
    _bulk(db_path, 105)
    href = _more_href(client.get("/entries").text)
    html = client.get(href).text
    assert _texts(html) == ["004", "003", "002", "001", "000"]
    assert "Older entries" not in html
    assert _more_href(html) is None
    assert "That is every entry" in html


def test_htmx_request_gets_rows_fragment_only(client, db_path):
    _bulk(db_path, 105)
    href = _more_href(client.get("/entries").text)
    frag = client.get(href, headers={"HX-Request": "true"}).text
    assert "<html" not in frag
    assert _texts(frag) == ["004", "003", "002", "001", "000"]


def test_exactly_100_entries_shows_no_link(client, db_path):
    _bulk(db_path, 100)
    html = client.get("/entries").text
    assert len(_texts(html)) == 100
    assert "Older entries" not in html and _more_href(html) is None


def test_ties_on_created_at_are_not_skipped_or_duplicated(client, db_path):
    _bulk(db_path, 105, same_time=True)
    seen = []
    href = "/entries"
    while href:
        html = client.get(href).text
        seen += _texts(html)
        href = _more_href(html)
    assert len(seen) == 105 and len(set(seen)) == 105
    assert seen == [f"{i:03d}" for i in range(104, -1, -1)]  # newest id first


def test_bad_cursor_is_400(client):
    assert client.get("/entries?before=nonsense").status_code == 400


# ---- day comes from local_date ----

def _set_tz(client, name):
    assert client.post("/timezone", data={"tz": name}).status_code == 204


def test_list_day_is_local_date_and_time_is_current_timezone(client, db_path):
    c = db.connect(db_path)
    c.execute("INSERT INTO entries(created_at, local_date, text) VALUES (?, ?, ?)",
              ("2026-10-31T01:32:15Z", "2026-10-30", "written in Los Angeles"))
    c.commit(); c.close()
    _set_tz(client, "Asia/Tokyo")  # now 10:32 on the 31st locally
    html = client.get("/entries").text
    assert "Friday 30 October, 10:32" in html
    assert "Saturday 31 October" not in html


def test_list_prefers_stored_local_date_over_derived_day(client, db_path):
    c = db.connect(db_path)
    c.execute("INSERT INTO entries(created_at, local_date, text) VALUES (?, ?, ?)",
              ("2026-10-31T01:32:15Z", "2026-10-29", "odd local date"))
    c.commit(); c.close()
    html = client.get("/entries").text
    assert "Thursday 29 October, 6:32 PM" in html
