from scripts.import_dayora import ImportReport, import_dayora

TZ = "America/Los_Angeles"


def rows(conn):
    return conn.execute("SELECT * FROM entries ORDER BY id").fetchall()


def test_maps_mood_energy_and_marks_approximate(conn, write_export, fake_entry):
    path = write_export([fake_entry("a1", "2026-09-01T18:00:00.000Z", mood="good", energy="high")])
    report = import_dayora(conn, path, TZ)
    assert report.imported == 1
    r = rows(conn)[0]
    assert (r["mood"], r["energy_score"], r["energy_is_approximate"]) == ("good", 8, 1)
    assert r["source_id"] == "a1"
    assert r["created_at"] == "2026-09-01T18:00:00Z"


def test_energy_low_medium_map(conn, write_export, fake_entry):
    path = write_export([
        fake_entry("a1", "2026-09-01T18:00:00.000Z", energy="low"),
        fake_entry("a2", "2026-09-02T18:00:00.000Z", energy="medium"),
    ])
    import_dayora(conn, path, TZ)
    assert [r["energy_score"] for r in rows(conn)] == [3, 5]


def test_late_pacific_entry_lands_on_pacific_day(conn, write_export, fake_entry):
    path = write_export([fake_entry("a1", "2026-10-31T01:32:15.046Z")])
    import_dayora(conn, path, TZ)
    assert rows(conn)[0]["local_date"] == "2026-10-30"


def test_null_mood_and_energy_stay_null(conn, write_export, fake_entry):
    path = write_export([fake_entry("a1", "2026-09-01T18:00:00.000Z")])
    import_dayora(conn, path, TZ)
    r = rows(conn)[0]
    assert r["mood"] is None and r["energy_score"] is None
    assert r["energy_is_approximate"] == 0


def test_mood_filled_from_moodscore_only_when_null(conn, write_export, fake_entry):
    path = write_export(
        [
            fake_entry("a1", "2026-09-01T18:00:00.000Z"),
            fake_entry("a2", "2026-09-02T18:00:00.000Z", mood="rough"),
        ],
        [
            {"id": "m1", "date": "2026-09-01T17:00:00.000Z", "moodScore": 4},
            {"id": "m2", "date": "2026-09-02T17:00:00.000Z", "moodScore": 4},
        ],
    )
    report = import_dayora(conn, path, TZ)
    assert [r["mood"] for r in rows(conn)] == ["good", "rough"]
    assert report.mood_filled == 1


def test_mood_entries_do_not_create_entries(conn, write_export):
    path = write_export([], [{"id": "m1", "date": "2026-09-01T17:00:00.000Z", "moodScore": 3}])
    report = import_dayora(conn, path, TZ)
    assert report == ImportReport(0, 0, 0, 0)
    assert rows(conn) == []


def test_rerun_creates_no_duplicates(conn, write_export, fake_entry):
    path = write_export([
        fake_entry("a1", "2026-09-01T18:00:00.000Z"),
        fake_entry("a2", "2026-09-02T18:00:00.000Z"),
    ])
    assert import_dayora(conn, path, TZ).imported == 2
    again = import_dayora(conn, path, TZ)
    assert again.imported == 0
    assert again.skipped_duplicates == 2
    assert len(rows(conn)) == 2


def test_empty_content_skipped(conn, write_export, fake_entry):
    path = write_export([
        fake_entry("a1", "2026-09-01T18:00:00.000Z", content="   "),
        fake_entry("a2", "2026-09-02T18:00:00.000Z"),
    ])
    report = import_dayora(conn, path, TZ)
    assert (report.imported, report.skipped_empty) == (1, 1)


def test_two_entries_same_day_both_imported(conn, write_export, fake_entry):
    path = write_export([
        fake_entry("a1", "2026-09-01T16:00:00.000Z"),
        fake_entry("a2", "2026-09-01T20:00:00.000Z"),
    ])
    assert import_dayora(conn, path, TZ).imported == 2
    assert len({r["local_date"] for r in rows(conn)}) == 1
