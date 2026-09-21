# 科研伴侣验收记录

## Phase 0–6 已通过

| 维度 | 结论 |
|------|------|
| 后端 API | `tests/test_server_api.py` 15/15 通过 |
| Zotero 同步 | 22 项测试通过（bibcode pull、anti-downgrade、backfill CLI） |
| 桌面壳 | Tauri `.app` 启动后端，退出时清理进程 |
| 深入调查 | semantic_report / topic_dossier + `[S1]` 来源 |
| 论文笔记 | `papers.notes` 可读写 |
| 真实库冒烟 | ~1606 篇论文，health/search/graph/ask 可用 |

## 优化前缺口（本次迭代目标）

| 问题 | 优化项 |
|------|--------|
| API 配置需改 `.env` | 设置页 + `app_settings.json` |
| 默认首页为图书馆 | `/` → 深入调查 |
| 论文 Q&A 单轮且在详情抽屉 | PDF 侧边多轮对话 + 链式搜索 |
| 引文图谱卡顿 | 服务端裁剪 + 前端布局优化 |

## 测试备注

- 全量 97/99 通过；`test_semantic_live.py` 2 项失败为 MiniMax embedding 套餐限制
- ADS 429 时 backfill 熔断；约 53 条 Unknown 待配额恢复
