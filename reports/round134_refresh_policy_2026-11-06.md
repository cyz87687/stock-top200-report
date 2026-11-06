# round134：分层数据更新频率裁决③ 落地（2026-11-06）

- 指令（老大）：**「3、股票池周五更新；财务数据每个财报日之后更新，如三季报公布完后不再更新；行情数据每日更新」**
- 该指令是对 round132 P1 §四 第 3 个裁决点「独立页日更还是周更」的回答
- 本轮性质：**规则代码化 + 现状审计 + P1 范围落定**；⚠️ **未改动任何生产链路的实际调度**

---

## 零、结论先行

1. 这不是「独立页日更 / 周更」的二选一，而是**按数据性质分层的更新频率总纲**：不同数据有各自的自然更新周期，不该一刀切每天全量重算。
2. **本轮最重要的发现**：这套规则 **2026-10-26 就已完整设计并写入代码**（`config.json` 的 `schedule` 段、`schedule_guard.py`、`fin_factor_cache.py`、法定披露截止日口径），但**生产链路零接入** —— 本地 `run_daily_local.sh` 与线上 `daily-refresh.yml` 都直接逐脚本全量跑，唯一编排了调度的 `run_optimized.py` 没有任何链路调用它。这是项目铁律「脚本不在日链 = 必然陈旧」的又一次中招。
3. 已把规则收敛成**单一判据** `data_refresh_policy.py`（纯函数、不执行、不写盘）+ 32 条回归；口径一律复用既有实现，不重复造轮子、不编造节假日表。
4. **真正把降频接进日更链会改变每日产物行为，且与 round133 刚落地的「日期不一致数据不展示」守卫有口径交互**，属风险偏好决策 → 本轮不擅自接线，接线前需老大确认 §五 的两点。

---

## 一、裁决的精确解读（含具体日期举例）

### 1.1 股票池：周五更新

**「股票池」对应**：`fetch_top200_sina.py` 产出的 `top500_all_a.json`（全 A 快照 + 初筛池）、`config.json` 的 `pool_rules` 筛选结果、以及由池成分决定的完整排名表。

| 日期 | 星期 | 是否重建池成分 | 说明 |
|---|---|---|---|
| **2026-11-06** | **周五** | ✅ 更新 | 今天，本周评分日 |
| 2026-11-07 / 08 | 周六 / 日 | ❌ | 非交易日 |
| 2026-11-09 | 周一 | ❌ | 沿用 11-06 池成分 |
| 2026-11-10 ~ 12 | 周二~周四 | ❌ | 同上 |
| **2026-11-13** | **周五** | ✅ 更新 | 下一次池更新 |

判据来自 `config.json → schedule.pool_rerun_weekday = 4`（0=周一…4=周五）。`schedule_guard.plan_mode()` 另加「周五 15:00 收盘后」与「周末补跑」两个执行层条件，本轮判据模块只做日历级判定，不重复实现。

> ⚠️ **语义待确认**：现有 `config.schedule` 把「股票池周五更新」实现成「**完整评分链路周五才跑一次**」（其余交易日只刷行情）。两者不完全等价，见 §五·1。

### 1.2 财务数据：财报披露窗口内随披露更新，窗口关闭即冻结

**法定披露截止日**（复用 `growth_factor_b._PERIOD_DEADLINE`，与 `fund_split.pit_period` / `build_panel_long` 同源）：

| 报告期 | 期末 | 法定披露截止 | 披露窗口 |
|---|---|---|---|
| 一季报 `YYYY0331` | 3/31 | `YYYY-04-30` | 4/1 ~ 4/30 |
| 中报 `YYYY0630` | 6/30 | `YYYY-08-31` | 7/1 ~ 8/31 |
| 三季报 `YYYY0930` | 9/30 | `YYYY-10-31` | 10/1 ~ 10/31 |
| 年报 `YYYY1231` | 12/31 | 次年 `4-30` | 次年 1/1 ~ 4/30 |

**具体日期走一遍（老大要的例子）**：

| 日期 | 状态 | 生效报告期 | 财务数据动作 |
|---|---|---|---|
| 2026-10-15 | 三季报窗口内 | 20260630（严格 PIT） | ✅ 有新公司披露三季报 → 按个股事件重算（`fin_factor_cache` 按 `(code, 报告期)` 缓存） |
| 2026-10-30 | 三季报窗口内 | **20260630** | ✅ 同上；⚠️ 法定截止 10/31 未过，严格 PIT 下**不能**提前用三季报（禁前视） |
| **2026-10-31** | 三季报窗口关闭（**周六**） | 20260930 | 窗口关闭 |
| **2026-11-01 ~ 12-31** | **冻结期** | **恒为 20260930** | ❌ **不再重算** —— 正是老大「如三季报公布完后不再更新」 |
| 2026-11-06（今天） | 冻结期 | 20260930 | ❌ 冻结；池与行情仍各自更新 |
| **2027-01-01** | 年报窗口开启 | 20260930 | ⚠️ 恢复**按披露事件扫描**（有新披露才重算，不是每天全量） |
| 2027-04-15 | 年报 + 一季报双窗口重叠 | 20260930 | 两个窗口同时活跃 |
| 2027-05-01 | 双窗口关闭 | 20270331 | 冻结，生效报告期跳到 20270331 |

> **「三季报公布完后不再更新」= 报告期级冻结，不是永久冻结**：冻结到下一条财报披露窗口开启为止。单测 `test_30` / `test_34a` 把这个边界钉死了。
> **个案财报更正不属调度层**：由 `fin_factor_cache` 的三条失效条件（明细层缺该 `(code,P)` / EM 缓存 mtime 变新 / schema 版本变化）自行处理。

### 1.3 行情数据：每交易日更新

| 日期 | 星期 | 行情 |
|---|---|---|
| 2026-11-06 | 周五 | ✅ 更新 |
| 2026-11-07 / 08 | 周末 | ❌ 非交易日 |
| 2026-11-09 ~ 13 | 周一~周五 | ✅ 每日更新 |

**行情层字段**：全 A 快照（成交额 / 总市值 / 流通市值 / PE / PB / 涨跌幅）、池内现价、K 线增量、板块动量、指数基准、市场状态与风险预算。

> ⚠️ **法定节假日不能靠 `weekday()` 判**。本模块**不编造节假日表**（无真实数据源，违反项目数据纪律）：周末粗判为不更新；工作日返回「应更新」，但**最终以 `check_data_fresh.py` 的指数最新日期门禁（真实行情源）裁决**。例：2026-10-01~10-08 国庆休市，`weekday` 是工作日，但指数日期不前进 → 门禁拒绝，链路 exit 1。调用方也可注入 `is_trade_day` 覆盖。

---

## 二、现状审计：规则已存在，但生产零接入（本轮核心发现）

| 组件 | 状态 | 证据 |
|---|---|---|
| `config.json → schedule` | ✅ 已写（10-26 需求③） | `{"enabled":true,"pool_rerun_weekday":4,"quote_daily":true,"pool_weekly":true}` + 完整 detail 说明 |
| `schedule_guard.py` | ✅ 已实现 | `plan_mode()`：周五 15:00 后 `full`，其余 `quote_only`；含周末补跑、`SCORER_FORCE_FULL/QUOTE` 开关 |
| `fin_factor_cache.py` | ✅ 已实现（需求①） | 按 `(code, 报告期)` 增量缓存，50.5s → 0.4s |
| `step2_stockscorer_v2.py:4403` | ✅ 已接入缓存 | 财务因子增量生效 |
| `growth_factor_b.py` / `build_panel_long.py` | ✅ 法定截止日口径 | `_PERIOD_DEADLINE` 单一来源 |
| `run_optimized.py` | ✅ 唯一编排三项提速的入口 | 调 `schedule_guard.plan_mode()`，`quote_only` 时只跑 `fetch_top200_sina.py` |
| **`run_daily_local.sh`** | ❌ **零调用** `schedule_guard` | 30+ 步无条件全量：fetch → check → step2 → AI → enhance → sector → portfolio → risk → track → IC → selfcheck → focus → bench → regime → gen_report |
| **`.github/workflows/daily-refresh.yml`** | ❌ **零调用** `run_optimized.py` | 同样直接逐脚本全量 |
| **测试覆盖** | ❌ 本轮前为 0 | 全项目 grep `schedule_guard\|plan_mode` 只出现在 `run_optimized.py` 与模块自身 |

**结论**：`run_optimized.py` 是「一次性脚本不在日链」的典型 —— 规则设计了 11 天，从未生效。今天 2026-11-06 恰好是周五、恰好会跑完整链路，但这是**日历巧合，不是调度生效**（11-09 周一仍会全量跑）。

---

## 三、分层矩阵：数据类 → 日链步骤 → 产物 → 页面区块

| 类别 | 频率 | 关联日链步骤 | 主要产物 | 受影响页面区块 |
|---|---|---|---|---|
| **POOL** | 每周五 | `fetch_top200_sina`（池重建 / 初筛）、`apply_pool_rules`、`pick_portfolio`、`pick_multifactor` | `top500_all_a.json`、`top200_scored_<date>.json`、`sector/portfolio_<date>.json`、`sector/mf_latest.json` | 主页 Top3 / 完整排名表成分 / 评级分位、独立页多因子精选组合清单 |
| **FINANCIAL** | 财报窗口内按披露事件；窗口外冻结 | `step2`（quality / growth / ep / size_growth / 现金红旗 / cagr3）、`fin_factor_cache`、`archive_fund_universe` | 财务因子缓存、`data/fin_cache_em/`、`data/fund_universe_*.json` | 个股详情卡基本面、评分分位、成长锚定门槛、PEG 分母 |
| **MARKET** | 每交易日 | `fetch_top200_sina`（快照）、`append_klines_latest`、`refresh_kline_meta`、`verify_qfq_anchor`、`sector_momentum_daily`、`apply_sector_momentum`、`fetch_bench`、`regime_model`、`track_recommendations`、`portfolio_metrics`、`factor_eval`、`generate_ai_assessment` | `data/klines_long.json` + meta、`sector/sector_momentum_*.json`、`regime_forecast.json`、`ai_assessment.json`、因子 IC | 现价 / 涨跌幅 / 成交额 / 市值、板块动量、市场状态卡、复盘点评、焦点池、推荐跟踪 |

> ⚠️ **一处需单独定口径**：`fwd_pe` / `pe_ladder` 是**卖方一致预期**（同花顺预测年报净利润 × 腾讯总市值），**不是财报数据**，不随法定披露窗口变化，而随研报发布/修正更新（round131 的 FY2026 陈旧守卫正是对付它）。老大这条裁决的三类里没有显式覆盖它，见 §五·3。

---

## 四、本轮实际落地（未接线）

| 文件 | 改动 |
|---|---|
| `data_refresh_policy.py`（新建） | 裁决③ **单一判据**：`POOL / FINANCIAL / MARKET` 三类 `should_refresh` + `plan()` 汇总 `full / quote_only`；`effective_report_period()`、`active_disclosure_windows()`、`latest_closed_window()`；CLI `--at / --week / --json`。纯函数，**不执行、不写盘、不编节假日表**；报告期口径直接复用 `growth_factor_b._PERIOD_DEADLINE` |
| `test_round134_refresh_policy.py`（新建） | 32 测：日期工具、严格 PIT 报告期、四类窗口边界、周五/自定义 weekday/enabled=false、冻结期与年报窗口恢复、事件驱动、force、周末、注入交易日历、`plan` 模式、CLI、源码契约（含「禁止自造节假日表」） |
| `schedule_guard.py`（小修） | `load_cfg` / `latest_scored` 两处裸 `json.load(open(...))` → `with open(...)`，消除本轮测试触发的 ResourceWarning（D3 同类遗留） |
| `reports/round132_p1_scope_2026-11-06.md` | §四 裁决③ 改为已定、C 组解冻并按本审计重写 |
| 本文件 | 裁决规范 + 日期举例 + 零接入审计 + 接线前待确认点 |

**验证**：新增 32 测全过 / 全量 **323 测通过** / `verify_weights_round36` PASS（八处权重一致）/ 权重指纹 `c9104e9d…` 未变 / `py_compile` OK。**未 push，仅本地提交**。

**明确未做**：❌ 不改 `run_daily_local.sh` / `daily-refresh.yml` 调度 ❌ 不动权重与评分内核 ❌ 不动页面渲染 ❌ 不改日更链顺序。

---

## 五、接线前需老大确认的 3 个点

1. **「股票池周五更新」是否等价于「完整评分仅周五跑一次」**？
   `config.schedule` 的现实现是后者（10-26 需求③，理由：财务因子报告期内常量、技术因子黏性 0.903、持有周期 T+10 / IC 峰值 h40，日频换分不产生 alpha 只产生换手摩擦）。若老大要的是「**只冻结池成分，但每天仍用当日行情重算全维评分**」，链路会天天 `full`，与提速目标相反。
   → **建议**：沿用 10-26 原设计（周五 `full` / 其余 `quote_only`）。

2. **`quote_only` 日页面怎么标注（与 round133 守卫的口径交互）**？
   `run_optimized.py` 的现设计是「不出新评分页，页面沿用最近一期并在页头标注『评分为 YYYY-MM-DD 期，行情为今日』」。但 round133 的「日期不一致数据不展示」守卫按「产物日期 ≠ 数据日期」隐藏区块 —— 若 11-09 页面沿用 11-06 的 scored，组合/预算等区块会被判陈旧而隐藏。
   → 接线前必须定清：**页面须区分「评分期」与「行情日」两个日期**，守卫按区块分别判定，不能一刀切把沿用上期评分误判成「陈旧数据」。

3. **`fwd_pe` / 卖方一致预期的更新频率**？
   不属上述三类（不是财报、不是行情、也不是池）。round131 已装 `fwd_stale_guard` 对付陈旧预期。建议单列为「按研报事件 + 陈旧守卫」类，**不随财报窗口冻结**。

---

## 六、P1 / C 组接线清单（按上述裁决重写，未实施）

| ID | 内容 | 前置 |
|---|---|---|
| C1' | `run_daily_local.sh` 头部调用 `data_refresh_policy.plan()`，把 `mode` 写日志 + `pipeline_status` 留痕，**先只记录不跳过任何步**，观察一周实际判定 | 无（零行为变更，可先做） |
| C2' | 确认 §五·1/·2 后，`quote_only` 日跳过非行情步；页面加「评分期 / 行情日」双日期标注，守卫按区块判定 | §五 裁决 |
| C3' | 线上 `daily-refresh.yml` 同步（或明确降频只在本地生效） | C2' |
| C4' | 独立页各区块按数据源分别标更新频率与「是否当日」（B6 的具体化：POOL 区块标周更、FINANCIAL 区块标财报期、MARKET 区块标日更） | C2' |
| C0 | ⚠️ **改 `run_daily_local.sh` 后必重跑 `TestPipelineStatus`**（项目硬规则，步级状态机会因跳过步骤误报「应跑未跑」） | 每一步 |

---

## 七、验证记录

```
新增 test_round134_refresh_policy.py       32/32 OK
全量 unittest discover                    323/323 OK
verify_weights_round36.py                  PASS（八处权重入口 + dim_max 一致）
py_compile                                 OK
factor_weights.json shasum                 c9104e9d684163d9（未变）
```

> 数字按当时实测记录；全量用例数会随后续测试增加而变化，**引用时现跑勿抄**。

**⚠️ 禁 push，仅本地提交。**
