import pytest

from app.main import create_app
from tests.auth_helpers import login_client


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "hosts.db")


def test_foreign_host_header_is_rejected(db_path):
    client = login_client(create_app(db_path))
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_localhost_with_port_is_allowed(db_path):
    client = login_client(create_app(db_path))
    assert client.get("/", headers={"Host": "localhost:8100"}).status_code == 200
    assert client.get("/", headers={"Host": "127.0.0.1:8100"}).status_code == 200


def test_testserver_is_not_allowed_by_default(db_path):
    client = login_client(create_app(db_path))  # TestClient sends Host: testserver
    assert client.get("/").status_code == 400


def test_allowed_hosts_can_be_overridden(db_path):
    client = login_client(create_app(db_path, allowed_hosts=["testserver"]))
    assert client.get("/").status_code == 200
    assert client.get("/", headers={"Host": "localhost"}).status_code == 400


def _env_app(db_path, monkeypatch, value):
    monkeypatch.setenv("ANIMUS_ALLOWED_HOSTS", value)
    return create_app(db_path)


def test_env_allowed_host_passes_and_unknown_is_400(db_path, monkeypatch):
    client = login_client(_env_app(db_path, monkeypatch, "journal.example.test, Other.Example.Test"))
    assert client.get("/", headers={"Host": "journal.example.test"}).status_code == 200
    assert client.get("/", headers={"Host": "other.example.test:443"}).status_code == 200
    assert client.get("/", headers={"Host": "localhost"}).status_code == 200
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_env_rejects_wildcard_and_malformed_entries_without_logging_them(db_path, monkeypatch, caplog):
    bad = "*,https://a.example.test,b.example.test/x,c d.example.test,e.example.test:8100,*.f.example.test"
    with caplog.at_level("WARNING", logger="animus"):
        client = login_client(_env_app(db_path, monkeypatch, bad))
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"Host": "a.example.test"}).status_code == 400
    assert client.get("/", headers={"Host": "localhost"}).status_code == 200
    assert "ignored 6 invalid entries" in caplog.text
    assert "example.test" not in caplog.text


def test_env_unset_means_loopback_only(db_path, monkeypatch):
    monkeypatch.delenv("ANIMUS_ALLOWED_HOSTS", raising=False)
    from app import config
    assert config.get_allowed_hosts() == ("localhost", "127.0.0.1")


def test_env_allowed_host_origin_accepted_for_post(db_path, monkeypatch):
    client = login_client(_env_app(db_path, monkeypatch, "journal.example.test"))
    host = {"Host": "journal.example.test"}
    ok = client.post("/entries", data={"text": "fake"}, headers={**host, "Origin": "https://journal.example.test"})
    assert ok.status_code == 200
    bad = client.post("/entries", data={"text": "fake"}, headers={**host, "Origin": "https://evil.example"})
    assert bad.status_code == 403
