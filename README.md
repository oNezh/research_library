# research-library

本地天文学论文库：**ADS / arXiv 检索**、**PDF 入库**、**引用图**、**TeX 优先的语义索引**（ar5iv → 本地 TeX → PDF 兜底），以及 **stdio MCP** 供 Cursor / Claude Desktop / OpenClaw 调用。

数据与代码分离：仓库只含 Python 包；索引、PDF、向量库默认在 `~/program-data/research_library`（可用环境变量改路径）。

---

## 安装

**Python ≥ 3.10**

```bash
git clone https://github.com/oNezh/research_library.git
cd research_library

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"    # 含 pdf、mcp、semantic、tex-source、tex-local、pytest
```

按需安装可选能力：

| Extra | 用途 |
|-------|------|
| `pdf` | PyMuPDF 抽表/图 |
| `mcp` | MCP 服务器 |
| `semantic` | Chroma 向量索引 |
| `semantic-local` | 本地 SentenceTransformers（如 Qwen3-Embedding） |
| `tex-source` | ar5iv HTML 清洗（beautifulsoup4） |
| `tex-local` | arXiv tarball + pylatexenc 本地 TeX 清洗 |

最小语义检索（API 嵌入）：

```bash
pip install -e ".[semantic,tex-source,tex-local,mcp]"
```

---

## 配置

```bash
cp .env.example .env
```

**必填（ADS 检索、BibTeX、引用同步、PDF 网关下载）：**

```bash
ADS_API_TOKEN=your_token   # https://ui.adsabs.harvard.edu/user/settings/token
```

**常用可选项：**

```bash
# 数据根目录（默认 ~/program-data/research_library）
RESEARCH_LIBRARY_DATA_DIR=/path/to/data

# 语义检索：vector（Chroma）| fts（仅 SQLite FTS，无需嵌入模型）
RESEARCH_SEMANTIC_BACKEND=vector

# 本地 Qwen 嵌入（示例）
RESEARCH_EMBEDDING_PROVIDER=local_sentence_transformer
RESEARCH_LOCAL_EMBEDDING_HOME=/path/to/qwen
RESEARCH_LOCAL_EMBEDDING_HF_OFFLINE=1

# PDF 入库后自动做 semantic-index（TeX→PDF fallback）
RESEARCH_INGEST_AUTO_SEMANTIC_INDEX=1

# LLM（pdf-analyze、semantic-report 等）
MINIMAX_API_KEY=...
MINIMAX_MODEL=MiniMax-M2.7-highspeed
```

完整变量说明见 [.env.example](.env.example)。

程序会加载**仓库根目录**下的 `.env`（已在 `.gitignore` 中，勿提交 token）。

---

## 数据目录结构

```
$RESEARCH_LIBRARY_DATA_DIR/          # 默认 ~/program-data/research_library
├── index/
│   ├── library.db          # SQLite：papers、paper_chunks、paper_references、FTS
│   └── chroma_semantic/    # Chroma 向量（RESEARCH_SEMANTIC_BACKEND=vector）
├── pdfs/                   # 入库 PDF（按 bibcode 命名）
├── sources/<paper_id>/     # TeX 清洗文本 main.txt + sections.json
├── arxiv_sources/          # arXiv e-print 缓存（pylatexenc 用）
└── arxiv_cache.json        # arxiv-keywords 轮询缓存
```

---

## 快速上手

```bash
# 1. 把 PDF 入库（自动 ADS 匹配、复制到 pdfs/、同步引用、可选自动 embedding）
research-lib library ingest-pdf /path/to/paper.pdf

# 2. 元数据检索
research-lib library search "GD-1 stream metallicity"

# 3. 语义检索（需 semantic 后端 + 嵌入配置）
research-lib library semantic-search "alpha-knee globular cluster" --limit 5

# 4. 带引用的 LLM 报告
research-lib library semantic-report "LMC star formation history" --json
```

---

## CLI 总览

入口：`research-lib <command> …`

| 一级命令 | 说明 |
|----------|------|
| `lookup` | ADS/arXiv 检索、BibTeX、下载、pdf2ads |
| `library` | 本地 SQLite 文献库（见下节） |
| `pdf-analyze` | PDF + LLM 摘要 / 按问题摘录 / **引用链** |
| `pdf-extract` | PDF 表/图抽取（需 `[pdf]`） |
| `arxiv-keywords` | arXiv 关键词轮询 + 可选写入库 |
| `ads-data-products` | ADS 数据产品 |
| `refs-classify` | 参考文献 + 引用上下文分类 |

---

## 本地文献库 `research-lib library …`

### 库管理与元数据检索

```bash
research-lib library init              # 建表（首次写入也会自动建）
research-lib library stats             # 论文数、库路径
research-lib library search "query" --limit 20
research-lib library find "query"      # 先搜本地，再 ADS/arXiv
research-lib library import-cache      # 从 arxiv_cache.json 批量导入
research-lib library dedupe --dry-run  # 查重复 bibcode/arXiv/pdf
```

### PDF 入库与更新

**从 PDF 文件入库**（抽 DOI/arXiv/标题 → ADS 匹配 → 写 `papers` + `pdf_relpath`）：

```bash
research-lib library ingest-pdf paper.pdf

# 自动匹配失败时手动补 metadata
research-lib library ingest-pdf paper.pdf --doi 10.1234/xxx
research-lib library ingest-pdf paper.pdf --arxiv 2401.12345
research-lib library ingest-pdf paper.pdf --match-title "Exact paper title"
research-lib library ingest-pdf paper.pdf --bibcode 2024ApJ...L

# 只解析不入库
research-lib library ingest-pdf paper.pdf --dry-run --json
```

**从参考文献行入库并下载 PDF：**

```bash
research-lib library ingest-ref --text "Smith et al. 2020, ApJ, 123, 45"
research-lib library ingest-ref --text "2020ApJ...S" --skip-download
```

**更新 PDF**（优先期刊版：`pub > ads > eprint > arxiv`）：

```bash
research-lib library update-pdf --paper-id 365
research-lib library update-pdf --all --source auto --reindex
```

**批量 Zotero / 文件夹入库：**

```bash
python scripts/batch_ingest_pdfs.py "/path/to/pdfs" 2>&1 | tee batch.log
python scripts/batch_ingest_pdfs.py "/path/to/pdfs" --no-semantic   # 只入库不 embed
```

### TeX 源与 Embedding

Embedding **默认走 TeX 优先**：`ar5iv` → `pylatexenc`（本地 tarball）→ **PDF 兜底**。

```bash
# 拉 TeX 清洗文本（写 sources/<id>/main.txt）
research-lib library fetch-source --paper-id 365 --force
research-lib library fetch-source --all --force

# 切块 + 嵌入（无 TeX 时会自动 fetch；仍失败则用 PDF）
research-lib library semantic-index --force
research-lib library semantic-index --paper-id 365 --force
research-lib library semantic-index --only-missing   # 只补未索引的

# 一键：fetch + re-embed
research-lib library reembed-from-source --all --force

# 语义检索
research-lib library semantic-search "Sagittarius GD-1 cocoon" --limit 10 --json
```

相关环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `RESEARCH_SEMANTIC_AUTO_FETCH_TEX` | `1` | index 前自动拉 TeX |
| `RESEARCH_TEX_LOCAL_BACKEND` | `pylatexenc` | 第二步本地 TeX 后端 |
| `RESEARCH_INGEST_AUTO_SEMANTIC_INDEX` | `1` | ingest-pdf 后自动 index |
| `RESEARCH_SEMANTIC_CHUNK_SIZE` | `1200` | 切块大小 |
| `RESEARCH_SEMANTIC_CHUNK_OVERLAP` | `200` | 块重叠 |

### 引用图

```bash
# 从 ADS 同步每篇论文的 references → paper_references
research-lib library citation sync
research-lib library citation sync --missing-only

# 导出引用图 / 找缺失 hub
research-lib library citation graph --min-hub 2

# 把缺失 hub 的 bibcode 从 ADS 灌入库
research-lib library citation ingest-hubs -
```

### BibTeX 导出

```bash
# 文件：每行 bibcode / arXiv / DOI / 参考文献行
research-lib library bib-export refs.txt

# stdin
echo "2020ApJ...S" | research-lib library bib-export -
```

### LLM 综合报告

```bash
# 多 query _gather + LLM 合成
research-lib library topic-dossier "LMC metallicity gradient"

# 语义片段 + paper_references + LLM，输出带 [S1] 来源标记
research-lib library semantic-report "How does Sgr affect GD-1?" --expand-queries
research-lib library semantic-report "..." --no-synth --json   # 只要结构化上下文
```

---

## ADS / arXiv 检索 `research-lib lookup …`

子命令与独立 `paper_lookup` 兼容：

```bash
research-lib lookup ref --text "Balbinot 2022" --json
research-lib lookup title --text "GD-1 stream" --json
research-lib lookup query --text "globular cluster kinematics" --json
research-lib lookup bibtex --bibcode 2024ApJ...L
research-lib lookup download --bibcode 2024ApJ...L --library    # 存到 data/pdfs/
research-lib lookup download --arxiv 2401.12345 --library
research-lib lookup pdf2ads /path/to/paper.pdf --json
```

---

## PDF 分析 `research-lib pdf-analyze`

**单篇摘要 / 按问题摘录：**

```bash
research-lib pdf-analyze paper.pdf
research-lib pdf-analyze paper.pdf -q "What tracer did they use for metallicity?"
research-lib pdf-analyze paper.pdf --json
```

**多跳引用链**（根 PDF 在库中且已有 `paper_references` 时，优先用 ADS 引用图，而非解析 PDF 文末参考文献）：

```bash
research-lib pdf-analyze seed.pdf --reference-chain \
  -q "Your research question" \
  --max-hops 2

# 常用环境变量见 .env.example：
# RESEARCH_PDF_CHAIN_LIBRARY_REFS=1
# RESEARCH_PDF_CHAIN_MAX_FOLLOW_PER_HOP=0
# RESEARCH_PDF_CHAIN_AUTO_INGEST=1
```

---

## arXiv 监控 `research-lib arxiv-keywords`

```bash
research-lib arxiv-keywords astro-ph.GA --days=7
research-lib arxiv-keywords --stats
research-lib arxiv-keywords --clear-cache
```

默认把匹配论文写入 `arxiv_cache.json`，并 **upsert** 到 `library.db`（`--no-persist-db` 可只写缓存）。

---

## MCP 服务器

### 启动

```bash
# 方式 A：安装后的入口
research-library-mcp

# 方式 B：仓库内 wrapper（优先 .venv）
./mcp/run.sh
```

stdio 协议，无 HTTP 端口。

### 注册到 Cursor

1. 打开 **Cursor Settings → MCP**（或编辑 `~/.cursor/mcp.json`）
2. 添加：

```json
{
  "mcpServers": {
    "research-library": {
      "command": "/absolute/path/to/research_library/mcp/run.sh",
      "args": []
    }
  }
}
```

若不用 wrapper，可指定 venv Python：

```json
{
  "mcpServers": {
    "research-library": {
      "command": "/absolute/path/to/research_library/.venv/bin/python",
      "args": ["-m", "research_library.mcp_server"],
      "cwd": "/absolute/path/to/research_library"
    }
  }
}
```

确保 `cwd` 下存在 `.env`，或已在系统环境中设置 `ADS_API_TOKEN` 等变量。

### 注册到 Claude Desktop

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "research-library": {
      "command": "/absolute/path/to/research_library/mcp/run.sh",
      "args": []
    }
  }
}
```

重启 Claude Desktop 后生效。

### 注册到 OpenClaw

```bash
openclaw mcp set research_library \
  '{"command":"/absolute/path/to/research_library/mcp/run.sh","args":[]}'
```

### MCP 工具一览

**检索 / 下载**

| 工具 | 说明 |
|------|------|
| `lookup_ref` | 参考文献行 → ADS 解析 |
| `lookup_title` | 标题检索 |
| `lookup_query` | 自由文本 ADS + arXiv |
| `fetch_bibtex` | 按 bibcode 或 arXiv 取 BibTeX |
| `download_pdf` | 下载 PDF（`use_library=true` 存 data/pdfs/） |
| `pdf_to_ads` | 本地 PDF → ADS 元数据 |

**本地库**

| 工具 | 说明 |
|------|------|
| `library_search` | FTS 检索 title/abstract |
| `library_stats` | 库统计 |
| `library_import_cache` | 导入 arxiv_cache.json |
| `library_ingest_pdf` | PDF 入库（支持 `manual_doi` / `manual_arxiv` / `manual_bibcode` 等） |
| `library_bib_export` | 参考文献列表 → BibTeX + 可选 ingest |
| `library_fetch_source` | 拉 ar5iv / 本地 TeX 文本 |
| `library_update_pdf` | 更新 PDF（期刊版优先） |

**语义 / 报告**

| 工具 | 说明 |
|------|------|
| `library_semantic_status` | chunk 数、当前 backend |
| `library_semantic_index` | 建/重建索引（`force`） |
| `library_semantic_search` | 块级语义检索 |
| `library_get_related_papers` | 相关论文 |
| `library_compare_topic` | 多论文主题对比 |
| `library_topic_dossier` | 主题 dossier |
| `library_semantic_report` | 片段 + 引用 + LLM 报告（`[S1]` 标签） |

**引用图 / 引用链**

| 工具 | 说明 |
|------|------|
| `library_citation_sync` | ADS → paper_references |
| `library_citation_graph` | 引用图 / mindmap |
| `library_citation_ingest_hubs` | 补全缺失 hub |
| `pdf_analyze` | PDF LLM 分析（`reference_chain=true` 开引用链） |
| `pdf_reference_chain` | 专用引用链（需 `question`） |

**其他**

| 工具 | 说明 |
|------|------|
| `pdf_extract_tables_or_images` | 表/图抽取 |
| `references_classify` | 引用分类 + 上下文 |
| `ads_data_products` | ADS 数据产品 |
| `arxiv_keyword_scan` | arXiv 关键词扫描 |

`semantic_backend` 参数：`""`（读环境变量）、`fts`、`vector`。

---

## 典型工作流

### 新建个人论文库

```bash
cp .env.example .env   # 填 ADS_API_TOKEN、嵌入、LLM

research-lib library ingest-pdf ~/Downloads/*.pdf
research-lib library citation sync
research-lib library semantic-index --only-missing
```

### 从 arXiv 每日更新扩库（无 PDF）

```bash
research-lib arxiv-keywords astro-ph.GA --days=1
research-lib library fetch-source --all
research-lib library semantic-index --only-missing
```

仅有 metadata、无 PDF 的论文：TeX 成功则可语义搜索；TeX 也失败则无法 index，除非之后 `download_pdf` / `ingest-ref`。

### 单篇升级到期刊 PDF + 重 embed

```bash
research-lib library update-pdf --paper-id 365 --reindex
```

---

## 开发与测试

```bash
pip install -e ".[dev]"
pytest
```

---

## 许可证

见仓库 LICENSE（若未添加，使用前请自行补充）。
