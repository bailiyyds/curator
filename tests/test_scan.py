from pathlib import Path
from curator.core.scan import scan
from curator.core.config import Config, load_config

def test_stable_read_only(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Hello", encoding="utf-8")
    before = list(tmp_path.iterdir())
    first = scan(tmp_path)
    assert first.model_dump_json() == scan(tmp_path).model_dump_json()
    assert first.files[0].kind == "markdown"
    assert list(tmp_path.iterdir()) == before

def test_budgets(tmp_path: Path) -> None:
    (tmp_path / "big").write_bytes(b"123")
    assert scan(tmp_path, Config(max_size=1)).skipped
    (tmp_path / ".git").mkdir()
    assert len(scan(tmp_path, Config(max_size=1)).skipped) == 2

def test_config(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    user.write_text("max_files = 2")
    (tmp_path / ".curator.toml").write_text("max_files = 3")
    assert load_config(tmp_path, {"max_files": 4}, user).max_files == 4
