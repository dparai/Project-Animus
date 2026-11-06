class FakeLLM:
    """Test double for LlmClient: records calls, returns queued dicts or raises queued exceptions."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def queue(self, item):
        self.responses.append(item)

    def complete_json(self, system: str, user: str, *, max_tokens: int = 1500,
                      read_timeout: float | None = None, models=None, thinking: bool = True) -> dict:
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens,
                           "read_timeout": read_timeout, "models": models, "thinking": thinking})
        if not self.responses:
            raise AssertionError("FakeLLM has no queued response")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
