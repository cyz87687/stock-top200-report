# round136 · 11-06 降级项收口 + 盘中污染防护（2026-11-09）

## 一、本轮要解决的问题

2026-11-06（上周五）的日更已产出评分与页面并本地提交（`9375d69`），但遗留
**6 项可降级步骤失败**，`pipeline_status` 判 `degraded`：

| 步骤 | 失败原因（当日日志） |
| --- | --- |
| `append_klines_latest` | 单条后台长任务中断 + `_to 600` 包装未生效，2914s 未完成 |
| `refresh_kline_meta` | 依赖上一步，未执行 |
| `verify_qfq_anchor` | 价格库停在 11-05，无可校验的当日截面 |
| `archive_fund_universe` | 同上（避免陈旧收盘价写入 fund 归档） |
| `portfolio_risk` | `KeyError: 'window'` —— 组合日 11-06 不在有效截面日历 |
| `factor_eval --optimize-weights` | 两次超时（600s / 240s） |

## 二、发现的真问题：盘中行情污染（本轮新增防护）

### 2.1 风险链条

补跑这些步骤需要抓行情，而**当天是周一盘中（10:47）**。实测腾讯 `fqkline`：

```
sh600519  2026-11-06  vol=42848 手   ← 整日
sh600519  2026-11-09  vol=12532 手   ← 盘中 10:50，约 29%
```

接口在盘中就返回当日行，**价格与成交量都是未收盘的瞬时值**。

而本项目所有增量落库都遵循同一条铁律 —— **已有日期绝不覆盖**
（保历史可复现、防前复权因子漂移静默改写面板）。两条叠加：

> 盘中缺陷跑一次 → 半日 K 线被**永久写死** → 收盘后也不会被修正 → 污染不可逆

### 2.2 受影响的脚本不止一个

| 脚本 | 落库位置 | 污染后果 |
| --- | --- | --- |
| `append_klines_latest.py` | `data/klines_long.json` | 价格面板混入半日价；`refresh_kline_meta` 据此判有效截面 |
| `factor_eval.py::refresh_price_tail` | `factor_history.json` 的 `prices` store | **只填空格子、不覆盖旧值** → 半日价固化进 IC 计算的价格轴 |

`factor_eval` 这一条此前**完全没被发现**：它跟日链的 `append_klines_latest` 是
两套独立的行情落库路径，同一个接口、同一种「不覆盖」语义、同样的污染风险。

### 2.3 修法：判据单一实现

新增 `kline_cutoff.py`（纯函数、不抓网、不写盘）：

- `resolve_cutoff(until=None, now=None)` → `(cutoff_iso | None, reason)`
  - 显式 `--until` 优先，**格式非法直接抛错**（禁止静默降级成「不限制」）
  - 未显式给：周一~周五 09:30~15:05 视为交易时段 → 上限 = 前一日
  - 周末 / 盘后：不限制（保持原行为，收盘后跑不受影响）
- `bar_allowed(date_str, cutoff)` → 逐 bar 判定

两个脚本都改为 `import kline_cutoff`，**禁止各写一份判据**（项目已有教训：
日期判据散落 → 口径不一致 → 陈旧数据静默上线）。

刻意**不编节假日表**：「今天是否真开市」由 `check_data_fresh` 的指数日期门禁兜底；
本模块只负责「不把未收盘的当日价写进库」这一件事。

### 2.4 实测验证

小样本 dry-run（30 只）：

```
🔒 日期上限：2026-11-06（显式 --until）→ 晚于该日的 bar 丢弃
完成：29 只被追加，新增 29 个价格点，失败 0 只
🔒 按日期上限丢弃 29 个 bar（日期：['2026-11-09']）
新增日期：['2026-11-06']
```

保护生效：11-06 正常补入，11-09 的 29 个盘中 bar 全部丢弃。

## 三、`portfolio_risk` 的 `KeyError: 'window'` 是**两个 bug**

### 3.1 表层原因（数据）

`portfolio_risk.py:174` —— 组合日不在有效截面日历 → 提前返回：

```python
if d0 not in cidx:
    out['reason'] = f'组合日 {d0} 不在交易日历内（非有效截面）'
    return out
```

当日价格库停在 11-05，有效截面门槛 `max(1000, 2138×0.5)=1069` 只，11-06 未达标
→ 11-06 不在日历 → 走这条提前返回路径。

### 3.2 深层原因（代码缺陷）

回填阶段（`portfolio_risk.py:458`）用 `r[k]` **硬取**：

```python
merged.update({k: r[k] for k in (..., 'window', 'window_start', 'window_end', ...)})
```

但 `compute_one` 的 `out` 初始化**根本没有** `window` 三字段，只有正常计算路径才会
写入。任何早期返回（无持仓 / 缺 date / 不在日历 / 覆盖率不足）都会让回填阶段
`KeyError` 崩溃 —— **整个 `--inplace` 挂掉，risk 块一个字段都没写回**。

修法改为 `r.get(k)`。**这不是把错误藏起来**：缺失字段写 `None` 是真实信息
（= 该口径未计算），下游 `if rk.get("window")` 的假分支本来就是为此设计的；
真正要防的是崩溃导致**全部**字段丢失。

> 教训与 round122 的 `_ev_gap` 完全同型：数据层有 → 回填层丢 → 渲染层静默。
> 但这次更进一步：回填层直接崩，连「静默」的机会都没有。

## 四、待补齐的执行顺序（依赖关系）

```
append_klines_latest --until 2026-11-06   （2138 只，约 9 分钟）
  └→ refresh_kline_meta        （纯本地重算，~2s，让 11-06 进入有效截面日历）
       ├→ portfolio_risk --inplace   （消除 KeyError）
       └→ archive_fund_universe --date 2026-11-06
            └→ verify_qfq_anchor --date 2026-11-06
factor_eval --optimize-weights --until 2026-11-06   （2478 只 × 网络，需长超时）
```

`factor_eval` 的耗时根因：`refresh_price_tail` 对**历史全部出现过的代码**逐只抓
（本次 2478 只，`need=30`），每只 `sleep(0.02)` + 网络往返 ≈ 250s+。逻辑本身不需改，
用长超时后台跑即可。

## 五、执行结果（2026-11-09 11:10 ~ 12:10 全部补齐）

### 5.1 六项降级全部清零

| 步骤 | 11-06 失败原因 | 本轮结果 |
|---|---|---|
| `append_klines_latest` | 腾讯三域名限流，313 只失败，价格库停在 11-05 | ✅ 786 只补入，**新增 2153 点，仅 2 只失败** |
| `refresh_kline_meta` | 依赖上一步 | ✅ 有效截面 **308 → 309**，`date_range` → 11-06 |
| `verify_qfq_anchor` | 依赖上一步 | ✅ 抽查 35 只**全部一致，0 漂移** |
| `archive_fund_universe` | 价格库陈旧，主动跳过 | ✅ 入档 **2111 只** |
| `portfolio_risk` | `KeyError: 'window'` | ✅ 修 `r.get` 后跑通，两路线 `computable=True` |
| `factor_eval` | 执行被中断 | ✅ 价格尾部 **+2 日**，覆盖 3206 只（成功 3204 / 失败 31） |

`pipeline_status --finalize` → **`verdict=ok`**，`degraded: 0`、`not_run: 0`、`failed_critical: 0`。

### 5.2 价格库覆盖率：决定性改善

| 指标 | 补跑前 | 补跑后 |
|---|---|---|
| 11-06 有价只数 | 1348（**63.0%**，低于 1069 门槛之上仍不足） | **2134（99.8%）** |
| 库内日期数 | 345 | 345（最新恒 11-06） |
| 2026-11-09 是否入库 | — | **False（污染拦截有效）** |

首轮失败 786 只几乎全为沪市（`688×309 / 603×217 / 600×147 / 601×84 / 605×29`），
归因**限流而非停牌** —— 按代码排序后段被腾讯三域名集体拒绝（501）。新浪备源解决了这一点：
第二轮 1400 处起补数开始上升（89 → 539 → 1139 → 1739 → 2153 点），失败数稳定在 2 只。

### 5.3 权重指纹未变（守卫按设计拦截）

`factor_eval --optimize-weights` 正常跑完，但 **`factor_weights.json` 指纹恒为
`9347d48f…`、mtime 仍 2026-11-05 09:11**。这是 `force_overwrite=False` 守卫的正确行为：
现权重为受约束管理版本（v4.19 含 `weight_constraints`）→ 拒绝整份覆盖，改写到
`factor_weights_optimized_YYYYMMDD.json`。**不能理解成「优化没生效」**。

`verify_weights_round36.py` → **✅ 八处权重入口 + dim_max 入口全部一致**。

### 5.4 测试

全量 `unittest discover` → **343 tests OK**。

过程中出现过一次真实红灯：`test_meta_has_valid_cross_sections` 报
`308 != 309 : meta 的 valid_cross_sections 与现算不一致 → meta 已陈旧`。
**这是正确的报警，不是测试脆弱** —— 价格库新增了日期而 meta 还是旧缓存。
修法是跑 `refresh_kline_meta`（本就在降级清单里），**没有放宽断言**。

## 六、硬约束（本轮未变）

- **禁 push**，只做本地提交
- 权重指纹 `factor_weights.json` = `9347d48ff91c41accfc6e23825ed8ec235027cba79b23a6b300f4b367a37ebc9`
  （mtime 2026-11-05 09:11，今日未改动；改权重前必须过 `verify_weights_round36`）
- 新增审计产物须登记 `trial_log`
- 页面必须说实话：过程可迁、**披露不可迁**

## 七、遗留（未在本轮处理）

1. `verify_qfq_anchor` 轮转为分片设计，累积覆盖 **5.00%（107/2138）**；未覆盖部分
   **不是已通过，是没测**。走满一轮需 55 个交易日。
2. `_to 600` 纯 bash 超时包装实测不生效（round135 遗留，根因待查）。
3. `analysis_*.html` 未进 `prune_keep`，会无限累积（round132 P1）。
4. `audit_round112` 6 条历史死断言待同步（P1）。
