# 阶段四：文件整理

## 新增与修改文件
- 新增 `src/curator/core/organize.py`：type/date/topic 提案、冲突区、目录提案与独立 cleanup 列表。
- 新增 `src/curator/analyze/{__init__,docs}.py`：本地 TF-IDF、代表样本层次聚类、主题标签上限。
- 修改 `src/curator/core/apply.py`：显式 mkdir、跨目录 move、父目录与目标结构预检。
- 修改 `src/curator/core/undo.py`：跨目录恢复、新建空目录归档、目录身份校验。
- 修改 `src/curator/models/changes.py`：独立 CleanupProposal 与 journal 目录身份字段，旧计划兼容默认值。
- 修改 `src/curator/core/config.py`：max_topic_documents 预算，默认 1024。
- 修改 `src/curator/cli/changes.py`：organize 命令、类型化策略参数、整理计划独立导出及清理提案预览。
- 新增 `tests/test_organize.py`；调整 `tests/test_transactions.py`，允许合法 move，仍拒绝未支持的 archive 计划。
- 新增 `examples/organize/` 文本示例；更新 README 与本阶段记录。

## 关键设计决策
- 按类型分为 docs/code/images/archives/other；归档类型结合扩展名识别。
- 日期目录使用 mtime 的 UTC YYYY/MM，避免同一目录在不同时区产生不同提案。
- 主题使用本地 TF-IDF 和最多 64 个代表文档的层次聚类，其余文档按余弦相似度归类。
- 主题总标签最多 12 个，包含“其他”；默认最多分析 1024 个文档、每文档 20000 字符、128 个词项。
- 超出主题预算或无法匹配的文件明确归入“其他”，超预算逐项警告，不联网、不下载模型。
- 同名文件进入目标目录下的 _conflicts/，必要时追加数字，不覆盖已有或同批目标。
- 已在 --into 输出目录内的文件不重复整理，保证重复运行不再次搬动结果。
- mkdir 作为独立 Change，执行前先刷盘意图，成功后记录设备与 inode 身份。
- 撤销会先恢复文件，再归档本事务创建且仍为空、身份未变的目录；原有目录保留。
- 旧目录 cleanup 只列在独立列表中，requires_confirmation=true，本阶段不执行清理。

## 验证证据
- Python 3.12.14、uv 环境：67 passed、1 skipped，全量覆盖率 91%。
- mypy --strict：25 个源文件通过；ruff check 与 format --check 通过。
- type/date/topic 三种策略：提案稳定、默认无写入、apply/undo 后原路径和 SHA-256 一致。
- 类型目录、UTC 日期目录、大小写不敏感同名处理、现有目标保留及 -2 后缀：通过。
- 英文相近主题合并、不同主题区分、中文二元词项、空文档回退及标签总数上限：通过。
- 主题预算超限回退并警告：通过。
- 重复整理时跳过输出目录，原始空目录与移动后空目录仅出清理提案：通过。
- 撤销期间发现新增文件或目录身份被替换：拒绝移动该目录，原数据保留。
- 强制终止跨目录移动进程后，prepare 记录恢复与未处理文件完整性：通过。
- 原阶段三的 50 文件还原、重命名中断恢复、锁、SHA-256 与保护区测试继续通过。
- 独立 CLI 手动验证：三种策略分别预览、执行、撤销，9 次文件处理均核对哈希成功。
- 整理计划单独导出不移动文件、JSON 输出、终端确认和非法 --by 返回 2：通过。
- 真实外部符号链接测试因本机 Windows 权限跳过，没有计为通过。

## 示例命令
```sh
uv run curator organize examples/organize --by type --json
uv run curator organize examples/organize --by date
uv run curator organize examples/organize --by topic
# 仅导出整理计划，不移动文件
uv run curator plan examples/organize --operation organize --by type -o plan.json --apply --yes
# 审阅后再执行
uv run curator apply examples/organize/plan.json --apply --yes
uv run curator undo examples/organize --last 1 --apply --yes
```

## 限制与下一步
- 本阶段只在 Windows 实机验证，Linux/macOS 系统调用分支仍待验证。
- 跨目录移动要求同一文件系统；跨设备不做复制/删除回退，失败会保留源文件并报告。
- 崩溃窗口内仅有 prepare、尚无成功提交的 mkdir 无法确认归属，撤销保留目录并提示人工审阅。
- 层次聚类在代表样本上运行，标签是统计词项，不等同于 LLM 语义主题；大型语料可能集中到“其他”。
- 不自动删除或清理旧目录；cleanup 没有执行入口，后续若需要须单独确认并实现归档流程。
- 进程锁与内容校验不承诺抵御不配合进程的所有路径替换竞争或机器断电故障。
- 阶段四确认后进入阶段五：静态代码规则引擎、精确位置、合并排序与审查报告。
