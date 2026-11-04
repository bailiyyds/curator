"""Configuration with explicit precedence."""
import tomllib
from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

class Config(BaseModel):
    """Validated resource budgets."""
    model_config = ConfigDict(extra="forbid")
    max_size: int = Field(default=50 * 1024 * 1024, ge=1)
    max_files: int = Field(default=100000, ge=1)
    max_depth: int = Field(default=100, ge=0)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

def load_config(root: Path, overrides: dict[str, Any] | None = None,
                user_path: Path | None = None) -> Config:
    """Merge defaults, user TOML, project TOML, then explicit CLI values."""
    values: dict[str, Any] = {}
    for path in (user_path or Path.home() / ".config/curator/config.toml",
                 root / ".curator.toml"):
        if path.is_file():
            with path.open("rb") as stream:
                values.update(tomllib.load(stream))
    values.update(overrides or {})
    return Config.model_validate(values)
