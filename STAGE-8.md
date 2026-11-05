# 阶段八：检索、相似度、性能与最终交付

## 文件清单

新增 core/search.py、core/search_index.py、analyze/semantic.py、analyze/duplicates.py、cli/library.py、safety/limits.py、tests/test_library.py、tools/acceptance_windows.py、examples/README.md，以及 ACCEPTANCE.md、ACCEPTANCE-METRICS.json、ACCEPTANCE-MANUAL.json。

修改 CLI 注册与日志选项、scan 日志、undo 内部索引日志过滤、rename 语言偏好及 LLM 标题输入、pyproject.toml 的可选 semantic 依赖与项目描述、uv.lock 和 README。

## 设计决策

- 关键词采用中文字符/双字组合与拉丁词 token，按 Document.body 建倒排索引，不下载分词模型。
- 只读查询默认在内存运行；保存索引需要 --build-index --apply，沿用锁、prepare/commit 刷盘和原子不覆盖发布。
- 不可变索引以源路径、内容哈希、根路径和抽取大小预算定位，旧索引保留，损坏索引明确提示后降级，拒绝覆盖。
- 结果与索引分开，普通 JSON 不泄露整份本地文档正文，位置明确标为 extracted-text。
- 语义模型必须来自扫描根之外的可信本地目录，限制内置模块和 safetensors，拒绝代码、pickle 与链接，禁用自动下载和远程代码。
- 可选语义任务失败隔离；文档向量按源内容与模型内容哈希保存，查询向量不保存，缺依赖或缺模型回退关键词。
- 精确重复按 SHA-256 全量分组，近似建议用确定性 64 位 simhash 和明确预算，仅报告、不自动处理文件。
- 配置初始化沿用新文件事务及 undo，不覆盖现有配置；--verbose 输出诊断日志，常规结果保留结构化输出。
- 本阶段不引入扫描并发：Windows 实测已经满足性能预算，顺序扫描保持确定性并限制峰值内存；LLM 路径已有可配置并发。

## 自检结果

159 项测试通过、1 项因 Windows 真符号链接权限跳过，总覆盖率 92%。mypy 严格检查 44 个源文件通过，Ruff 检查与格式通过；uv lock/sync/build 通过。新增 11 项测试通过。手动实跑六项命令并校验 JSON；1000 混合文件扫描约 1.07 秒、峰值进程工作集约 40 MiB；Windows 外部 junction 拒绝与外部内容不变验证通过。

原始八项验收逐条结果、原始数据与剩余平台条件见 ACCEPTANCE.md；不能把 Windows 强制结束等同于已测试 POSIX kill -9，也不能把模拟模型接口等同于真实模型验证。

## 遗留验证

Linux/macOS、真符号链接以及真实向量模型和真实远程服务仍需在相应环境验证。核心八阶段开发已交付，本阶段结束不继续扩展 GUI、云同步或监听功能。
