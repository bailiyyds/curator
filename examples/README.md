# 离线演示

从项目根目录运行以下三个命令；均默认只读：

```sh
uv run curator scan examples --json
uv run curator review examples/review --no-llm --severity info+
uv run curator doc examples/docgen --from ast --json
```

还可运行 `uv run curator search "curator" examples --json` 与 `uv run curator duplicates examples --json`。

PDF、Markdown、文本与代码可交给 extract；rename、organize 和 docgen 子目录分别提供对应演示输入。review 子目录是故意有缺陷的测试数据，请勿执行其代码。首次尝试实际变更时，建议复制演示目录再加 --apply；undo 的归档和日志保留在该副本的 .curator 下。
