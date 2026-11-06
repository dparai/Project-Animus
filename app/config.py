import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent

# Defaults target the NVIDIA API; any OpenAI-compatible endpoint works via LLM_BASE_URL.
DEFAULT_LLM_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Comma-separated priority list: the client tries each model in order (see app/llm.py).
DEFAULT_LLM_MODEL = "nvidia/nemotron-3-ultra-550b-a55b,nvidia/nemotron-3.5-lightning-30b-a3b"


# Interactive calls (Ask, Counselor, chat memory) favour accuracy: strong model first, fast one as fallback.
DEFAULT_LLM_MODEL_INTERACTIVE = "nvidia/nemotron-3-ultra-550b-a55b,nvidia/nemotron-3.5-lightning-30b-a3b"
# Model Ask escalates to when the first attempt finds nothing (first entry of LLM_MODEL_ESCALATE).
DEFAULT_LLM_MODEL_ESCALATE = "nvidia/nemotron-3-ultra-550b-a55b"


def parse_models(value: str) -> tuple[str, ...]:
    """Split a comma-separated model list: strip names, drop empties and duplicates, keep order."""
    seen: dict[str, None] = {}
    for name in (value or "").split(","):
        name = name.strip()
        if name:
            seen.setdefault(name)
    return tuple(seen)


@dataclass(frozen=True)
class LlmSettings:
    api_key: str | None = field(repr=False)
    base_url: str
    model: str  # raw configured value; may be a comma-separated priority list
    model_interactive: str = DEFAULT_LLM_MODEL_INTERACTIVE  # same format, for interactive calls
    model_escalate: str = DEFAULT_LLM_MODEL_ESCALATE  # first entry is the escalation model

    @property
    def models(self) -> tuple[str, ...]:
        return parse_models(self.model)

    @property
    def interactive_models(self) -> tuple[str, ...]:
        return parse_models(self.model_interactive) or parse_models(DEFAULT_LLM_MODEL_INTERACTIVE)

    @property
    def escalation_model(self) -> str:
        return (parse_models(self.model_escalate) or parse_models(DEFAULT_LLM_MODEL_ESCALATE))[0]


BASE_ALLOWED_HOSTS = ("localhost", "127.0.0.1")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def parse_allowed_hosts(value: str) -> tuple[str, ...]:
    """Valid host names from a comma-separated list. Anything else (wildcards, schemes, ports,
    slashes, spaces) is dropped; a warning counts the dropped entries but never shows them."""
    good: dict[str, None] = {}
    bad = 0
    for raw in (value or "").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if _HOST_RE.fullmatch(name):
            good.setdefault(name)
        else:
            bad += 1
    if bad:
        logging.getLogger("animus").warning("ANIMUS_ALLOWED_HOSTS: ignored %d invalid entries", bad)
    return tuple(good)


def get_allowed_hosts() -> tuple[str, ...]:
    """localhost and 127.0.0.1 always, plus the names in ANIMUS_ALLOWED_HOSTS."""
    extra = parse_allowed_hosts(os.environ.get("ANIMUS_ALLOWED_HOSTS", ""))
    return tuple(dict.fromkeys((*BASE_ALLOWED_HOSTS, *extra)))


def get_db_path() -> str:
    """ANIMUS_DB_PATH, else data/vault.db inside the app directory."""
    return os.environ.get("ANIMUS_DB_PATH") or str(APP_DIR / "data" / "vault.db")


def _env(name: str) -> str:
    """LLM_<name>, else the older NVIDIA_<name> (so an old .env keeps working). Never logged."""
    for prefix in ("LLM_", "NVIDIA_"):
        value = (os.environ.get(prefix + name) or "").strip()
        if value:
            return value
    return ""


def get_llm_settings() -> LlmSettings:
    key = _env("API_KEY") or None
    base = _env("BASE_URL") or DEFAULT_LLM_BASE_URL
    model = _env("MODEL") or DEFAULT_LLM_MODEL
    interactive = _env("MODEL_INTERACTIVE")
    if not parse_models(interactive):
        interactive = DEFAULT_LLM_MODEL_INTERACTIVE
    escalate = _env("MODEL_ESCALATE")
    if not parse_models(escalate):
        escalate = DEFAULT_LLM_MODEL_ESCALATE
    return LlmSettings(api_key=key, base_url=base, model=model, model_interactive=interactive,
                       model_escalate=escalate)


def get_auth_settings():
    """Password hash and session secret from the environment (systemd EnvironmentFile=.env)."""
    from app import auth  # local import: auth pulls in bcrypt and itsdangerous

    return auth.load_settings(os.environ)
