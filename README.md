# research-library

Python package migrated from the OpenClaw `paper-search-skill`: ADS/arXiv lookup, PDF download, extraction, and an MCP server.

## Install

```bash
uv pip install -e ".[pdf,mcp]"
```

### Conda（独立环境，避免与其它 Python 混用）

环境名：`research-library`（位于 `conda env list` 下列出的独立 prefix）。

```bash
conda create -n research-library python=3.11 pip -y
conda activate research-library
cd /Users/zenn/program/research_library_exploration
pip install -e ".[pdf,mcp]"
```

之后在本仓库里先 `conda activate research-library` 再运行 `research-lib` / `research-library-mcp`。

### 配置 API / 路径（`.env`）

在**本仓库根目录**使用 `.env`（已加入 `.gitignore`，勿提交 token）：

```bash
cp .env.example .env
# 编辑 .env，填入 ADS_API_TOKEN（申请：https://ui.adsabs.harvard.edu/user/settings/token ）
```

程序会**优先**加载该文件，再补充加载当前工作目录下的 `.env`；已存在于进程环境里的变量不会被覆盖。

可选：`RESEARCH_LIBRARY_DATA_DIR` 覆盖默认数据目录。

Data root defaults to `/Users/zenn/program-data/research_library` (`index/`, `pdfs/`).

## 仓库布局

| 位置 | 作用 |
|------|------|
| `pyproject.toml` | 包元数据、`research-lib` / `research-library-mcp` 入口 |
| `mcp/run.sh` | OpenClaw stdio MCP 启动包装：切到仓库根、优先 `.venv`、`python -m research_library.mcp_server`（路径勿随便改，OpenClaw 配置里写死了） |
| `.env.example` / `.env` | 仓库根配置模板与实际密钥（勿提交） |
| `chain_runs/`（可选） | 本地保存引用链 `--json`/`.md` 的运行产物；是否纳入版本管理自定 |

数据与缓存不在仓库内，而在 `RESEARCH_LIBRARY_DATA_DIR`（默认 `~/program-data/research_library`）。

## 源码结构（`src/research_library/`）

**子包**

| 路径 | 职责 |
|------|------|
| `library/db.py` | 本地 SQLite + FTS5（`library.db`）：入库、检索、统计 |
| `library/cli.py` | `research-lib library …` |
| `analysis/pdf.py` | PDF 抽文本、LLM 摘要与按问题摘录 |
| `analysis/llm/` | LLM 抽象与 MiniMax 实现 |

**顶层模块（CLI / MCP 仍从这里 import；体量合适时可再收到子包）**

| 文件 | 职责 |
|------|------|
| `cli.py` | 所有 `research-lib` 子命令的总入口、参数分发 |
| `mcp_server.py` | FastMCP 工具定义（lookup / download / library / pdf_analyze 等），stdio |
| `config.py` | 数据目录、`load_env`、`ADS_API_TOKEN` 助手 |
| `lookup.py` | ADS + arXiv 检索、BibTeX、下载、`pdf2ads` 等（原 paper_lookup 主体） |
| `ads_data_products.py` | ADS 数据产品统计与目录扫描 |
| `arxiv_keywords.py` | arXiv 按关键词轮询、写 `arxiv_cache.json`、可选写入 `library.db` |
| `pdf_extract.py` | 用 PyMuPDF 抽 PDF 表/图（可选依赖 `[pdf]`） |
| `refs_classify.py` | 参考文献列表 + 引用上下文管线（可调外部 LLM） |
| `ref_classifier.py` | 按 PDF 文本对引用做背景/方法/结果等分类（`lookup` 在需要时会子进程调用） |

**演进建议（未实施）**：若继续膨胀，可把 `lookup` + `ads_data_products` 收到 `discovery/`，`arxiv_keywords` 收到 `ingest/`，`pdf_extract` + `refs_*` 收到 `pdf_workflows/`，并把 `cli.py` / `mcp_server.py` 减薄为只做注册；改动手面大时需同步改文档与 MCP 相关说明。

## CLI

- `research-lib lookup …` — same subcommands as the original `paper_lookup.py` (`title`, `query`, `ref`, `bibtex`, `download`, `pdf2ads`). Use `download --library` to save under the configured `pdfs/` dir.
- `research-lib ads-data-products …`
- `research-lib arxiv-keywords …`
- `research-lib pdf-extract …`
- `research-lib pdf-analyze PATH [-q '问题'] [--json]` — PDF 抽文本 + LLM 中文摘要；可选 `--question` 做「按问题摘录」。加 **`--reference-chain`** 时按参考文献多跳跟读（见下节），产出 `markdown_report`；可用 `--max-hops`、`--no-persist-library`、`--no-library-refs` 等（`research-lib pdf-analyze --help`）。需配置 LLM（默认与 `QF_LLM_*` / `MINIMAX_*` 一致，见 `.env.example`）。
- `research-lib refs-classify …`

## 引用链（`pdf-analyze --reference-chain`）

多跳流程：对当前 PDF 做「跟读哪些参考文献编号 → 拉取子 PDF → （可选）写入 `library.db` 与 `paper_references` → 对子文重复」，最后合成一篇 Markdown 报告。根节点若已在库中且开启默认的 library 参考文献模式，**优先用 ADS 同步的 `paper_references` 建引用表**，不再回退解析 PDF 文末参考文献区。

**常用环境变量**（完整列表见 `.env.example`）：

| 变量 | 作用 |
|------|------|
| `RESEARCH_PDF_CHAIN_LIBRARY_REFS` | `1`（默认）用库内引用图；`0` 只解析 PDF 参考文献字符串 |
| `RESEARCH_PDF_CHAIN_AUTO_INGEST` | `1`（默认）根 PDF 不在库时先 `ingest` + 同步引用边（需 `ADS_API_TOKEN`） |
| `RESEARCH_PDF_CHAIN_MAX_FOLLOW_PER_HOP` | 每跳最多跟几条（`0` 不限制）；跑长链时可设为 `1` 控成本 |
| `RESEARCH_PDF_CHAIN_MAX_STEP_TOKENS` / `RESEARCH_PDF_CHAIN_STEP_PASS1_MAX_TOKENS` | 单步 completion 上限；推理模型建议 pass1 不要太低 |
| `RESEARCH_PDF_ACQUIRE_TIMEOUT` | 子文献 PDF `curl` 超时（秒） |

**示例**（先自备种子 PDF，或先用 `lookup download --bibcode …`）：

```bash
research-lib pdf-analyze /path/to/seed.pdf --reference-chain --max-hops 2 \
  -q "你的问题（如某星团、某方法）"
# 结构化结果（含 trace、每步 library_ingest）：追加 --json
```

说明：**`--json` 顶层字段 `library_ingested_ok`** 只统计带 `library_ingest` 且成功的子节点；根节点若走自动入库，请看 `trace[0].chain_auto_library_ingest`。子 PDF 可能落在数据目录下的 `pdf_chain_cache/...` 或 `pdfs/...`，与是否复制进标准 `pdfs/` 有关。

## LLM（MiniMax 推理类模型）与 completion

带推理链的模型会把部分额度用在内部 `<think>` 中。若**单次** `max_tokens` / pass1 上限过小，会出现去掉标记后「正文为空」、链中断。本仓库已：**提高引用链 pass1 默认上限**、在 MiniMax 客户端对「仅截断在 thinking」的响应**自动加倍重试一次**，并将 HTTP **`IncompleteRead`** 等纳入可重试网络错误。仍不稳定时可调高 `RESEARCH_LLM_MAX_COMPLETION_TOKENS`、`RESEARCH_PDF_CHAIN_STEP_PASS1_MAX_TOKENS` 或 `RESEARCH_LLM_TIMEOUT`（见 `.env.example`）。

## PDF 获取（bibcode 与下载失败）

仅有 **bibcode** 时也会用 `ADS_API_TOKEN` 走解析器尝试 arXiv / eprint / ADS / 出版商链接。旧版错误码 `no_arxiv_or_doi` 易误解：现已区分 **`ads_all_pdf_downloads_failed`**（已试 ADS 链路但均无可用 PDF）与真正的无标识情形。仅 bibcode 时会从 ADS **回填 DOI** 以利于解析。老期刊（如 *Nature* 早期）常见出版商付费墙或无效 eprint 链接，**不等于 ADS 查不到条目**。

## 本地文献库（SQLite + FTS）

数据根目录与 `pdfs/`、`arxiv_cache.json` 相同（默认 `/Users/zenn/program-data/research_library`，可用 `RESEARCH_LIBRARY_DATA_DIR` 覆盖）。索引库文件为 **`index/library.db`**。`research-lib arxiv-keywords` 在更新 `arxiv_cache.json` 的同时会把本轮匹配到的论文 **upsert** 进该库（可加 `--no-persist-db` 只写缓存不写库）。

批量把某个目录下所有 PDF 入库并做向量/FTS 索引，可用仓库内脚本：

`uv run python scripts/batch_ingest_pdfs.py "/path/to/Zotero/files" 2>&1 | tee batch.log`

（默认：`ingest_pdf_file` 复制到 `data/pdfs/`，写 `library.db`，按篇同步 `paper_references`，再对成功入库的 `paper_id` 调用 `semantic.index_papers`。）

- `research-lib library init` — 显式建表（首次写入前也会自动建表）
- `research-lib library stats` — 条数与路径
- `research-lib library search QUERY [--limit N] [--json]`
- `research-lib library import-cache` — 从现有 `arxiv_cache.json` 批量灌库
- `research-lib library ingest-pdf PATH.pdf` — PDF 自动抽元数据并匹配 ADS；**失败补录**可附加 `--doi`、`--arxiv`、`--match-title '…'`（与抽取结果合并），或 `--bibcode 19xxApJ…` 直接指定 ADS 记录；成功后可用 `library search` / `library semantic-index` 检索

## MCP (stdio)

`library_search`、`library_stats`、`library_import_cache`、`arxiv_keyword_scan`（`persist_db`）等见上；**`library_ingest_pdf`** 支持 `manual_doi` / `manual_arxiv` / `manual_match_title` / `manual_bibcode` 做失败重试。引用链可调用专用工具 **`pdf_reference_chain`**（参数与 CLI `--reference-chain` 对应），或 **`pdf_analyze`** 设 **`reference_chain=true`**；均可选 `llm_provider`、`max_hops`、`max_chars`/`max_chars_per_pdf`、`max_step_tokens`、`max_synth_tokens` 等。返回 JSON 含 `markdown_report`、`trace` 等（见上文「引用链」）。

本仓库提供与 `qf_mcp/run.sh` 相同用法的包装脚本（优先使用 `.venv`，否则 `PYTHONPATH=src` + Homebrew `python3.11`）：

```bash
openclaw mcp set research_library '{"command":"/Users/zenn/program/research_library_exploration/mcp/run.sh","args":[]}'
```

也可直接（需已 `pip install -e ".[mcp]"` 到当前环境）：

```bash
research-library-mcp
```
