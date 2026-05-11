# Theme Discovery Dashboard — Implementation Design

## Version
v0.1

---

# 1. 系统架构总览

## 整体模式

每日批处理流水线 + 静态 Web Dashboard 输出。

```text
[定时触发 — 每日收盘后]
        ↓
[数据采集层] ── RSS / EDGAR / Yahoo Finance / 财报文本
        ↓
[主题提取引擎] ── LLM 聚类 + 命名
        ↓
[分析引擎] ── Stage 分类 / Conviction 评分 / 研究压缩
        ↓
[数据存储] ── SQLite 本地数据库
        ↓
[Dashboard 渲染] ── 生成静态 HTML 或 Next.js 前端
        ↓
[推送通知] ── Telegram Bot 摘要
```

## 核心原则

- 单机本地运行，无需云基础设施
- 所有状态存 SQLite，无需运维
- LLM 调用集中在分析层，控制成本
- 前端与后端解耦：后端输出 JSON，前端消费 JSON

---

# 2. 技术栈选型

| 层级 | 技术 | 理由 |
|---|---|---|
| 语言 | Python 3.11+ | 数据处理生态最完善 |
| LLM | Claude API (claude-haiku-4-5 / claude-sonnet-4-6) | haiku 处理批量提取，sonnet 处理深度分析 |
| 数据库 | SQLite + SQLModel | 零运维，本地持久化，ORM 简化查询 |
| 任务调度 | cron (macOS launchd) 或 GitHub Actions | 本地用 cron，托管用 Actions |
| 前端 | Next.js 14 (App Router) + Tailwind CSS | 组件化，易于迭代，静态导出可行 |
| 爬虫/抓取 | httpx + BeautifulSoup + feedparser | 轻量，无需 Selenium |
| 股票数据 | yfinance | 免费，覆盖价格/成交量 |
| SEC 数据 | sec-edgar-downloader | 官方 EDGAR API 封装 |
| 推送 | Telegram Bot API | 免费，配置简单，手机端友好 |
| 包管理 | uv | 速度快，现代 Python 工具链 |

---

# 3. 模块设计

---

## Module 1 — 数据采集层 (ingestion/)

### 职责
从各数据源抓取原始数据，标准化为统一格式存入数据库。

### 子模块

#### 1.1 RSS 新闻抓取 (ingestion/rss.py)
- 数据源：Reuters Finance、Seeking Alpha、Yahoo Finance News、Barron's RSS
- 工具：`feedparser`
- 输出：`RawArticle(title, url, content_snippet, source, published_at)`
- 频率：每日一次，拉取最近 24 小时

#### 1.2 SEC EDGAR 抓取 (ingestion/edgar.py)
- 数据源：EDGAR 全文搜索 API
- 关注文件类型：8-K（重大事件）、earnings release
- 工具：`sec-edgar-downloader` + `httpx`
- 输出：`RawFiling(ticker, form_type, filed_at, text_content)`
- 频率：每日一次

#### 1.3 Yahoo Finance 价格数据 (ingestion/price.py)
- 数据：收盘价、成交量、52 周高低
- 关注标的：主题相关 Ticker 列表（从上一轮分析结果动态维护）
- 工具：`yfinance`
- 输出：`PriceSnapshot(ticker, date, close, volume, volume_ratio_20d)`

#### 1.4 财报文本抓取 (ingestion/transcripts.py)
- 数据源：The Motley Fool earnings transcripts（免费，有延迟）
- 工具：`httpx` + `BeautifulSoup`
- 输出：`EarningsTranscript(ticker, quarter, text_content, published_at)`
- 备注：延迟 1-2 天，MVP 可接受

### 数据标准化输出格式

```python
@dataclass
class RawSignal:
    source_type: str        # "news" | "filing" | "transcript" | "price"
    source_name: str
    ticker: str | None
    headline: str
    content: str
    signal_date: date
    url: str | None
```

---

## Module 2 — 主题提取引擎 (theme_engine/)

### 职责
将原始信号聚类成主题，命名并去重。

### 流程

```text
RawSignal 列表 (当日)
    ↓
[批量摘要] claude-haiku — 每条信号提取关键概念 (3-5个词)
    ↓
[聚类] claude-sonnet — 将相似概念合并为主题，输出主题名 + 归属信号
    ↓
[去重/合并] 与历史主题对比，判断是新主题还是已有主题的延续
    ↓
Theme 列表写入数据库
```

### LLM 使用策略

| 任务 | 模型 | 理由 |
|---|---|---|
| 单条信号关键词提取 | claude-haiku-4-5 | 批量便宜，任务简单 |
| 跨信号主题聚类 | claude-sonnet-4-6 | 需要推理能力 |
| 主题与历史比对 | claude-haiku-4-5 | 结构化匹配任务 |

### Claude API 调用示例（主题聚类）

```python
# 输入：当日所有信号的关键词列表
# 输出：结构化主题列表（JSON）

CLUSTER_PROMPT = """
You are a market analyst. Given the following list of market signals from today,
cluster them into 5-10 coherent market themes.

For each theme output:
- theme_name: concise label (2-4 words)
- signal_ids: list of signal IDs belonging to this theme
- one_line_summary: what is happening in this theme today

Signals:
{signals_json}

Output valid JSON only.
"""
```

### 输出数据结构

```python
@dataclass
class Theme:
    id: str
    name: str
    first_seen: date
    last_active: date
    representative_tickers: list[str]
    signal_count_today: int
```

---

## Module 3 — 分析引擎 (analysis/)

### 职责
对每个活跃主题进行 Stage 分类、Conviction 评分、研究压缩。

### 3.1 Stage 分类 (analysis/stage.py)

LLM 根据该主题近 30 天的信号密度、信号类型分布、价格行为，判断阶段。

```text
输入：主题近 30 天信号列表 + 代表标的价格走势
输出：
  - stage: "Early Discovery" | "Acceleration" | "Momentum Expansion" | "Crowded Euphoria" | "Narrative Breakdown"
  - stage_reasoning: 1-2 句话解释
  - stage_confidence: 1-10
```

### 3.2 Conviction 评分 (analysis/conviction.py)

```text
输入：主题所有信号的 Bull/Bear 分类 + 信号来源权重
输出：
  - direction: "Bullish" | "Bearish" | "Neutral"
  - conviction_score: 1-10
  - bull_evidence: [最强的 3 条 bull 信号]
  - bear_evidence: [最强的 3 条 bear 信号]
  - conviction_basis: 解释分数高低的 1-2 句话
```

#### 信号来源权重（初始默认值，未来可调整）

| 来源 | 权重 |
|---|---|
| 财报文本（management guidance） | 1.0 |
| SEC 8-K 重大事件 | 0.9 |
| 主流财经媒体 | 0.6 |
| RSS 一般新闻 | 0.4 |
| 价格异动 | 0.5 |

### 3.3 研究压缩 (analysis/compression.py)

为每个主题生成结构化摘要。

```text
输出：
  - bull_case: 2-3 句
  - bear_case: 2-3 句
  - current_drivers: 当前主要驱动因素
  - key_risks: 3 条风险
  - short_term_outlook: 1-4 周
  - mid_term_outlook: 1-6 月
  - long_term_outlook: 1-3 年
```

模型：claude-sonnet-4-6（需要较强的综合推理）

---

## Module 4 — 数据存储 (db/)

### 数据库：SQLite + SQLModel

本地文件 `data/theme_dashboard.db`，无需服务器。

### 核心表结构

```sql
-- 原始信号
raw_signals (id, source_type, source_name, ticker, headline, content, signal_date, url, created_at)

-- 主题
themes (id, name, first_seen, last_active, representative_tickers_json, is_active)

-- 主题每日快照（每天一条）
theme_daily_snapshots (
    id, theme_id, snapshot_date,
    direction, conviction_score,
    stage, stage_confidence,
    bull_evidence_json, bear_evidence_json,
    bull_case, bear_case, current_drivers, key_risks,
    short_term_outlook, mid_term_outlook, long_term_outlook,
    signal_count, crowdedness_score
)

-- 信号-主题关联
signal_theme_map (signal_id, theme_id, relevance_score)
```

### 数据保留策略

- `raw_signals`：保留 90 天
- `theme_daily_snapshots`：永久保留（这是未来 feedback loop 的基础）
- `themes`：永久保留

---

## Module 5 — 前端界面 (frontend/)

### 技术选型：Next.js 14 + Tailwind CSS

### 数据获取方式

后端每日批处理结束后，生成 `public/data/latest.json`，前端直接读取静态 JSON。

可选：加一个轻量 FastAPI 提供 `/api/themes` 接口，前端实时查询。

### 页面结构

#### 主页 (/)

```
┌─────────────────────────────────────────────────────┐
│  Theme Discovery Dashboard    Last updated: May-06   │
├──────────────────┬──────────────────────────────────┤
│ 🔥 High          │  Theme Card × 3                  │
│ Conviction       │  [AI Infrastructure  Bullish 8/10]│
│                  │  [Nuclear Energy     Bullish 7/10]│
│                  │  [Robotics           Neutral 5/10]│
├──────────────────┼──────────────────────────────────┤
│ 🌱 Emerging      │  Theme Card × 2                  │
│                  │  [Space Economy  Early  6/10]     │
├──────────────────┼──────────────────────────────────┤
│ ⚠️  Crowded /    │  Theme Card × 1                  │
│    Fading        │  [Meme Stocks   Breakdown  3/10] │
└──────────────────┴──────────────────────────────────┘
```

#### 主题详情页 (/theme/[id])

```
┌─────────────────────────────────────────────────────┐
│  AI Infrastructure          Bullish ↑   8/10        │
│  Stage: Acceleration        Crowdedness: Medium      │
├─────────────────────────────────────────────────────┤
│  Bull Case          │  Bear Case                    │
│  ─────────────────  │  ────────────────────────     │
│  · MSFT capex ↑     │  · Valuation stretched        │
│  · NVDA beat +15%   │  · Supply chain normalizing   │
├─────────────────────────────────────────────────────┤
│  Timeline                                            │
│  May-02 ── MSFT earnings mention AI demand           │
│  May-04 ── HBM shortage discussion increases         │
│  May-05 ── Multiple analyst upgrades                 │
├─────────────────────────────────────────────────────┤
│  Outlook                                             │
│  Short-term │ Mid-term │ Long-term                   │
├─────────────────────────────────────────────────────┤
│  Representative Tickers: NVDA  AMD  AVGO             │
└─────────────────────────────────────────────────────┘
```

### 关键 UI 组件

| 组件 | 描述 |
|---|---|
| `ThemeCard` | 主列表卡片，显示方向/分数/Stage |
| `ConvictionBadge` | 1-10 分视觉化（进度条/颜色编码） |
| `StageTag` | Stage 标签，每个阶段对应颜色 |
| `EvidenceList` | Bull/Bear 证据列表 |
| `NarrativeTimeline` | 时间轴组件 |
| `OutlookPanel` | 三段式展望面板 |

---

## Module 6 — 推送通知 (notification/)

### 技术：Telegram Bot API（免费）

### 每日推送内容

```text
📊 Theme Dashboard — May 06

🔥 High Conviction
• AI Infrastructure  Bullish 8/10  (Acceleration)
• Nuclear Energy     Bullish 7/10  (Acceleration)

🌱 Emerging
• Space Economy      Bullish 6/10  (Early Discovery)

⚠️  Watch
• Meme Stocks        Bearish 3/10  (Breakdown)

View full dashboard → http://localhost:3000
```

### 配置

`.env` 文件存储：
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

---

# 4. AI 工具使用方案

## 构建阶段：使用 Claude Code

用 Claude Code（即当前工具）逐模块生成代码：

| 任务 | 如何用 Claude Code |
|---|---|
| 搭建项目脚手架 | 生成目录结构 + pyproject.toml |
| 编写数据采集模块 | 提供数据源文档，生成爬虫代码 |
| 设计 Claude API 调用 | 生成 prompt 模板 + structured output 解析 |
| 前端组件 | 生成 Next.js 组件 + Tailwind 样式 |
| 数据库 schema | 生成 SQLModel 模型定义 |

## 运行阶段：使用 Claude API

| 任务 | 模型 | 预估成本/天 |
|---|---|---|
| 批量关键词提取（100-200 条信号） | claude-haiku-4-5 | ~$0.05 |
| 主题聚类（1 次/天） | claude-sonnet-4-6 | ~$0.10 |
| Stage 分类（5-10 个主题） | claude-sonnet-4-6 | ~$0.20 |
| Conviction 评分（5-10 个主题） | claude-sonnet-4-6 | ~$0.20 |
| 研究压缩（5-10 个主题） | claude-sonnet-4-6 | ~$0.30 |
| **合计** | | **~$0.85/天 ≈ $25/月** |

---

# 5. 项目目录结构

```
theme-dashboard/
├── ingestion/
│   ├── rss.py
│   ├── edgar.py
│   ├── price.py
│   └── transcripts.py
├── theme_engine/
│   ├── extractor.py       # 关键词提取
│   └── clusterer.py       # 主题聚类
├── analysis/
│   ├── stage.py           # Stage 分类
│   ├── conviction.py      # Conviction 评分
│   └── compression.py     # 研究压缩
├── db/
│   ├── models.py          # SQLModel 表定义
│   └── session.py         # DB 连接管理
├── notification/
│   └── telegram.py
├── frontend/              # Next.js 项目
│   ├── app/
│   │   ├── page.tsx       # 主页
│   │   └── theme/[id]/
│   │       └── page.tsx   # 详情页
│   └── components/
│       ├── ThemeCard.tsx
│       ├── ConvictionBadge.tsx
│       ├── StageTag.tsx
│       └── NarrativeTimeline.tsx
├── pipeline.py            # 主流水线入口
├── pyproject.toml
└── .env
```

---

# 6. 开发顺序建议

按依赖关系和验证价值排序：

### Phase 1 — 数据能跑通（1 周）
1. 搭项目骨架（目录、pyproject.toml、SQLite 模型）
2. 实现 RSS 抓取 → 存入 raw_signals
3. 实现 Yahoo Finance 价格抓取
4. 实现 EDGAR 8-K 抓取

**验收标准**：每天能自动跑，数据库里有数据。

### Phase 2 — LLM 分析跑通（1 周）
5. 实现主题聚类（Claude API）
6. 实现 Conviction 评分
7. 实现 Stage 分类
8. 实现研究压缩

**验收标准**：每天能生成 `latest.json`，内容结构完整。

### Phase 3 — 界面和推送（1 周）
9. 实现 Next.js 主页（主题列表）
10. 实现主题详情页
11. 实现 Telegram 推送

**验收标准**：每天收到 Telegram 摘要，能打开网页看详情。

### Phase 4 — 整合和调优（持续）
12. 接入财报文本数据源
13. 调整 prompt 提升准确性
14. 添加 conviction score 历史趋势图

---

# 7. 关键技术决策说明

## 为什么用 SQLite 而不是 PostgreSQL？
个人工具，单机运行，零运维。未来迁移到 PostgreSQL 的成本低（SQLModel 支持切换）。

## 为什么前端用 Next.js 而不是纯静态 HTML？
主题详情页需要动态路由，数据量大后需要分页。Next.js 静态导出模式可在无服务器时使用。

## 为什么推送用 Telegram 而不是邮件？
Telegram Bot 配置 10 分钟内完成，手机端体验好，支持 Markdown 格式，免费无限量。

## Haiku vs Sonnet 的分工
批量、简单任务（提取关键词、信号分类）用 haiku 控制成本；需要推理综合的任务（聚类、分析）用 sonnet 保证质量。

---

# 8. v0.2+ 扩展方向

| 功能 | 技术方案 |
|---|---|
| Options flow 数据 | Unusual Whales API 或 Tradier API |
| Reddit 信号 | PRAW（Reddit API，需申请 app） |
| X/Twitter 信号 | X API Basic（$100/月）|
| Feedback loop | 定时任务对比历史 snapshot 与实际价格表现 |
| 移动端适配 | Tailwind responsive 已覆盖 |
| 多语言支持 | i18n（中文界面） |
