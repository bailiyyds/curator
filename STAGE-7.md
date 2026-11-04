# 阶段七：AST 与 Git 文档生成

## 新增与修改文件
- 新增 `src/curator/models/docgen.py`：参数、声明、模块、Git 活跃度、输出提案与错误模型。
- 新增 `src/curator/analyze/docast.py`：六种语言声明、签名、字面量文档、装饰器/注解、参数和直接调用目标。
- 新增 `src/curator/analyze/git_history.py`：安全系统 Git、NUL 分隔 numstat、文件级聚合与预算。
- 新增 `src/curator/analyze/docgen.py`：模块/文件拆分、Markdown 目录、活动摘要、生成变体与 unified diff。
- 新增 `src/curator/core/artifacts.py`：多文件一次锁定和日志事务、预检、不覆盖、逐条落盘与撤销。
- 新增 `src/curator/cli/doc.py`；修改 `src/curator/cli/__init__.py` 注册 doc。
- 新增 `src/curator/templates/doc-module.md`，作为独立随包模板。
- 修改 `src/curator/core/config.py`：Git 提交数、总时间和输出字节预算。
- 新增 `tests/test_docgen.py`；新增 `examples/docgen/` 的 Python 和 Go 示例；更新 README。

## 关键设计决策
- 所有源结构来自 tree-sitter，不导入、执行、eval 或编译被扫描代码。
- Python docstring 按 AST 字面量提取，安全解码转义，不使用 literal_eval；其他语言读取相邻源注释。
- Markdown 包含自动目录、源文件行列、类/函数层级、签名块、参数表、装饰器与直接文本调用目标。
- docstring、签名、提交摘要等按数据渲染，用足够长的代码围栏和转义避免被解释为 HTML 或外链。
- --split module 以源模块为单位，Go 按同目录包名分组；--split file 保留每个源文件及扩展名。
- Git 只使用根目录之外的 PATH 系统可执行文件，拒绝扫描目录内的同名 Git。
- 显式禁用 pager、外部 diff、textconv、签名辅助、fsmonitor、替换对象和 lazy fetch，不执行仓库脚本。
- Git 读取默认 200 个最新提交、10 秒总期限、8 MiB 输出预算；不联网，忽略生成输出目录。
- Git 不可用、空仓库、超预算或不支持安全参数时，明确警告并降级为纯 AST。
- 当前人工文档不覆盖：有差异时生成 .generated.md，已有生成变体则分配 .generated-2.md 等。
- 内容未变化时标为 up to date，不重复写入；差异用于人工合并，不自动修改人工文档。
- 一批输出只用一个 journal，目录创建和文件发布前后逐条刷盘，undo 可一次恢复整批。

## 验证证据
- Python 3.12.14、uv 环境：148 passed、1 skipped，全量覆盖率 92%。
- mypy --strict：38 个源文件通过；ruff check、format --check 通过。
- uv build：wheel 与源码包通过；模块模板随 wheel 打包验证通过。
- Python 模块/类/函数、嵌套名称、签名、参数默认值/类型、装饰器、原文位置与调用关系：通过。
- 六种语言声明与参数；Java 注解、Rust impl/属性、Go 接收者、JS 箭头函数：通过。
- Unicode/转义/raw/相邻字符串 docstring、f-string 不误当 docstring、不执行顶层 raise：通过。
- Git 两次提交的文件次数、增删行、最近摘要、二进制变更、子目录范围、非代码仓库索引：通过。
- 仓库配置的外部 diff/textconv 辅助程序未被调用：通过。
- Git 缺失、源目录内伪 Git、输出预算、超时、非法路径和错误 numstat 记录：安全降级或拒绝。
- 默认预览没有创建 docs/.curator；生成文档、重复跳过、原文档保留、.generated 后缀与差异：通过。
- 多文件碰撞预检、单文件 IO 失败隔离、整批撤销、发布后未提交结果时强制终止恢复：通过。
- 解码失败结构化报告并返回 4，非法路径返回 3，非法 --from 返回 2：通过。
- 独立 CLI 演示：2 个模块、3 个输出、2 次 Git 提交；人工文档保留，重复无写入，一次 undo 恢复原文件及 SHA-256。
- 先前阶段的完整测试继续通过；真实外部符号链接测试仍因本机 Windows 权限跳过，未计为通过。

## 使用方式
```sh
# 默认只读预览
uv run curator doc examples/docgen --from ast
uv run curator doc examples/docgen --from both --split module --json
uv run curator doc examples/docgen --from git --split file
# 显式写入根目录内的 docs/，不覆盖现有文件
uv run curator doc examples/docgen --from both --out docs --apply --yes
# 一次撤销本批生成文档
uv run curator undo examples/docgen --last 1 --apply --yes
```
- --out 必须是被扫描根目录内的相对目录，不允许保护区、链接或路径逃逸。
- JSON 提案包含所有 Markdown 内容、实际目标、差异、跳过项、错误与警告。
- 成功审阅返回 0，部分读取/写入失败返回 4；Git 降级不是整体失败。
- Git 预算可用配置 doc_git_max_commits/doc_git_timeout/doc_git_max_bytes 调整，提交数也支持 --git-max-commits。

## 限制与下一步
- 调用关系为 AST 中直接出现的文本目标，不证明动态派发、继承、跨模块别名或运行时调用图。
- 注释/docstring 按字面提取，不推断无文档参数的意义；生成内容仍需人工审阅。
- Git 摘要仅包含已提交历史，不包含未提交更改；默认窗口最多 200 个提交，不跨重命名前后的文件路径追踪。
- --from git 的模块页不展示 AST 声明，文件活动汇总在索引与相关模块页中。
- 已有文档差异读取要求 UTF-8 且不超过大小预算；无效旧输出会拒绝生成，不覆盖文件。
- 原有目录不删除；撤销新文件会归档到恢复目录，本事务创建的空目录才按归属检查归档。
- 当前实机验证为 Windows；其他平台与进程外路径竞争、断电恢复的限制沿用前阶段。
- 阶段七确认后进入阶段八：关键词/可选语义检索、去重、配置初始化、性能与完整验收、README/examples 收尾。

## Git 接口依据
- [git log 官方文档](https://git-scm.com/docs/git-log)
- [Git 安全读取参数](https://git-scm.com/docs/git)
