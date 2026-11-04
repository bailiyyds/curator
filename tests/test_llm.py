"""Mock protocol, deadlines, cache, bounded review and deterministic fallback."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from typer.testing import CliRunner

from curator.analyze.code import review_directory
from curator.cli import app
from curator.core.config import Config, load_config
from curator.core.plan import rename_plan
from curator.core.undo import latest_journals, undo_journal
from curator.llm.namer import suggest_titles
from curator.llm.provider import Completion, Provider, ProviderConfig
from curator.llm.reviewer import (
    Decision,
    build_prompt,
    review_findings,
    safe_patch,
    validate_decision,
)

DECISION = {
    "confirmed": True,
    "reason": "Visible mutable default",
    "suggestion": "Use None and initialize locally.",
    "patch": None,
}


def response(content: str | None = None) -> httpx.Response:
    """Return an OpenAI-compatible Chat Completions envelope."""
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content or json.dumps(DECISION)}}]}
    )


def provider(root: Path, handler: object, **kwargs: object) -> Provider:
    """Build a loopback-only provider backed by an in-process mock transport."""
    return Provider(
        ProviderConfig.model_validate(
            {"base_url": "http://127.0.0.1:11434/v1", "model": "test-model", **kwargs}
        ),
        root,
        httpx.MockTransport(handler),
    )


def test_http_protocol_and_no_default_writes(tmp_path: Path) -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert str(request.url) == "http://127.0.0.1:11434/v1/chat/completions"
        assert "authorization" not in request.headers
        return response()

    client = provider(tmp_path, handle)
    result = asyncio.run(client.complete("JSON only", "partial excerpt", validate_decision))
    assert result.content and not result.error and result.attempts == 1
    assert seen[0]["max_tokens"] == 512 and seen[0]["stream"] is False
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert not (tmp_path / ".curator").exists()


def test_retry_then_success_and_total_deadline(tmp_path: Path) -> None:
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls < 3 else response()

    result = asyncio.run(provider(tmp_path, flaky).complete("JSON", "excerpt", validate_decision))
    assert result.content and result.attempts == 3

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return response()

    result = asyncio.run(provider(tmp_path, slow, timeout=0.02).complete("JSON", "excerpt"))
    assert "deadline" in (result.error or "")
    result = asyncio.run(
        provider(tmp_path, lambda r: httpx.Response(429), retries=1).complete("JSON", "excerpt")
    )
    assert result.attempts == 2 and result.error


def test_transport_retry_and_nonretryable_response(tmp_path: Path) -> None:
    calls = 0

    def disconnected(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("No secret leakage", request=request)

    result = asyncio.run(provider(tmp_path, disconnected, retries=1).complete("JSON", "excerpt"))
    assert calls == 2 and "ConnectError" in (result.error or "")
    for status in (400, 401, 302):
        result = asyncio.run(
            provider(tmp_path, lambda r, status=status: httpx.Response(status), retries=3).complete(
                "JSON", "excerpt"
            )
        )
        assert result.attempts == 1 and result.error


def test_bad_json_schema_size_and_budget(tmp_path: Path) -> None:
    cases = [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={}),
        response("not-json"),
        response(json.dumps({**DECISION, "confirmed": "true"})),
        httpx.Response(200, content=b"x" * (1024 * 1024 + 1)),
    ]
    for item in cases:
        result = asyncio.run(
            provider(tmp_path, lambda r, item=item: item).complete(
                "JSON", "excerpt", validate_decision
            )
        )
        assert result.error and result.content is None
    client = provider(
        tmp_path, lambda r: pytest.fail("Over-budget prompt reached network"), input_tokens=256
    )
    assert asyncio.run(client.complete("system", "x" * 1000)).attempts == 0


def test_missing_key_model_and_url_safety(tmp_path: Path) -> None:
    client = Provider(ProviderConfig(model="configured"), tmp_path)
    assert "key" in (asyncio.run(client.complete("JSON", "excerpt")).error or "")
    assert Provider(ProviderConfig(), tmp_path).unavailable()
    for url in (
        "http://example.com/v1",
        "https://key:secret@example.com/v1",
        "https://example.com/v1?key=x",
        "file:///tmp",
    ):
        with pytest.raises(ValueError):
            Provider(ProviderConfig(base_url=url), tmp_path)
    with pytest.raises(ValueError):
        Provider(ProviderConfig(token_parameter="bad"), tmp_path)


def test_authorization_secret_not_dumped_and_token_parameter(tmp_path: Path) -> None:
    config = ProviderConfig(
        base_url="https://example.test/v1",
        model="m",
        api_key=SecretStr("private-test-key"),
        token_parameter="max_completion_tokens",
    )
    assert "private-test-key" not in repr(config) and "api_key" not in config.model_dump()

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer private-test-key"
        assert json.loads(request.content)["max_completion_tokens"] == 512
        return httpx.Response(401, content=b"private-test-key must not be echoed")

    result = asyncio.run(
        Provider(config, tmp_path, httpx.MockTransport(handle)).complete("JSON", "excerpt")
    )
    assert "private-test-key" not in result.model_dump_json()


def test_cache_readonly_writes_and_history_exclusion(tmp_path: Path) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response()

    writer = provider(tmp_path, handle, write_cache=True)
    first = asyncio.run(writer.complete("JSON", "excerpt", validate_decision))
    assert first.content and calls == 1
    key = writer.prompt_key("JSON", "excerpt")
    canonical = json.dumps(["JSON", "excerpt"], ensure_ascii=False, separators=(",", ":"))
    assert key == hashlib.sha256((canonical + "test-model" + "0.0").encode()).hexdigest()
    cache = writer.cache_path(key)
    assert cache.exists() and "partial excerpt" not in cache.read_text()
    reader = provider(tmp_path, handle)
    hit = asyncio.run(reader.complete("JSON", "excerpt", validate_decision))
    assert hit.cache_hit and hit.attempts == 0 and calls == 1
    assert latest_journals(tmp_path) == []
    journal = next((tmp_path / ".curator/journal").glob("*.jsonl"))
    with pytest.raises(ValueError):
        undo_journal(journal, apply=True)
    ignored = asyncio.run(
        provider(tmp_path, handle, use_cache=False).complete("JSON", "excerpt", validate_decision)
    )
    assert not ignored.cache_hit and calls == 2
    assert writer.cache_path(key) != provider(tmp_path, handle, max_tokens=128).cache_path(key)


def test_invalid_cache_falls_back_without_overwriting(tmp_path: Path) -> None:
    client = provider(tmp_path, lambda r: response(), write_cache=True)
    key = client.prompt_key("JSON", "excerpt")
    path = client.cache_path(key)
    path.parent.mkdir(parents=True)
    path.write_text("invalid cache")
    result = asyncio.run(client.complete("JSON", "excerpt", validate_decision))
    assert result.content and result.warnings
    assert path.read_text() == "invalid cache"


def sources(root: Path, count: int = 3) -> None:
    """Write safely parsed sources with enough context to avoid sending whole files."""
    for i in range(count):
        (root / f"source-{i}.py").write_text(
            f"# context {i}\ndef f(values=[]):\n    return values\n# excluded tail\n"
        )


def test_reviewer_concurrency_and_isolated_failures(tmp_path: Path) -> None:
    sources(tmp_path, 4)
    active = peak = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        value = json.loads(json.loads(request.content)["messages"][1]["content"])
        active -= 1
        if value["path"] == "source-1.py":
            return httpx.Response(500)
        if value["path"] == "source-2.py":
            return response(
                json.dumps({**DECISION, "confirmed": False, "reason": "Insufficient evidence"})
            )
        return response()

    report = review_directory(tmp_path)
    updated = asyncio.run(
        review_findings(report, provider(tmp_path, handle, concurrency=2, retries=0), Config())
    )
    assert 1 < peak <= 2
    assert updated.llm_stats["confirmed"] == 2 and updated.llm_stats["failed"] == 1
    assert updated.llm_stats["unconfirmed"] == 1
    assert len(updated.findings) == len(report.findings) and all(
        f.source == "rule" for f in report.findings
    )
    assert not (tmp_path / ".curator").exists()


def test_prompt_slices_limits_and_changed_sources(tmp_path: Path) -> None:
    sources(tmp_path, 1)
    report = review_directory(tmp_path)
    finding = report.findings[0]
    text = (tmp_path / finding.path).read_text()
    prompt = build_prompt(finding, text, "python", "system", 2000)
    assert prompt and text.strip() not in prompt and "excluded tail" in text
    payload = json.loads(prompt)
    assert "excluded tail" not in payload["snippet"]
    assert "2: def f(values=[]):" in payload["snippet"]
    assert payload["partial_excerpt"] and len(b"system") + len(prompt.encode()) + 128 <= 2000
    assert build_prompt(finding, "changed", "python", "system", 2000) is None
    (tmp_path / finding.path).write_text("changed")
    result = asyncio.run(
        review_findings(
            report, provider(tmp_path, lambda r: pytest.fail("Changed source was sent")), Config()
        )
    )
    assert result.llm_stats["skipped"] == 1 and result.findings[0].source == "rule"


def test_single_line_and_task_budget(tmp_path: Path) -> None:
    sources(tmp_path, 3)
    (tmp_path / "one.py").write_text("import os\n")
    report = review_directory(tmp_path)
    result = asyncio.run(
        review_findings(report, provider(tmp_path, lambda r: response(), max_tasks=2), Config())
    )
    assert result.llm_stats["tasks"] == 2 and result.llm_stats["skipped"] >= 2
    assert len(result.findings) == 4


def test_patch_validation_and_never_apply(tmp_path: Path) -> None:
    good = "--- a/source-0.py\n+++ b/source-0.py\n@@ -2 +2 @@\n-def f(values=[]):\n+def f(values=None):\n"
    assert safe_patch(good, "source-0.py")
    assert not safe_patch(good.replace("b/source-0.py", "b/../escape.py"), "source-0.py")
    assert not safe_patch(good.replace("@@ -2 +2 @@", "@@ -2,3 +2 @@"), "source-0.py")
    sources(tmp_path, 1)
    before = (tmp_path / "source-0.py").read_bytes()
    result = asyncio.run(
        review_findings(
            review_directory(tmp_path),
            provider(tmp_path, lambda r: response(json.dumps({**DECISION, "patch": good}))),
            Config(),
        )
    )
    assert result.findings[0].patch == good and (tmp_path / "source-0.py").read_bytes() == before
    result = asyncio.run(
        review_findings(
            review_directory(tmp_path),
            provider(
                tmp_path, lambda r: response(json.dumps({**DECISION, "patch": "delete everything"}))
            ),
            Config(),
        )
    )
    assert result.findings[0].patch is None and result.warnings


def test_strict_decision_schema() -> None:
    for value in ({**DECISION, "confirmed": "yes"}, {**DECISION, "extra": 1}, {"confirmed": True}):
        with pytest.raises(ValidationError):
            Decision.model_validate(value)


def test_nested_config_precedence_and_openai_key_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    user = tmp_path / "user.toml"
    user.write_text('[llm]\nmodel="user-model"\nconcurrency=2\n')
    (tmp_path / ".curator.toml").write_text(
        '[llm]\nmodel="project-model"\nbase_url="https://custom.example/v1"\n'
    )
    config = load_config(tmp_path, {"llm": {"model": "cli-model"}}, user)
    assert config.llm.model == "cli-model" and config.llm.concurrency == 2
    assert config.llm.api_key is None
    monkeypatch.setenv("CURATOR_LLM_API_KEY", "custom-test-key")
    assert load_config(tmp_path, user_path=user).llm.api_key.get_secret_value() == "custom-test-key"


def test_title_suggestions_priority_and_safe_slug(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text(
        "Apple orchard harvest notes and plans for next season.\nMore content excluded."
    )
    (tmp_path / "titled.md").write_text("# Existing Title\nKeep this title.")
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return response(json.dumps({"title": "../../Orchard Notes", "confidence": 0.8}))

    titles, warnings = asyncio.run(suggest_titles(tmp_path, provider(tmp_path, handle), Config()))
    assert len(seen) == 1 and not warnings
    plan = rename_plan(tmp_path, source="llm", llm_titles=titles)
    change = next(c for c in plan.changes if c.src == "notes.txt")
    assert change.dst == "Orchard-Notes.txt" and change.risk == "medium"
    assert next(c for c in plan.changes if c.src == "titled.md").reason == "first level-one heading"


def test_cli_missing_key_no_cache_and_write_confirmation(tmp_path: Path) -> None:
    sources(tmp_path, 1)
    runner = CliRunner()
    missing = runner.invoke(
        app, ["review", str(tmp_path), "--llm", "--model", "test-model", "--json"]
    )
    assert missing.exit_code == 0 and json.loads(missing.stdout)["warnings"]
    assert not (tmp_path / ".curator").exists()
    assert runner.invoke(app, ["review", str(tmp_path), "--llm", "--write-cache"]).exit_code == 2
    assert (
        runner.invoke(
            app, ["review", str(tmp_path), "--llm", "--write-cache", "--no-cache", "--apply"]
        ).exit_code
        == 2
    )
    declined = runner.invoke(
        app, ["review", str(tmp_path), "--llm", "--write-cache", "--apply"], input="no\n"
    )
    assert declined.exit_code == 0 and not (tmp_path / ".curator").exists()


def test_long_line_prompt_and_unusable_budget(tmp_path: Path) -> None:
    text = "padding = '" + "x" * 8000 + "'; result = value == None\n# tail withheld\n"
    (tmp_path / "long.py").write_text(text)
    finding = review_directory(tmp_path).findings[0]
    prompt = build_prompt(finding, text, "python", "system", 1000)
    assert prompt and "trimmed" in prompt
    assert len(prompt.encode()) + len("system") + 128 <= 1000
    assert build_prompt(finding, text, "python", "s" * 2000, 256) is None


def test_cache_schema_failure_does_not_persist(tmp_path: Path) -> None:
    client = provider(tmp_path, lambda r: response("wrong JSON"), write_cache=True)
    result = asyncio.run(client.complete("JSON", "excerpt", validate_decision))
    assert result.error and not (tmp_path / ".curator").exists()


def test_unexpected_task_failure_is_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources(tmp_path, 2)
    client = provider(tmp_path, lambda r: response())
    original = client.complete

    async def one_bad(system: str, prompt: str, validate: object = None) -> Completion:
        if "source-0.py" in prompt:
            raise RuntimeError("isolated failure")
        return await original(system, prompt, validate_decision)

    monkeypatch.setattr(client, "complete", one_bad)
    result = asyncio.run(review_findings(review_directory(tmp_path), client, Config()))
    assert result.llm_stats["skipped"] == 1 and result.llm_stats["confirmed"] == 1


def test_invalid_config_redacts_secret_values(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    user.write_text('[llm]\napi_key="private-test-key"\nmodel=123\n')
    with pytest.raises(ValueError) as error:
        load_config(tmp_path, user_path=user)
    assert "private-test-key" not in str(error.value)
    user.write_text('llm="bad"\n')
    with pytest.raises(ValueError, match="table"):
        load_config(tmp_path, user_path=user)


def test_llm_audit_rendering_and_backtick_fences(tmp_path: Path) -> None:
    from curator.cli.review import render_markdown, render_text

    sources(tmp_path, 1)
    report = review_directory(tmp_path)
    finding = report.findings[0]
    finding.source = "llm"
    finding.llm_confirmed = True
    finding.llm_reason = "Untrusted ``` text"
    finding.suggestion = "Use ``` as data, not markup"
    finding.patch = "--- a/source-0.py\n+++ b/source-0.py\n@@ -2 +2 @@\n-def f(values=[]):\n+def f(values=None):\n"
    text = render_text(report)
    markdown = render_markdown(report)
    assert "source=llm" in text and "not applied" in text
    assert "````text" in markdown and "LLM confirmed: True" in markdown
    assert "Suggested patch (not applied)" in markdown


def test_namer_unexpected_failure_is_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("bad.txt", "good.txt"):
        (tmp_path / name).write_text(
            "Meaningful orchard harvest observations and plans for next year."
        )
    client = provider(
        tmp_path, lambda r: response(json.dumps({"title": "Orchard Notes", "confidence": 0.8}))
    )
    original = client.complete

    async def unstable(system: str, prompt: str, validate: object = None) -> Completion:
        if "bad.txt" in prompt:
            raise RuntimeError("single task failed")
        return await original(system, prompt)

    monkeypatch.setattr(client, "complete", unstable)
    titles, warnings = asyncio.run(suggest_titles(tmp_path, client, Config()))
    assert set(titles) == {"good.txt"} and any("Isolated title failure" in w for w in warnings)
