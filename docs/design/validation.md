# 信号验证技术规范

> 本文档描述三个验证指标的数据来源、计算方法和触发条件。
> 与 PRODUCT.md 对应：PRODUCT.md 说"验证什么"，本文档说"怎么算"。

---

## 数据来源

所有计算从 `docs/data/portfolio.json` 读取，关键字段：

```
portfolio.json
├── positions[]
│   ├── theme_id, theme_name
│   ├── conviction_at_entry          # 开仓时的 conviction 分数
│   ├── entry_date / exit_date
│   ├── tickers[]
│   ├── entry_prices{ticker: price}
│   ├── exit_prices{ticker: price}
│   ├── realized_return              # 平均持仓收益率
│   ├── spy_return_during_holding    # 同期 SPY 收益率
│   ├── excess_return                # realized_return - spy_return
│   └── daily_snapshots[]
│       ├── date
│       └── floating_return
│
└── theme_history{theme_id: []}
    └── []{date, conviction, direction, stage, tickers[]}
```

---

## 验证一：Daily IC（短期，第 4–8 周开始有意义）

### 目标

判断 conviction 分数是否有方向性预测力。

### 计算逻辑

对每一天 `t`，取当天所有主题的 conviction 分数，与其 **20 个交易日后**（约 4 周）的方向调整收益做 Spearman 秩相关：

```
IC_t = SpearmanCorr(
    [conviction of theme_i on day t],
    [direction_adjusted_return of theme_i from t to t+20]
)
```

`direction_adjusted_return`：
- Bullish 主题：持有期收益（tickers 等权平均）
- Bearish 主题：持有期收益取反（预测下跌，实际涨了是负的）
- 用 `theme_history` 里的 `tickers` + `daily_snapshots` 推算

IC 时间序列的解读：
- 持续为正 → 信号有方向性
- 随机波动 → 无预测力
- 持续为负 → LLM 系统性反向偏差（需检查 conviction 打分逻辑）

### 实现代码

```python
import json
import numpy as np
from scipy.stats import spearmanr
from pathlib import Path
from datetime import date, timedelta

def compute_daily_ic(portfolio_path: str, forward_days: int = 20) -> list[dict]:
    data = json.loads(Path(portfolio_path).read_text())
    theme_history = data.get("theme_history", {})

    # Build price lookup: {theme_id: {date_str: floating_return}}
    price_lookup: dict[str, dict[str, float]] = {}
    for pos in data["positions"]:
        tid = pos["theme_id"]
        price_lookup[tid] = {}
        for snap in pos.get("daily_snapshots", []):
            price_lookup[tid][snap["date"]] = snap["floating_return"]

    # Collect all observation dates
    all_dates = sorted({
        h["date"]
        for records in theme_history.values()
        for h in records
    })

    results = []
    for t_str in all_dates:
        t = date.fromisoformat(t_str)
        t_fwd_str = str(t + timedelta(days=forward_days))

        convictions, returns = [], []
        for theme_id, records in theme_history.items():
            rec_t   = next((r for r in records if r["date"] == t_str), None)
            rec_fwd = next((r for r in records if r["date"] == t_fwd_str), None)

            if rec_t is None or rec_fwd is None:
                continue

            fwd_return = price_lookup.get(theme_id, {}).get(t_fwd_str)
            if fwd_return is None:
                continue

            direction_sign = 1 if rec_t["direction"] == "Bullish" else -1
            convictions.append(rec_t["conviction"])
            returns.append(fwd_return * direction_sign)

        if len(convictions) >= 3:  # 至少 3 个样本才有意义
            ic, _ = spearmanr(convictions, returns)
            results.append({"date": t_str, "ic": round(ic, 4), "n": len(convictions)})

    return results


def ic_summary(ic_series: list[dict]) -> dict:
    values = [r["ic"] for r in ic_series]
    if not values:
        return {}
    mean_ic = np.mean(values)
    std_ic  = np.std(values)
    return {
        "mean_ic":   round(mean_ic, 4),
        "std_ic":    round(std_ic, 4),
        "ic_ir":     round(mean_ic / std_ic, 4) if std_ic > 0 else None,
        "pct_positive": round(sum(1 for v in values if v > 0) / len(values), 3),
        "n_observations": len(values),
    }
```

### 数据要求

| 条件 | 原因 |
|------|------|
| 至少运行 8 周（≈ forward_days + 2 周缓冲）才有第一批 IC | 需要等 forward return 兑现 |
| 每天截面样本 ≥ 3 个主题 | 秩相关在 n < 3 时无意义 |
| 建议观察 15+ 个 IC 数据点再下结论 | 单次 IC 噪音极大 |

---

## 验证二：Conviction 分桶分析（中期，15 个 closed positions 后）

### 目标

验证 conviction 分数是否有**校准价值**：高分组的表现应系统性优于低分组。

### 计算逻辑

```
将所有 closed positions 按 conviction_at_entry 分组：
  Bucket 7:  conviction == 7
  Bucket 8:  conviction == 8
  Bucket 9:  conviction == 9
  Bucket 10: conviction == 10

每个 bucket 计算：
  - count：样本数
  - avg_excess_return：平均超额收益
  - win_rate：excess_return > 0 的比例
  - avg_holding_days：平均持仓天数
```

预期结果：avg_excess_return 和 win_rate 随 bucket 单调递增。如果各组无差异，说明评分没有校准价值。

### 实现代码

```python
from collections import defaultdict
from datetime import date

def conviction_bucket_analysis(portfolio_path: str) -> list[dict]:
    data = json.loads(Path(portfolio_path).read_text())

    buckets: dict[int, list[dict]] = defaultdict(list)
    for pos in data["positions"]:
        if pos["status"] != "closed" or pos.get("excess_return") is None:
            continue
        conv = pos["conviction_at_entry"]
        entry = date.fromisoformat(pos["entry_date"])
        exit_ = date.fromisoformat(pos["exit_date"])
        buckets[conv].append({
            "excess_return":  pos["excess_return"],
            "holding_days":   (exit_ - entry).days,
        })

    results = []
    for conv in sorted(buckets):
        records = buckets[conv]
        excess  = [r["excess_return"] for r in records]
        results.append({
            "conviction":       conv,
            "count":            len(records),
            "avg_excess_return": round(np.mean(excess), 4),
            "win_rate":         round(sum(1 for e in excess if e > 0) / len(excess), 3),
            "avg_holding_days": round(np.mean([r["holding_days"] for r in records]), 1),
        })

    return results
```

### 解读示例

```
conviction | count | avg_excess | win_rate
-----------+-------+------------+---------
7          |  12   |  +0.8%     |  50%
8          |   8   |  +2.1%     |  62%
9          |   4   |  +3.5%     |  75%
```
→ 校准有效：高分组超额收益和胜率均高于低分组。

```
conviction | count | avg_excess | win_rate
-----------+-------+------------+---------
7          |  10   |  +1.2%     |  55%
8          |   8   |  +1.1%     |  54%
9          |   5   |  +1.0%     |  53%
```
→ 校准失效：各组无显著差异，conviction 打分没有区分力。

### 数据要求

| 条件 | 原因 |
|------|------|
| 至少 15 个 closed positions | 分桶后每桶 < 3 个样本无法判断 |
| 建议 30+ 个才能下结论 | 避免幸存者偏差和小样本噪音 |

---

## 验证三：IC-IR（长期，30+ 个 IC 观测值后）

### 目标

综合衡量信号质量：不仅要 IC 均值为正，还要稳定（方差小）。

### 计算公式

```
IC-IR = mean(IC) / std(IC)
```

| IC-IR 值 | 解读 |
|----------|------|
| > 0.5    | 信号有实用价值，可作为决策输入 |
| 0.3–0.5  | 信号微弱，需结合其他因素 |
| < 0.3    | 信号质量不足，不宜依赖 |

IC-IR 已内嵌在上面 `ic_summary()` 的返回值中。

### 数据要求

需要 30–50 个 IC 观测值（约 10–15 个月运行后），才有足够的统计意义区分"信号有效"和"随机运气"。

---

## 触发时机汇总

| 指标 | 最早可计算 | 可下结论 |
|------|-----------|---------|
| Daily IC（单次） | 第 8 周 | — |
| IC 趋势判断 | 第 10 周（15 个观测值） | — |
| IC-IR | 第 10 个月（30 个观测值） | 第 12 个月 |
| Conviction 分桶 | 第 3 个月（15 个 positions） | 第 6 个月（30 个） |
| 超额胜率 | 第 3 个月（15 个 positions） | 第 6 个月（30 个） |

---

## 运行方式

```python
# 在 repo 根目录运行
python -c "
from docs.design import validation  # or just run inline
import json

ic_series = compute_daily_ic('docs/data/portfolio.json')
print('IC Summary:', ic_summary(ic_series))

buckets = conviction_bucket_analysis('docs/data/portfolio.json')
for b in buckets:
    print(b)
"
```

或直接将上述代码放入 `analysis/validate.py`，在数据积累到门槛后定期运行。
