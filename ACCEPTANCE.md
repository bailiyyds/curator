# 最终验收记录

验证日期：2026-11-05。环境：Windows、Python 3.12.14；工作目录为本仓库。阶段一至八的主要功能已实现。下表区分本机证据与仍需其他环境验证的条件，不把平台等效测试当成 POSIX 实测。

## 原始八项验收

| 编号 | 要求 | 本轮结果与证据 |
|---|---|---|
| 1 | 1000 混合文件，扫描 <10 秒，峰值内存 <300 MB | 通过：文本、Markdown、Python、有效 PDF、图片、二进制，共 1000 份；扫描 1.070 秒，Windows GetProcessMemoryInfo 的进程 PeakWorkingSetSize 为 41857024 字节（约 39.92 MiB）。不是仅测 Python 分配量。原始数据见 ACCEPTANCE-METRICS.json；复现脚本 tools/acceptance_windows.py。 |
| 2 | 十类已知缺陷全部命中，无额外命中 | 通过：tests/test_review.py::test_ten_known_defect_categories_without_extra_hits；仅此固定夹具满足零额外命中，不推及任意仓库。 |
| 3 | 50 文件 rename/apply/undo 全部哈希还原 | 通过：tests/test_transactions.py::test_fifty_roundtrip。 |
| 4 | apply 中途 kill -9 后正确恢复 | Windows 等效验证通过：test_abrupt_kill_after_rename_before_commit 强制结束子进程，覆盖移动完成但未 commit 的窗口；另有跨目录移动与批量文档发布的强制结束恢复测试。真正 POSIX kill -9 尚未实测。 |
| 5 | 断网时五个主要命令可用 | 通过离线连接拒绝测试：tests/test_library.py::test_all_primary_commands_with_network_blocked；scan/organize/rename/review --no-llm/doc --from ast 均运行成功、不写入源目录。Python socket.connect 被禁止；不是修改系统防火墙的物理断网实验。 |
| 6 | 指向 /etc 的符号链接被拒绝，不写入 | 尚未完成原样验收：本机无创建真符号链接权限，相关测试跳过。已实测 Windows 指向外部临时目录的 junction：扫描报告 skipped，safe_path 拒绝经链接写入，外部哨兵文件内容和目录清单不变。详见原始指标 JSON。 |
| 7 | 覆盖率 >80% | 通过：159 passed、1 skipped；总覆盖率 92%，3305 条语句、270 条未覆盖。终端命令 pytest --cov=curator --cov-report=term-missing -q。 |
| 8 | examples 数据和三个可运行命令 | 通过：examples/README.md 给出 scan/review/doc 三条命令；本机另实跑 search/duplicates/config init，六项命令均退出 0 且 JSON 可解析。见 ACCEPTANCE-MANUAL.json。 |

## 类型、风格、打包

- mypy --strict src：通过，44 个源文件。
- ruff check .：通过；ruff format --check .：76 个文件格式正确。
- uv lock 与 uv sync --frozen：通过；默认未安装可选语义模型依赖。
- uv build：wheel 与 sdist 生成成功；wheel 中 review/title 提示词、文档模板及新增检索模块已核对。
- 新增 11 项测试覆盖搜索位置、中文 token、预算、不可覆盖索引、缓存复用与失效、损坏回退、失败日志、重复建议、配置初始化、离线运行和本地语义接口。
- 语义接口测试使用模拟 SentenceTransformer，确认 local_files_only=True、trust_remote_code=False、safetensors 约束、向量索引落盘和二次查询仅编码查询。没有下载或运行真实模型。

## 仍需验证的边界

- Linux/macOS 的原子不覆盖系统调用、真正 kill -9、指向 /etc 的符号链接与各自文件系统行为。
- 真实本地向量模型的加载兼容性、效果、内存与推理时间；可选语义功能目前不能据模拟测试宣称模型实测通过。
- 真实付费 OpenAI 兼容服务：已有本地 HTTP 模拟服务证据，没有真实服务质量保证。
- 搜索行号是 Document.body 的抽取文本行号，Markdown/PDF 不保证映射到源文件物理行；JSON 明确携带 line_basis。
- 文件系统检查不是抵御同权限恶意进程持续置换路径的安全沙箱；写入前应审阅计划并保留独立备份。

阶段报告记录当时结果，最终状态以本报告及当前测试为准。
