from datetime import date, timedelta

from app import mood_streak

TODAY = date(2026, 11, 1)


def add(conn, day, mood):
    conn.execute(
        "INSERT INTO entries(created_at, local_date, text, mood) VALUES (?, ?, 'fake text', ?)",
        (f"{day}T12:00:00Z", day.isoformat(), mood),
    )
    conn.commit()


def test_low_mood_hint_true_at_five_days(conn):
    for i in range(5):
        add(conn, TODAY - timedelta(days=i), "low" if i % 2 else "sad")
    add(conn, TODAY - timedelta(days=5), "good")
    assert mood_streak.low_mood_hint(conn, TODAY) is True


def test_low_mood_hint_false_at_four_days(conn):
    for i in range(4):
        add(conn, TODAY - timedelta(days=i), "low")
    add(conn, TODAY - timedelta(days=4), "okay")
    add(conn, TODAY - timedelta(days=7), "low")  # outside the 7-day window
    add(conn, TODAY - timedelta(days=8), "sad")
    assert mood_streak.low_mood_hint(conn, TODAY) is False


def test_low_mood_counts_days_not_entries(conn):
    for _ in range(3):
        add(conn, TODAY, "low")
        add(conn, TODAY - timedelta(days=1), "sad")
    for i in (2, 3):
        add(conn, TODAY - timedelta(days=i), "low")
    assert mood_streak.low_mood_hint(conn, TODAY) is False  # 4 distinct days, 8 entries
    add(conn, TODAY - timedelta(days=6), "sad")
    assert mood_streak.low_mood_hint(conn, TODAY) is True


def test_low_mood_ignores_future_days_and_custom_params(conn):
    for i in range(1, 6):
        add(conn, TODAY + timedelta(days=i), "low")
    assert mood_streak.low_mood_hint(conn, TODAY) is False
    add(conn, TODAY, "rough")
    add(conn, TODAY - timedelta(days=1), "rough")
    assert mood_streak.low_mood_hint(conn, TODAY, days=2, threshold=2, low_moods=("rough",)) is True
    assert mood_streak.low_mood_hint(conn, TODAY, low_moods=()) is False
