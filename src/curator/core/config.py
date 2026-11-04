"""Configuration with explicit precedence."""

import os
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from curator.llm.provider import ProviderConfig


class Config(BaseModel):
    """Validated resource budgets."""

    model_config = ConfigDict(extra="forbid")
    llm: ProviderConfig = Field(default_factory=ProviderConfig)
    follow_symlinks: bool = False
    doc_git_max_commits: int = Field(default=200, ge=1, le=10000)
    doc_git_timeout: float = Field(default=10, gt=0, le=60)
    doc_git_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    max_size: int = Field(default=50 * 1024 * 1024, ge=1)
    max_files: int = Field(default=100000, ge=1)
    max_topic_documents: int = Field(default=1024, ge=1)
    max_depth: int = Field(default=100, ge=0)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


def load_config(
    root: Path, overrides: dict[str, Any] | None = None, user_path: Path | None = None
) -> Config:
    """Merge defaults, user TOML, project TOML, then explicit CLI values."""
    env = {
        key: value
        for key, value in {
            "base_url": os.getenv("CURATOR_LLM_BASE_URL"),
            "model": os.getenv("CURATOR_LLM_MODEL"),
            "api_key": os.getenv("CURATOR_LLM_API_KEY") or os.getenv("OPENAI_API_KEY"),
        }.items()
        if value is not None
    }
    values: dict[str, Any] = {"llm": env}

    def merge(layer: dict[str, Any]) -> None:
        for key, value in layer.items():
            if key == "llm":
                if not isinstance(value, dict):
                    raise ValueError("llm configuration must be a TOML table")
                values[key] = {**values.get(key, {}), **value}
            else:
                values[key] = value

    for path in (user_path or Path.home() / ".config/curator/config.toml", root / ".curator.toml"):
        if path.is_file():
            with path.open("rb") as stream:
                merge(tomllib.load(stream))
    merge(overrides or {})
    if (
        not os.getenv("CURATOR_LLM_API_KEY")
        and os.getenv("OPENAI_API_KEY")
        and values.get("llm", {}).get("api_key") == os.getenv("OPENAI_API_KEY")
        and urlsplit(values.get("llm", {}).get("base_url", "https://api.openai.com/v1")).hostname
        != "api.openai.com"
    ):
        values["llm"].pop("api_key", None)
    try:
        return Config.model_validate(values)
    except ValidationError as error:
        details = "; ".join(
            ".".join(map(str, item["loc"])) + ": " + item["type"]
            for item in error.errors(include_input=False, include_context=False)
        )
        raise ValueError("Invalid configuration fields: " + details) from None
