# 阶段六：兼容 Provider、缓存与 LLM 复核

## 新增与修改文件
- 新增 `src/curator/llm/{__init__,provider,reviewer,namer}.py`：HTTP 抽象、严格复核、标题建议。
- 新增 `src/curator/llm/prompts/{review,title}.txt`：独立提示词资源，随安装包交付。
- 修改 `src/curator/core/config.py`：嵌套 LLM 配置合并、环境变量、凭据隔离与错误信息脱敏。
- 修改 `src/curator/cli/review.py`：LLM 参数、审计展示、--no-cache 与显式缓存写入。
- 修改 `src/curator/cli/changes.py`、`src/curator/core/plan.py`：可选 LLM 标题建议，保留确定性命名与冲突处理。
- 修改 `src/curator/core/{journal,undo}.py`：区分内部缓存日志与用户文件事务。
- 修改 `src/curator/models/findings.py`：llm_confirmed、llm_reason 与调用统计。
- 新增 `tests/test_llm.py`、`tests/conftest.py`，测试隔离真实环境密钥。
- 新增 `examples/llm-config.toml`；更新 pyproject.toml、uv.lock、README。

## 关键设计决策
- 使用 httpx 直接调用 /chat/completions，不引入厂商 SDK 或 agent 框架。
- base_url、api_key、model 可切换兼容服务；回环地址允许无 key，本地协议兼容模式不依赖登录。
- 默认 HTTP 总期限 60 秒，重试与退避包含在期限内，最多 3 次重试；缓存文件 IO 不计入 HTTP 期限。
- 默认输出上限 512 tokens；可选 max_tokens 或 max_completion_tokens 参数，JSON mode 可配置。
- 单次输入默认 2000 预算，以 UTF-8 字节数加固定聊天开销作保守上界，不声称适用于所有厂商 tokenizer 的精确计数。
- 只复核现有命中位置（包括潜在除零等不确定项），最多前后各 5 行；不发送整份文件，不自动扩展到未命中代码。
- 过短、变化、无法读取或无法在预算内切片的文件保留规则结果并报告，不为满足调用量而发送整份文件。
- 默认并发 4、每次最多 64 个命中任务；可配置，超过预算的结果保留规则来源。
- 模型输出采用 strict Pydantic JSON schema；未确认、坏 JSON、超时、网络失败和单任务异常都保留静态结果。
- confirmed=true 才标记 source=llm；模型补丁只展示，校验路径、头部和 hunk 计数，不自动执行或应用。
- 缓存 key 为 sha256(规范化 prompt + model + temperature)，用端点与生成参数命名空间隔离不同服务。
- 缓存只存验证后的响应和非敏感元数据，不存原始 prompt 或 API key；坏缓存忽略且不覆盖。
- 为遵守默认只读，默认只读已有缓存；写缓存需要 --write-cache --apply 和确认，--no-cache 禁用读写。
- 缓存采用追加式 prepare/commit 日志与暂存原子发布；内部缓存日志不参与 undo --last。
- OPENAI_API_KEY 不自动发往其他主机；自定义服务使用 CURATOR_LLM_API_KEY。错误和配置 dump 不展示凭据。
- 标题建议只输入部分文档，模型只能返回标题与置信度，最终路径仍由安全规划器生成；frontmatter/H1 继续优先。

## 验证证据
- Python 3.12.14、uv 环境：126 passed、1 skipped，全量覆盖率约 92%。
- mypy --strict：32 个源文件通过；ruff check、format --check 通过。
- uv build 通过；wheel 内 review.txt 和 title.txt 提示词资源均存在且非空。
- 模拟协议：消息结构、输出参数、JSON mode、Authorization、重定向不跟随、错误正文不回显：通过。
- HTTP 429/5xx、连接失败、重试次数、总期限、坏 JSON、非严格 schema、超大响应与输入预算：通过。
- 缓存首次写入、只读命中、不同生成参数隔离、--no-cache、损坏缓存不覆盖、坏 schema 不缓存：通过。
- 缓存日志不进入用户撤销历史；默认调用不生成 .curator：通过。
- 并发上限、明确不确认、单任务意外异常隔离、源内容变化、单行文件与任务预算回退：通过。
- 有限上下文、长行裁剪、严格禁止完整文件上传、Unicode 字节预算：通过。
- 补丁路径与 hunk 校验、拒绝越界与多文件头、永不修改源代码：通过。
- 人工可读来源、复核理由、建议与补丁围栏：通过。
- 嵌套配置优先级、错误脱敏、缺 key、无 model、缓存写入确认及拒绝相互冲突参数：通过。
- 可选标题建议、可靠原标题优先、路径字符串只能转 slug、单任务失败隔离：通过。
- 真实回环 HTTP 服务 + 独立 CLI 进程：2 个发现确认、2 次缓存命中、命名执行与撤销、源文件 SHA-256 不变。
- 原阶段一至五完整回归继续通过；真实外部符号链接测试仍因本机 Windows 权限跳过。
- 未调用真实付费服务，未验证具体商业模型或运行中的 Ollama 模型。

## 使用方式
```sh
# 默认仍是纯静态、无网络
uv run curator review examples/review --no-llm
# 显式启用已配置服务，默认不写缓存
uv run curator review examples/review --llm --model your-model --base-url http://127.0.0.1:11434/v1 --json
# 显式允许响应缓存写入
uv run curator review examples/review --llm --write-cache --apply --yes --json
# 禁用缓存读写
uv run curator review examples/review --llm --no-cache --json
# 标题建议只生成提案，默认不改名
uv run curator rename examples --source llm --no-cache --json
```
- CURATOR_LLM_BASE_URL、CURATOR_LLM_MODEL、CURATOR_LLM_API_KEY 提供环境默认值。
- 优先级：CLI > 项目配置 > 用户配置 > 环境默认值 > 内置默认值，LLM 表按字段合并。
- 也可用 OPENAI_API_KEY，但仅自动绑定 api.openai.com；不要把真实 key 发到聊天或提交到仓库。
- JSON 自动化涉及写入时请加 --yes，避免交互提示混入输出。
- rename 的 LLM 标题路径始终不写缓存，--no-cache 可以同时禁用缓存读取。

## 限制与下一步
- 兼容协议不等于所有厂商/所有模型支持相同参数；可在配置中切换 token_parameter、json_mode。
- 输入预算是保守字节上界，未捆绑任何 tokenizer 或联网模型下载。
- 未确认结果不会被删除；用户可按 llm_confirmed 审阅，统计中的 rule_counts 仍是原始规则计数。
- 模型可能误判，补丁只做结构与目标路径检查，不证明语义正确或可应用。
- 标题置信度是建议而非证明；阈值以下继续标高风险并默认跳过。
- 当前实际验证为 Windows 与本地模拟服务，跨平台和真实模型效果仍待验证。
- 阶段六确认后进入阶段七：基于 AST 与 git 历史生成 Markdown 文档。

## 协议依据
- [OpenAI 官方 Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
