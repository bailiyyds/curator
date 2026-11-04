"""Keep tests independent of real provider credentials and accounts."""

import pytest


@pytest.fixture(autouse=True)
def isolate_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never accidentally use the workstation's live LLM configuration."""
    for name in (
        "CURATOR_LLM_BASE_URL",
        "CURATOR_LLM_MODEL",
        "CURATOR_LLM_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
