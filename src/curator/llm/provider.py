"""Small OpenAI-compatible HTTP client with bounded retries and immutable cache."""

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from curator.core.journal import Journal, move_no_replace, root_lock
from curator.models.changes import JournalEntry
from curator.safety.paths import safe_path


class ProviderConfig(BaseModel):
    """Protocol settings; credentials never appear in representations or dumps."""

    model_config = ConfigDict(extra="forbid")
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    model: str = ""
    timeout: float = Field(default=60, gt=0, le=600)
    retries: int = Field(default=3, ge=0, le=3)
    max_tokens: int = Field(default=512, ge=1, le=8192)
    input_tokens: int = Field(default=2000, ge=256, le=32000)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_tasks: int = Field(default=64, ge=1, le=10000)
    concurrency: int = Field(default=4, ge=1, le=32)
    token_parameter: str = "max_tokens"
    json_mode: bool = True
    use_cache: bool = True
    write_cache: bool = False


class Completion(BaseModel):
    """Expected endpoint/cache failures do not interrupt other tasks."""

    content: str | None = None
    error: str | None = None
    attempts: int = 0
    cache_hit: bool = False
    warnings: list[str] = Field(default_factory=list)


class CacheRecord(BaseModel):
    """Only responses are persisted; no source prompts or API credentials."""

    model_config = ConfigDict(extra="forbid", strict=True)
    endpoint: str
    model: str
    key: str
    content: str


class Provider:
    """Call /chat/completions without binding to a vendor SDK."""

    def __init__(
        self, config: ProviderConfig, root: Path, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Initialize without network access or filesystem writes."""
        self.config, self.root, self.transport = config, root.resolve(), transport
        parsed = urlsplit(config.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Expected an HTTP(S) base URL without credentials, query or fragment")
        try:
            self.local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            self.local = parsed.hostname == "localhost"
        if parsed.scheme == "http" and not self.local:
            raise ValueError("Plain HTTP is permitted only for loopback endpoints")
        if config.token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Unsupported output-token parameter")
        self.endpoint = config.base_url.rstrip("/") + "/chat/completions"

    def unavailable(self) -> str | None:
        """Explain missing configuration without disclosing credentials."""
        if not self.config.model.strip():
            return "LLM model is not configured; retaining deterministic results"
        if not self.local and (
            self.config.api_key is None or not self.config.api_key.get_secret_value()
        ):
            return "LLM API key is missing; retaining deterministic results"
        return None

    def prompt_key(self, system: str, prompt: str) -> str:
        """Hash canonical prompt + model + temperature as the cache key."""
        canonical = json.dumps([system, prompt], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(
            (canonical + self.config.model + str(float(self.config.temperature))).encode()
        ).hexdigest()

    def cache_path(self, key: str) -> Path:
        """Separate endpoints and generation settings to prevent cache collisions."""
        namespace = hashlib.sha256(
            json.dumps(
                [
                    self.endpoint,
                    self.config.max_tokens,
                    self.config.token_parameter,
                    self.config.json_mode,
                ]
            ).encode()
        ).hexdigest()[:24]
        return safe_path(self.root, f".curator/cache/{namespace}/{key}.json", internal=True)

    def _read_cache(self, key: str, validate: Callable[[str], None] | None) -> Completion | None:
        if not self.config.use_cache:
            return None
        path = self.cache_path(key)
        if not path.is_file():
            return None
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Oversized cache record ignored")
        record = CacheRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if (
            record.endpoint != self.endpoint
            or record.model != self.config.model
            or record.key != key
        ):
            raise ValueError("Cache metadata mismatch")
        if validate:
            validate(record.content)
        return Completion(content=record.content, cache_hit=True)

    def _write_cache(self, key: str, content: str) -> None:
        if not self.config.use_cache or not self.config.write_cache:
            return
        path = self.cache_path(key)
        payload = (
            CacheRecord(endpoint=self.endpoint, model=self.config.model, key=key, content=content)
            .model_dump_json()
            .encode()
        )
        with root_lock(self.root), Journal(self.root, kind="cache") as journal:
            path = self.cache_path(key)
            if path.exists():
                return
            digest = hashlib.sha256(payload).hexdigest()
            event = JournalEntry(
                ts=datetime.now(UTC),
                change_id="cache_" + uuid4().hex,
                op="cache_write",
                src=path.relative_to(self.root).as_posix(),
                dst=None,
                result="ok",
                phase="prepare",
                post_sha256=digest,
            )
            journal.append(event)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                staging = safe_path(self.root, ".curator/staging", internal=True)
                staging.mkdir(exist_ok=True)
                temporary = safe_path(self.root, ".curator/staging/" + uuid4().hex, internal=True)
                with temporary.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                move_no_replace(temporary, self.cache_path(key))
                event = event.model_copy(update={"phase": "commit", "ts": datetime.now(UTC)})
            except (OSError, ValueError) as error:
                event = event.model_copy(
                    update={"phase": "commit", "result": "failed", "error": type(error).__name__}
                )
                journal.append(event)
                raise
            journal.append(event)

    async def complete(
        self, system: str, prompt: str, validate: Callable[[str], None] | None = None
    ) -> Completion:
        """Enforce a total deadline, bounded output and isolated retry failures."""
        unavailable = self.unavailable()
        if unavailable:
            return Completion(error=unavailable)
        if len(system.encode()) + len(prompt.encode()) + 128 > self.config.input_tokens:
            return Completion(error="Input exceeds conservative UTF-8 byte/token budget")
        result = Completion()
        key = self.prompt_key(system, prompt)
        try:
            hit = self._read_cache(key, validate)
            if hit:
                return hit
        except (OSError, ValueError) as error:
            result.warnings.append(f"Cache read ignored: {type(error).__name__}")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key and self.config.api_key.get_secret_value():
            headers["Authorization"] = "Bearer " + self.config.api_key.get_secret_value()
        payload: dict[str, object] = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            self.config.token_parameter: self.config.max_tokens,
            "stream": False,
        }
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with asyncio.timeout(self.config.timeout):
                async with httpx.AsyncClient(
                    timeout=self.config.timeout,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    for attempt in range(self.config.retries + 1):
                        result.attempts += 1
                        retry = False
                        try:
                            async with client.stream(
                                "POST", self.endpoint, headers=headers, json=payload
                            ) as response:
                                if (
                                    response.status_code in {408, 409, 429}
                                    or response.status_code >= 500
                                ):
                                    retry = True
                                    result.error = f"LLM HTTP {response.status_code}"
                                elif response.status_code != 200:
                                    result.error = f"LLM HTTP {response.status_code}; no retry"
                                    break
                                else:
                                    data = bytearray()
                                    async for chunk in response.aiter_bytes():
                                        data.extend(chunk)
                                        if len(data) > 1024 * 1024:
                                            raise ValueError("Response exceeds byte budget")
                                    value = json.loads(data)
                                    content = value["choices"][0]["message"]["content"]
                                    if not isinstance(content, str):
                                        raise ValueError("Expected textual completion content")
                                    if validate:
                                        validate(content)
                                    result.content, result.error = content, None
                                    break
                        except httpx.TransportError as error:
                            retry, result.error = (
                                True,
                                f"LLM transport failure: {type(error).__name__}",
                            )
                        except (ValueError, KeyError, IndexError, TypeError):
                            result.error = "LLM returned invalid JSON or schema"
                            break
                        if not retry or attempt == self.config.retries:
                            break
                        await asyncio.sleep(min(0.25 * 2**attempt, 4))
        except TimeoutError:
            result.error = "LLM total deadline exceeded"
        except Exception as error:
            logging.getLogger(__name__).debug("Provider failure", exc_info=True)
            result.error = f"LLM unavailable: {type(error).__name__}"
        if result.content is not None:
            try:
                self._write_cache(key, result.content)
            except (OSError, ValueError) as error:
                result.warnings.append(f"Cache write skipped: {type(error).__name__}")
        return result
