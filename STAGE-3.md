# 阶段三：重命名事务与恢复

## 新增与修改文件
- 新增 `src/curator/models/changes.py`：逐字段实现 Change、Plan、JournalEntry，并补充 OperationReport。
- 新增 `src/curator/core/plan.py`：纯只读命名提案、标题优先级、slug、稳定 ID、大小写不敏感冲突分配、高风险标记。
- 新增 `src/curator/core/apply.py`：整份计划预检、显式执行、SHA-256 校验、高风险跳过、单条失败隔离、原子导出。
- 新增 `src/curator/core/journal.py`：进程级文件锁、JSONL 意图与结果、逐条 flush/fsync、不覆盖目标的系统重命名。
- 新增 `src/curator/core/undo.py`：倒序撤销、准备记录恢复、修改与冲突保护、已撤销事务过滤。
- 新增 `src/curator/safety/paths.py`：根目录、链接、保护区、Windows 设备名和路径规范检查。
- 新增 `src/curator/cli/changes.py`：rename、plan、apply、undo 参数、预览、确认、JSON 和退出码。
- 修改 `src/curator/cli/__init__.py`：注册事务命令，接入 scan -o 的显式写入。
- 修改 `src/curator/core/scan.py`、`src/curator/extract/__init__.py`：共用保护区定义。
- 新增 `tests/test_transactions.py`、`examples/rename/*.md`；更新 pyproject.toml、uv.lock 和 README。

## 关键设计决策
- 所有用户文件写入默认关闭，只有 --apply 才会写入；终端必须输入 yes，或显式使用 --yes。
- 提案、执行、日志和撤销分离；plan 命令的 --apply 只导出计划，apply 命令才执行计划。
- 执行前预检整份计划，拒绝目录逃逸、保护区、重复源或目标、重命名链与环。
- 先持久化 prepare，再执行重命名，再持久化 commit，填补“已改名但尚无结果记录”的崩溃窗口。
- 使用系统不覆盖目标的原子重命名发布临时文件；没有采用会覆盖既有目标的 os.replace。
- 进程锁由操作系统持有，进程强制终止后自动释放，不以锁文件是否存在判断活跃状态。
- 高风险条目默认跳过，--yes 不会绕过风险策略，必须另加 --allow-high-risk。
- 扩展名统一小写；无可信标题的任意文件类型使用原文件名去噪并标为高风险。
- 撤销导出文件会归档到 .curator/trash/<unique-id>/，不提供删除操作。
- .curator 的控制目录、锁、暂存文件和日志是内部元数据；用户计划不能操作该保护区。

## 验证证据
- Python 3.12.14、uv 环境：56 passed、1 skipped，总覆盖率 91%。
- mypy --strict：22 个源文件通过；ruff check、format --check：通过。
- uv build：wheel 与源码包均通过。
- 50 个文档 apply 后 undo：原文件名、文件数量和 SHA-256 全部一致。
- 五文件事务在第三次改名后强制终止：前两条已提交、第三条仅 prepare、后两条未处理；undo 全部恢复。
- Windows 使用进程 kill 的 TerminateProcess 等价场景，不宣称已在 Linux 实际运行 kill -9。
- 源内容变化、目标冲突、撤销目标冲突、改名后内容变化：拒绝覆盖或恢复，原内容保留。
- 高风险默认跳过、显式允许、单条 IO 失败不影响后续条目：通过。
- 并发文件锁拒绝第二写入者、强制终止后锁释放：通过。
- 路径逃逸、保护区本身作为根、设备名、非规范路径、备用数据流、模拟链接：拒绝。
- 不完整末行忽略并警告，损坏日志头拒绝，禁止撤销 undo 日志：通过。
- scan/plan/rename/apply/undo 默认预览无用户文件写入；JSON、终端确认与退出码：通过。
- 独立手动 CLI 演示：3 个文件计划导出、实际执行、撤销、SHA-256 核对通过。
- 真实符号链接测试仍因本机 Windows 无创建权限而跳过；没有算作通过。

## 命令与语义
```sh
uv run curator rename examples/rename --json
uv run curator plan examples/rename -o plan.json --apply --yes
uv run curator apply examples/rename/plan.json --apply --yes
uv run curator undo examples/rename --last 1 --apply --yes
```
- 所有 -o 路径相对被扫描根目录，不能越界，父目录须已存在；不覆盖既有文件。
- -o 在 dry-run 下只预览，不创建文件；scan -o 和 plan -o 都需要 --apply 才导出。
- JSON 自动化写入请配合 --yes，避免交互提示混入输出。
- 实际执行出现跳过或失败返回 4，安全策略拒绝返回 3，预览返回 0。
- --journal 指定单个原始日志；--last 从最近的未完整撤销事务中选择，导出事务也计入历史。

## 限制与下一步
- 本机只实际验证 Windows；Linux renameat2 与 macOS renamex_np 分支待对应平台验证。
- 当前覆盖进程中断恢复，不承诺断电或存储硬件故障的完整恢复。
- 锁保护配合 curator 的写入者；无法完全消除不配合进程在检查与系统调用间替换路径的竞争。
- 为降低恢复歧义，当前拒绝重命名链、环与跨目录操作；跨目录整理留待阶段四。
- 崩溃产生的内部暂存文件保留在 .curator/staging/，不自动删除。
- LLM 标题生成和语言偏好尚未接入，--source llm 明确警告并使用启发式降级，留待阶段六。
- 尚未实现 organize、review、LLM、docgen、search；阶段三确认后进入阶段四 organize。

## 接口依据
- [Python 文件重命名语义](https://docs.python.org/3.12/library/os.html#os.rename)
- [Linux renameat2](https://man7.org/linux/man-pages/man2/renameat2.2.html)
