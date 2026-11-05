# curator

本地文档与代码策展 CLI，使用 Python 3.12、Typer、Pydantic 和 tree-sitter。支持文档抽取、重命名与整理、代码审查、文档生成、检索及重复文件建议。

所有文件变更默认只预览。显式 `--apply` 后要求输入 `yes`；`--yes` 用于已经审阅的自动化调用。工具不执行被扫描目录中的代码，不提供删除操作。

## 安装与三个演示命令

需要 Python 3.12+ 和 uv。在仓库目录执行：

```sh
uv sync --frozen
uv run curator scan examples --json
uv run curator review examples/review --no-llm --severity info+
uv run curator doc examples/docgen --from ast --json
```

`examples/review` 故意包含缺陷，作为扫描输入，勿直接运行其中代码。PDF 没有文本层时只报告 `needs_ocr`，不进行 OCR。六种代码语言为 Python、TypeScript、JavaScript、Go、Rust、Java。

## 预览、执行与恢复

```sh
uv run curator rename examples/rename --template "{date}-{topic}-{slug}"
uv run curator plan examples/rename -o plan.json --apply --yes
uv run curator apply examples/rename/plan.json --apply --yes
uv run curator undo examples/rename --last 1 --apply --yes

uv run curator organize examples/organize --by type
uv run curator organize examples/organize --by date
uv run curator organize examples/organize --by topic --json
```

`plan -o --apply` 只导出计划，不执行改名；`apply` 单独执行计划。路径和输出参数按相应扫描根目录解析。执行前检查 SHA-256；源文件已改变、目标已存在或风险过高时跳过并给出原因。高风险变更需要额外 `--allow-high-risk`。

整理输出默认位于 `organized/`，可用 `--into` 指定根目录内的目录。同名文件进入 `_conflicts/`；主题聚类离线运行，最多 12 个标签，超预算文件归入“其他”。原有空目录仅生成独立清理建议。

每个事务持有系统文件锁，journal 的 prepare/commit 逐条 flush/fsync；崩溃恢复可识别文件已移动但 commit 未写出的窗口。undo 校验内容，拒绝覆盖或撤销后来修改的文件。撤销新建文件时归档到 `.curator/trash/`，保留恢复路径。

## 文档抽取与代码审查

```sh
uv run curator extract examples --json
uv run curator review examples/review --lang py --format json
uv run curator review examples/review --format md
uv run curator doc examples/docgen --from both --split module --out docs
```

审查结果给出路径、行列、证据和建议，同一行合并并保留最高严重度。默认 `warning+`，`info+` 包含注释 TODO/FIXME/XXX。静态规则不能保证任意代码仓库零误报。

文档包含声明、签名、参数表、装饰器、文档字符串、直接文本调用目标和 Git 活跃度；Git 不可用时明确降级到 AST。读取历史禁用 hooks、外部 diff/textconv、分页和按需拉取对象。人工文档不覆盖，有差异时生成 `.generated.md` 变体并展示 diff；一批文档一次 undo 即可恢复。加 `--apply` 才实际生成文件。

## 本地检索与去重

```sh
uv run curator search "文件" examples --top 10
uv run curator search "curator" examples --build-index --apply --yes --json
uv run curator duplicates examples --json
```

关键词倒排索引基于 `Document.body`。默认在内存构建，只读搜索；`--build-index --apply` 才保存不可覆盖快照到 `.curator/index/`。后续搜索按文件内容哈希和配置识别可复用索引，源内容变化则重新抽取；损坏索引有提示并回退内存构建，不静默覆盖。

结果包含路径、命中片段和行号。`line_basis: extracted-text` 表明行号属于抽取后的文本；Markdown/PDF 的位置不等同原始文件行号。JSON 结果不包含整份索引或全部文档内容。`-o report.json --apply` 可单独日志化导出结果。

相似度命令先按 SHA-256 分组精确重复，再给出 64 位 simhash 文本近似建议。它不删除、移动或合并文件；相似不代表可以安全删除。近似分析最多 512 个唯一文件、每份前 20000 字符，精确分组覆盖扫描预算内的全部可读文件。

可选语义检索需要单独安装依赖并准备可信的本地模型：

```sh
uv sync --frozen --extra semantic
uv run curator search "document organization" examples --semantic --model-path /trusted/models/model
uv run curator search "document organization" examples --semantic --model-path /trusted/models/model --build-index --apply --yes
```

Windows 的 `--model-path` 使用实际绝对路径，例如 `C:/Models/local-model`。模型目录必须位于扫描根目录之外；不自动下载，不信任远程代码，仅支持 safetensors 权重及内置 Transformer/Pooling/Normalize 模块，拒绝链接、Python 模块和 pickle 类权重。语义分析最多前 512 个非空文本行，每行前 1000 字符；结果明确报告截断。显式保存时本地向量索引按源内容与模型哈希分别存储，后续只需重新编码查询。缺依赖、模型或推理失败时给出提示并回退关键词搜索。当前只验证了模拟模型接口与持久化，没有实测真实模型质量。

## 配置与可选 LLM

```sh
uv run curator config init
uv run curator config init . --apply --yes
uv run curator --verbose scan examples
```

配置优先级为 CLI > 项目 `.curator.toml` > 用户 `~/.config/curator/config.toml` > 默认；配置初始化不会覆盖已有文件。默认单文件 50 MiB、100000 文件、深度 100。扫描支持 `--include`、`--exclude`、`--max-size`、`--max-files` 和 `--max-depth`。安全策略拒绝跟随符号链接/junction，不支持开启跟随。

默认不调用 LLM。服务配置示例见 [examples/llm-config.toml](examples/llm-config.toml)；环境默认值为 `CURATOR_LLM_BASE_URL`、`CURATOR_LLM_MODEL`、`CURATOR_LLM_API_KEY`。`OPENAI_API_KEY` 仅自动用于 api.openai.com；本地回环兼容服务允许无 key。不要将凭据提交到仓库。

```sh
uv run curator review examples/review --llm --json
uv run curator review examples/review --llm --no-cache --json
uv run curator review examples/review --llm --write-cache --apply --yes --json
uv run curator rename examples --source llm --lang zh,en --json
```

Provider 走 OpenAI 兼容 HTTP 协议，默认总期限 60 秒、最多 3 次重试、并发 4、64 个任务、2000 输入预算。输入使用保守 UTF-8 字节上界控制，输出限制 512 tokens。只发送有限片段；严格校验 JSON，复核失败保留静态结果，补丁从不自动应用。`rename --lang` 是 LLM 命名的语言偏好，本地标题保持原文。

默认仅允许读取已有缓存；显式写缓存才生成 `.curator/cache/`。缓存和检索索引都有内部元数据日志，排除在用户文件 undo 历史之外。真实付费服务未实测，HTTP 行为通过本地模拟服务验证。

## 安全边界与验证

扫描跳过链接/junction 和 `.git`、`node_modules`、`.venv`、`venv`、`__pycache__`、`.curator`；用户文件输出不能写入这些区域。工具自身的锁、日志、暂存、缓存、索引和恢复归档属于显式内部元数据。首次写事务会创建必要的内部目录。

没有删除或覆盖命令；写新文件使用暂存文件和系统原子不覆盖重命名。仍建议在副本上先预览。路径检查与执行之间无法对抗有权限的恶意进程持续置换目录，这不是一个文件系统权限隔离工具。

```sh
uv run pytest --cov=curator --cov-report=term-missing
uv run mypy --strict src
uv run ruff check .
uv run ruff format --check .
uv build
# Windows 实测扫描时间、进程峰值工作集与外部 junction 拒绝：
uv run python tools/acceptance_windows.py
```

本轮完整结果见 [ACCEPTANCE.md](ACCEPTANCE.md)，原始性能数据见 [ACCEPTANCE-METRICS.json](ACCEPTANCE-METRICS.json)。Windows 已验证强制终止恢复；Linux/macOS 原子系统调用分支、POSIX kill -9 和指向 /etc 的真符号链接仍需要对应系统实测。

阶段记录：[1](STAGE-1.md) · [2](STAGE-2.md) · [3](STAGE-3.md) · [4](STAGE-4.md) · [5](STAGE-5.md) · [6](STAGE-6.md) · [7](STAGE-7.md) · [8](STAGE-8.md)。历史阶段中的“待完成”描述反映当时状态，最终结果以验收报告为准。
