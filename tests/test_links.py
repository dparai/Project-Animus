from datetime import datetime, timedelta, timezone

import pytest

from app import entries, links
from app.llm import LLMError
from tests.fake_llm import FakeLLM

BASE = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def add(conn, text, days):
    return entries.create_entry(conn, text=text, mood=None, energy=None,
                                now=BASE + timedelta(days=days), tz="UTC")


def rows(conn):
    return conn.execute("SELECT * FROM entry_links ORDER BY id").fetchall()


def test_candidates_are_older_matching_entries_only(conn):
    old = add(conn, "feeling drained after the meeting with Sam", 0)
    add(conn, "lovely walk in the park", 1)
    new = add(conn, "feeling drained again after meeting Sam", 5)
    later = add(conn, "drained and meeting Sam again", 9)
    ids = [r["id"] for r in links.find_candidates(conn, new)]
    assert ids == [old]
    assert later not in ids and new not in ids


def test_no_candidates_means_no_llm_call(conn):
    add(conn, "unrelated gardening", 0)
    new = add(conn, "tough conversation with the landlord", 2)
    llm = FakeLLM()
    assert links.suggest_for_entry(conn, llm, new) == 0
    assert llm.calls == []


def test_suggestions_are_stored_as_suggested_and_validated(conn):
    old = add(conn, "dreading the tough conversation with Sam", 0)
    new = add(conn, "had the tough conversation with Sam and feel lighter", 3)
    llm = FakeLLM([{"links": [
        {"id": old, "type": "follow_up", "reason": "You dreaded this talk earlier."},
        {"id": 9999, "type": "related", "reason": "invented id"},
        {"id": old, "type": "related", "reason": "duplicate"},
    ]}])
    assert links.suggest_for_entry(conn, llm, new) == 1
    (row,) = rows(conn)
    assert (row["from_entry_id"], row["to_entry_id"]) == (new, old)
    assert (row["link_type"], row["source"], row["status"]) == ("follow_up", "ai", "suggested")
    assert llm.calls[0]["thinking"] is False


@pytest.mark.parametrize("bad", [
    {}, {"links": "x"}, {"links": [None, 5, {"id": "1", "type": "related"}]},
    {"links": [{"id": True, "type": "related"}]},
])
def test_malformed_model_output_stores_nothing(conn, bad):
    old = add(conn, "dreading the tough conversation with Sam", 0)
    new = add(conn, "had the tough conversation with Sam", 3)
    assert links.suggest_for_entry(conn, FakeLLM([bad]), new) == 0
    assert rows(conn) == []
    assert old


def test_bad_type_dropped_and_cap_applied(conn):
    ids = [add(conn, f"project Falcon update number {i}", i) for i in range(5)]
    new = add(conn, "project Falcon finally shipped", 10)
    llm = FakeLLM([{"links": [{"id": i, "type": "related", "reason": "x"} for i in ids]
                    + [{"id": ids[0], "type": "bogus"}]}])
    assert links.suggest_for_entry(conn, llm, new) == links.MAX_SUGGESTIONS


def test_llm_failure_is_swallowed(conn):
    add(conn, "dreading the tough conversation with Sam", 0)
    new = add(conn, "had the tough conversation with Sam", 3)
    llm = FakeLLM([LLMError("boom", retryable=True, reason="timeout")])
    assert links.suggest_for_entry(conn, llm, new) == 0
    assert rows(conn) == []


def test_dismissed_pair_is_not_suggested_again(conn):
    old = add(conn, "dreading the tough conversation with Sam", 0)
    new = add(conn, "had the tough conversation with Sam", 3)
    pick = {"links": [{"id": old, "type": "follow_up", "reason": "r"}]}
    links.suggest_for_entry(conn, FakeLLM([pick]), new)
    (row,) = rows(conn)
    assert links.set_status(conn, new, row["id"], "dismissed")
    llm = FakeLLM([pick])
    assert links.suggest_for_entry(conn, llm, new) == 0
    assert llm.calls == []  # excluded from candidates, so no model call


def test_confirm_only_from_newer_end_and_views(conn):
    old = add(conn, "dreading the tough conversation with Sam", 0)
    new = add(conn, "had the tough conversation with Sam", 3)
    links.suggest_for_entry(conn, FakeLLM([{"links": [{"id": old, "type": "follow_up", "reason": "r"}]}]), new)
    link_id = rows(conn)[0]["id"]
    assert links.links_for_entry(conn, new)["suggested"][0]["label"] == "Follow-up to"
    assert links.links_for_entry(conn, old) == {"confirmed": [], "suggested": []}
    assert not links.set_status(conn, old, link_id, "confirmed")  # older end cannot confirm
    assert links.set_status(conn, new, link_id, "confirmed")
    assert links.links_for_entry(conn, new)["confirmed"][0]["other_id"] == old
    assert links.links_for_entry(conn, old)["confirmed"][0]["label"] == "Followed up by"
    assert links.remove(conn, old, link_id)
    assert links.links_for_entry(conn, new)["confirmed"] == []


def test_deleting_an_entry_removes_its_links(conn):
    old = add(conn, "dreading the tough conversation with Sam", 0)
    new = add(conn, "had the tough conversation with Sam", 3)
    links.suggest_for_entry(conn, FakeLLM([{"links": [{"id": old, "type": "related", "reason": "r"}]}]), new)
    entries.delete_entry(conn, old)
    assert rows(conn) == []
