# 阶段一检查记录

## 文件
- src/curator/cli/__init__.py：预算参数、JSON 错误与部分失败退出码。
- src/curator/core/config.py：配置合并、链接策略验证。
- src/curator/core/scan.py：只读、稳定快照、保护目录与预算。
- tests/test_scan.py：10 项测试，包含独立进程的 1000 文件性能测量。
- pyproject.toml、uv.lock、README.md：工具配置、依赖锁及已知限制。

## 决策
- 快照时间使用最大文件 mtime，保证未改动目录的输出稳定。
- 不跟随任何符号链接或 junction，减少跨平台路径逃逸风险。
- 性能测量使用独立进程，避免覆盖率跟踪器干扰。
- 暂不提供文件输出，等待统一 journal 写入机制。

## 验证
- Python 3.12.14：9 passed，1 skipped；覆盖率 89%。
- 1000 文件扫描：完整计数，耗时 <10 秒，Python 分配内存峰值 <300 MB。
- 注意：tracemalloc 只测 Python 分配，不代表进程 RSS，完整 RSS 验收仍待补充。
- mypy --strict --python-version 3.12 src：通过。
- ruff check、ruff format：通过。
- 示例目录扫描与稳定 JSON：通过。
- 外部链接拒绝：测试因当前 Windows 无创建链接权限而跳过，未验收。

## 限制与下一步
- 依赖通过 uv 解析并生成锁文件，但网络下载缓慢，完整 uv sync 尚在进行。
- 本次 pytest 使用 Python 3.12 和单独下载的测试工具，coverage 使用 Python tracer。
- 阶段二至八未实现；阶段一确认后进入文档抽取。
