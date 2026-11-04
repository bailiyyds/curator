# 阶段二：文档抽取

## 新增与修改文件
- 新增 `src/curator/models/document.py`：Document、Heading、CodeBlock、Page、Word、Symbol、ExtractionResult、ExtractionReport。
- 新增 `src/curator/extract/{__init__,text,md,pdf,code,detect}.py`：安全入口、批处理、四类抽取及内容分类。
- 新增 `src/curator/safety/{__init__,bincheck}.py`：8 KiB 二进制嗅探。
- 修改 `src/curator/cli/__init__.py`：新增纯只读 extract 命令。
- 修改 `src/curator/core/scan.py`：魔数嗅探、保护目录大小写处理。
- 新增 `tests/test_extract.py`、混合 examples；更新 pyproject.toml、uv.lock、README。

## 关键设计决策
- 所有抽取经过根目录、链接、保护区和大小预算检查，预期失败返回结构化错误。
- PDF 使用 pdfplumber，保留页码、表格和词坐标；任一页面无文本层即标记 needs_ocr，并汇总 PDF 数量。
- Markdown 使用 markdown-it-py token 与 safe_load，保留 frontmatter 后的原始标题和代码块行号。
- 代码使用六种语言官方 tree-sitter 包，仅解析字节；保留 AST 声明位置与语法错误提示。
- UTF-8 优先，失败后使用置信度至少 0.6 的 chardet 检测；不替换无效字符。
- 二进制前缀优先于文本扩展名，PDF 交给专用解析器。

## 验证证据
- Python 3.12.14、uv 环境：30 passed、1 skipped，总覆盖率 95%。
- PDF 文本、两页页码、词坐标、2×2 表格、无文本层页及损坏文件：通过。
- Markdown frontmatter、H1、外链、代码块及原始行号：通过。
- Python、TypeScript、JavaScript、Go、Rust、Java 和 TSX AST：通过。
- 非 UTF-8 编码、无法确定编码、NUL 与二进制魔数：通过。
- 非法路径、保护区、负数预算、文件大小限制、禁止执行代码、无网络读取：通过。
- CLI JSON、失败汇总、OCR 提示、默认无写入：通过。
- mypy --strict、ruff check、ruff format：通过。
- 1000 文件扫描回归：通过。
- 真实外部符号链接：Windows 权限不允许创建，沿用阶段一跳过；模拟链接拒绝分支通过，不能代替跨平台验收。

## 限制与下一步
- 无 OCR 引擎，扫描件只标记和报告；符合本阶段范围。
- PDF 大小预算限制输入字节，尚无对恶意压缩 PDF 的进程级 CPU/内存隔离。
- Markdown body 为归一化文本；Heading/CodeBlock 行号对应原文，正文尚无逐行 source map。
- 文档仅输出到终端，统一文件写入与 journal 留待阶段三。
- 尚未实现 rename、organize、review、LLM、docgen、search。
- 阶段二确认后进入阶段三：rename 的 plan/apply/journal/undo 与中断恢复。

## API 依据
- https://github.com/tree-sitter/py-tree-sitter
- https://github.com/jsvine/pdfplumber
