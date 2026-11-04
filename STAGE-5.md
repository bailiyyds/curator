# 阶段五：静态代码审查

## 新增与修改文件
- 新增 `src/curator/models/findings.py`：逐字段实现 Finding，补充合并规则列表与 ReviewReport。
- 新增 `src/curator/analyze/code.py`：AST 规则、词法作用域、局部保护判断、证据定位、合并与排序。
- 新增 `src/curator/cli/review.py`：review 命令、text/JSON/Markdown 渲染、显式日志化导出。
- 修改 `src/curator/cli/__init__.py`：注册 review。
- 修改 `src/curator/core/scan.py`：JSX/TSX 纳入代码文件扫描。
- 新增 `tests/test_review.py`：已知缺陷、正常反例、多语言、位置、过滤和导出测试。
- 新增 `examples/review/*.py`：10 类已知缺陷与遗留标记演示，不应执行这些缺陷样本。
- 修改 pyproject.toml：只将故意保留缺陷的 examples/review 从 Ruff 检查与格式化排除；实际源代码及测试继续检查。
- 更新 README 与本阶段记录。

## 已实现规则
| ID | 行为 |
| --- | --- |
| bare-except | 裸 except |
| swallowed-exception | 宽泛捕获后仅 pass |
| mutable-default | list/dict/set 等可变默认值 |
| none-comparison | 用 == 或 != 与 None 比较 |
| unused-import | 未使用导入，考虑简单词法遮蔽、别名、显式导出 |
| long-function | 函数超过 80 行 |
| too-many-parameters | 声明参数超过 6 个 |
| loop-io | 循环内 await 或已知/潜在同步 IO |
| unclosed-resource | 文件/连接创建后无可见 with、close 或所有权转移 |
| mixed-formatting | f-string 与百分号格式化混用 |
| possible-zero-division | 常量零除数，或无可见非零保护的动态除数 |
| log-only-except | 捕获异常后仅记录日志并继续 |
| todo | 注释中的 TODO/FIXME/XXX，保留各标记出现次数 |
| syntax-error | 补充报告 tree-sitter 恢复的语法错误与缺失节点 |

## 关键设计决策
- 代码结构全部取自 tree-sitter AST，不导入、执行或 eval 被扫描代码；正则仅用于注释标记等文本匹配。
- Python 实现全部专属规则；其他五种语言及 JSX/TSX 实现通用复杂度、循环 IO、标记与语法规则。
- line/col 是一基行号与 Unicode 字符列，evidence 保存原代码行；只为分析归一化换行，不写回源文件。
- 同一 path/line 合并，保留最高严重度、全部 merged_rules 与建议，按严重度/路径/行列稳定排序。
- rule_counts 统计所选规则的唯一文件/行命中，发生在严重度过滤前；marker_counts 统计全部标记次数。
- --severity warning+ 默认显示 warning 和 error；info+ 包含标记，info 只显示 info。
- --rules 支持 all/default/python/general 或逗号分隔规则 ID；--lang 支持语言短名或完整名。
- 审查发现缺陷不代表执行失败，正常完成返回 0；读取/解码部分失败返回 4，参数错误返回 2，安全拒绝返回 3。
- -o 默认只预览；写入报告必须 --apply，并输入 yes 或提供 --yes，沿用不覆盖和 journal 机制。
- --llm 在本阶段明确提示降级并保留规则结果，不调用服务。

## 验证证据
- Python 3.12.14、uv 环境：104 passed、1 skipped，全量覆盖率 92%。
- mypy --strict：28 个源文件通过；ruff check、format --check 通过。
- 10 文件受控缺陷集：10 类全部命中，每个文件的规则集合与预期完全一致，无额外规则命中。
- 独立正常代码样本：with、finally close、返回资源所有权、显式 ZeroDivisionError、早退保护、不可变默认值，均零命中。
- 导入别名、参数遮蔽、属性名称、__all__、显式重导出、默认参数外层引用：通过。
- 文件:行:列、原始 evidence、Unicode 列号、LF/CRLF/CR 和 Unicode 行分隔字符：通过。
- 同行合并最高严重度、稳定 ID、严重度/规则/语言过滤、标记出现次数：通过。
- 六种语言的循环 IO 与 TODO，其他语言参数数量通用规则、TSX：通过。
- 语法缺失节点、解码失败与跳过原因、不执行输入代码、禁止网络调用：通过。
- 三种报告格式、Markdown 代码围栏、JSON 参数错误、确认、拒绝覆盖和导出撤销：通过。
- 独立 CLI 演示：11 个样本文件得到 11 个合并位置，三种格式可读，导出并撤销后源文件 SHA-256 不变。
- 原阶段一至四扫描、抽取、重命名、整理与强制终止恢复测试继续通过。
- 真实外部符号链接测试仍因本机 Windows 权限跳过，没有计作通过。

## 示例命令
```sh
uv run curator review examples/review --no-llm --severity info+
uv run curator review examples/review --lang py --format json
uv run curator review examples/review --format md
# 审阅后显式导出，相对扫描根目录，不覆盖已有报告
uv run curator review examples/review -o findings.json --format json --apply --yes
```

## 限制与下一步
- “无误报”证据仅适用于受控缺陷集与正常反例，不保证任意仓库零误报或零漏报。
- 除零、资源、循环 IO 为局部启发式；不做完整数据流、类型、全路径证明或跨模块调用分析。
- 导入解析是有限词法分析，动态导入、复杂 global/nonlocal、字符串前向注解与框架副作用可能需要人工复核。
- TODO/FIXME/XXX 只统计注释，不将普通字符串数据视为遗留工作。
- patch 字段保留为可选，当前不自动生成或应用代码补丁。
- 当前实际验证平台为 Windows，其他平台运行与完整性能打磨留待后续。
- 阶段五确认后进入阶段六：OpenAI 兼容 provider、超时/重试/缓存与有预算的 LLM 复核降级。
