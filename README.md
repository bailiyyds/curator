# curator

本地文档与代码策展 CLI，按阶段开发。当前为阶段一检查版；阶段二至八尚未实现，不能视为完整产品。

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

阶段一：9 项测试通过、1 项符号链接测试因 Windows 权限跳过；覆盖率 89%；mypy 严格检查通过，ruff 检查通过。

扫描支持 include/exclude、max-size、max-files、max-depth。跟随链接请求明确拒绝。

`scan -o` 与其他文件输出将在统一 journal 写入机制完成后接入，当前结果仅输出到终端。

完整安全对抗、跨平台链接测试、uv build 正式打包验收及阶段二至八仍待完成。
