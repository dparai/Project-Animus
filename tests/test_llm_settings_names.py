import pytest

from app import config
from app.config import LlmSettings
from app.llm import LlmClient

NAMES = ("API_KEY", "BASE_URL", "MODEL", "MODEL_INTERACTIVE", "MODEL_ESCALATE")


def _set(monkeypatch, prefix, **kw):
    for name, value in kw.items():
        monkeypatch.setenv(prefix + name, value)


def test_new_names_take_precedence_over_old(monkeypatch):
    _set(monkeypatch, "NVIDIA_", API_KEY="old-key", BASE_URL="https://old.test/v1", MODEL="old/m",
         MODEL_INTERACTIVE="old/i", MODEL_ESCALATE="old/e")
    _set(monkeypatch, "LLM_", API_KEY="new-key", BASE_URL="https://new.test/v1", MODEL="new/m",
         MODEL_INTERACTIVE="new/i", MODEL_ESCALATE="new/e")
    s = config.get_llm_settings()
    assert (s.api_key, s.base_url, s.model) == ("new-key", "https://new.test/v1", "new/m")
    assert s.interactive_models == ("new/i",) and s.escalation_model == "new/e"


def test_old_names_still_work_when_new_unset(monkeypatch):
    _set(monkeypatch, "NVIDIA_", API_KEY="old-key", BASE_URL="https://old.test/v1", MODEL="old/m",
         MODEL_INTERACTIVE="old/i", MODEL_ESCALATE="old/e")
    s = config.get_llm_settings()
    assert (s.api_key, s.base_url, s.model) == ("old-key", "https://old.test/v1", "old/m")
    assert s.interactive_models == ("old/i",) and s.escalation_model == "old/e"


def test_blank_new_name_falls_back_to_old(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "  ")
    monkeypatch.setenv("NVIDIA_API_KEY", "old-key")
    assert config.get_llm_settings().api_key == "old-key"


def test_nothing_set_uses_defaults(monkeypatch):
    s = config.get_llm_settings()
    assert s.api_key is None and s.base_url == config.DEFAULT_LLM_BASE_URL
    assert s.model == config.DEFAULT_LLM_MODEL


@pytest.mark.parametrize("prefix", ["LLM_", "NVIDIA_"])
def test_no_secret_in_repr_or_str(monkeypatch, prefix):
    monkeypatch.setenv(prefix + "API_KEY", "super-secret-key-123")
    s = config.get_llm_settings()
    client = LlmClient(s)
    for obj in (s, client):
        assert "super-secret-key-123" not in repr(obj)
        assert "super-secret-key-123" not in str(obj)
    assert "super-secret-key-123" not in repr(LlmSettings(api_key="super-secret-key-123", base_url="u", model="m"))
