# curator

本地文档与代码策展 CLI，按阶段开发。当前仅为阶段一初版，尚未验收，不能视为完整产品。

## 开发

需要 Python 3.12 和 uv。

```sh
uv sync
uv run curator scan examples --json
uv run pytest --cov=curator
uv run mypy --strict src
uv run ruff check .
uv build
```

扫描默认只读，不跟随符号链接或 Windows junction；跳过项包含原因。
配置合并顺序：CLI > 项目 `.curator.toml` > 用户 `~/.config/curator/config.toml` > 默认。
为满足快照稳定性，created_at 使用已观察文件的最大 mtime。

尚待完成：CLI 预算参数、完整安全对抗测试及性能验收；阶段二至八尚未实现。
