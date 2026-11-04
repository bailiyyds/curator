# curator

本地文档与代码策展 CLI，按阶段开发。当前完成阶段一至三；阶段四至八尚未实现，不能视为完整产品。

## 开发

需要 Python 3.12 和 uv。

```sh
uv sync
uv run curator scan examples --json
uv run curator extract examples
uv run curator extract examples --json
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

`scan -o` 已接入日志化导出；必须加 `--apply` 才写入，默认只预览。

阶段二：PDF/Markdown/纯文本与六种语言 AST 抽取已实现；30 项测试通过、1 项跳过，总覆盖率 95%。

`extract` 只读取文件并输出到终端；PDF 无文本层页面会标记需要 OCR。错误文件会报告原因并返回退出码 4。

阶段三新增重命名提案、执行、日志与撤销；56 项测试通过、1 项跳过，总覆盖率 91%。详见 [阶段二记录](STAGE-2.md) 和 [阶段三记录](STAGE-3.md)。

完整跨平台安全验证及阶段四至八仍待完成。


## 重命名与恢复

```sh
# 只读预览，含旧路径、新路径、理由和置信度
uv run curator rename examples/rename
# 只导出计划，不执行改名；-o 相对 examples/rename
uv run curator plan examples/rename -o plan.json --apply --yes
# 执行已经审阅的计划
uv run curator apply examples/rename/plan.json --apply --yes
# 撤销最近的未完整撤销事务
uv run curator undo examples/rename --last 1 --apply --yes
```

所有写入默认关闭。--apply 会要求输入 yes，--yes 仅免交互确认；高风险仍默认跳过。
输出文件不覆盖既有文件；撤销导出会将其归档到 .curator/trash/。
JSON 自动化调用请配合 --json --yes。日志记录 prepare 和 commit，并逐条 flush/fsync。
Linux/macOS 系统调用分支尚未实机验证；当前强制终止恢复证据来自 Windows。
