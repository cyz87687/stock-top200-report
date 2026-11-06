# round133：裁决落地 —— 「日期不一致数据不要展示」

- 日期：2026-11-06
- 老大裁决：①「不需要」（多因子精选组合不回主页）；②「日期不一致数据不要展示」
- 性质：P0 修复 + 页面数据纪律；**不动权重、不动评分内核、不动日更链顺序**

---

## 一、结论先行

裁决② 不是原选项 A（空仓 0）/ B（容忍 1 天）/ C（保守仓 0.2）中的任何一个 —— 那三项都在「给一个仓位数字」。裁决是**第四方案**：产物日期与数据日期不一致时，相关数值**既不产出、也不展示**，只在页面留一句「未更新」披露。

已落地为 `regime_date_guard.py` **单一实现**（禁止各处再写 `date_str in xxx` 弱判断），接入 4 层；11-05 页面重生成实测三处陈旧数据全部消失。

---

## 二、具体日期举例（问题现场）

| 时间 | 事件 |
|---|---|
| **2026-11-04 17:36** | 日更正常生成 `regime_forecast.json`：`date=2026-11-04`、内部 `gate.ok=True`（自检时 11-04 数据齐）、`combine.final_hi=0.5837` |
| **2026-11-05 晚** | `fetch_bench` 挂了（000985 中证全指全部行情源失效）→ `regime_model` 日期门禁**正确拒绝**生成新文件 |
| **2026-11-05 晚** | `pick_portfolio.load_w_total` 读到的仍是 11-04 文件；只校验文件内部 `gate.ok`（True）→ **放行** → 11-05 组合用 `w_total=0.5837`、`budget_status=OK` |
| 产物铁证 | `sector/portfolio_2026-11-05.json` → momentum / reversal 均 `w_total=0.5837`、`src.date=2026-11-04`、`budget_status=OK`；reversal `executable=True` |

**另一处同类陈旧**：`sector/mf_latest.json` 的 `date=2026-10-23`，而 11-05 页面用 `mf_pick_2026-11-05.json` 不存在 → 回退读它 → 独立页「🧮 多因子精选组合」展示的是 **10-23 的 10 只清单**，标题却写当日。

---

## 三、实现（单一实现 + 4 处接入）

### 3.1 `regime_date_guard.py`
- `normalize_date()`：抽 `YYYY-MM-DD`，容忍 `2026-11-05 15:00:00`
- `check_artifact_date()`：相等才 `OK`；不等 `STALE_DATE`；产物无日期 `NO_ARTIFACT_DATE`（无法证明一致，同样不展示）；缺数据日期 `UNVERIFIED_NO_DATA_DATE`
- `check_payload()` / `budget_source_stale()` / `hidden_notice()`：dict 取日期、组合产物复核、统一披露文案
- ⚠️ `budget_source_stale` **不信任产物自带的 `budget_status`**：历史产物写它时还没装守卫，仍标 `OK`；必须自己比 `source.date`

### 3.2 组合层 `pick_portfolio.py`
- `load_w_total(..., data_date=None)` 新增数据日期参数；生产 `main` 传 `sdate`
- **顺序修正**：原先「先读预算、后读 scored」导致想校验也没日期可比 → 改为先解析 scored 拿 `sdate`
- 日期不一致 → `STALE_DATE_HIDDEN` / `hide_data=True` / `executable=False` / 清空 `final_hi`、`final_lo`、`bound_by`、`gate_ok` 等全部陈旧数值
- `main` 写盘前若 `hide_data` → `out['portfolios'] = {}`，**不产出任何清单、权重、仓位数字**，只留 `hidden_reason` 与 `budget_source` 留痕
- `data_date=None`（单测/离线）跳过校验，旧契约不变（`test_round122_portfolio` 7 条全过）

### 3.3 风险层 `portfolio_risk.py`
`compute_one` 的「风险预算对齐」用 `pf['_date']`（main 注入）校验 `regime_forecast.json`；不一致时不写 `budget_target_vol` / `vol_vs_target` / `vol_aligned`，只留 `budget_note`。顺手把裸 `open()` 改成 `with`（减少 ResourceWarning）。

### 3.4 页面层 `gen_report_top200.py`
- Hero 敞口行 `_forecast_hero_line`、市场状态卡：把各自写的 `date_str in _rdate` 换成统一守卫
- 组合区块（🧮 多因子精选组合）两条**独立依据**，任一陈旧即整块替换占位：
  ① `mf_pick_<date>.json` / `mf_latest.json` 的 date（`2026-10-23`）
  ② 组合产物 `risk.w_total_source.date`（`2026-11-04`）
- 做法沿用「**计算保留、渲染迁移**」：区块照常生成，末尾 `del _detail_target()[_combo_detail_at:]` 换成 `_stale_placeholder()`，不动大段缩进
- 占位保留 `id="sec-mf-pick"` 与标题（导航锚点不死），列出被隐藏模块名，**只披露日期、不披露任何陈旧数值**

### 3.5 自检层 `audit_round112_page_selftest.py`
G 组原断言「`final_hi` 百分比必须出现在页面」等价于强迫展示过期预算，是死断言 → 改为日期感知：日期一致才断言出现，不一致则断言**未展示**。
> 踩坑：不能用裸 `58%` 判据 —— 主页实测 156 处，全部来自个股详情卡（「3年收缩58%」「+3.58%」「技反 58% 低波」），与 regime 无关；改锁 `0.5837` / `58.37%` / `风险预算 58%` / `单日最大敞口 58%` 四种可归因写法。

---

## 四、11-05 页面重生成实测

```
⚠️ Hero 敞口行跳过：产物日期 2026-11-04 ≠ 数据日期 2026-11-05（按裁决不展示陈旧数值）
⚠️ 市场状态卡片 … 产物日期 2026-11-04 ≠ 数据日期 2026-11-05 … 本卡片已跳过渲染
⛔ 组合区块按裁决不展示：产物日期 2026-10-23 ≠ 数据日期 2026-11-05
```

| 检查 | 结果 |
|---|---|
| 主页 `0.5837` / `风险预算 58%` / `单日最大敞口 58%` | **0** |
| 独立页 `0.5837` / `全页唯一的当日候选清单` | **0** |
| 独立页占位「本期不展示」/ `2026-10-23` / `id="sec-mf-pick"` | 各 1 |
| 主页核心要素（评分表 / 分页 / 五条披露 / 目标周期 / 禁称经验证 alpha） | 全在 |
| 双页 nav 锚点 | 零死链 |
| 体积 | 主页 7,345,294（不变）；独立页 248,926 → **219,691 字符** |

> **11-05 历史组合产物未重跑**（留证，避免盘中数据污染），但页面已独立复核日期、不再展示其陈旧清单。今晚日更链起，`pick_portfolio` 产出的文件本身也会带 `STALE_DATE_HIDDEN` + 空 `portfolios`。

---

## 五、边界与未做

- 主页个股表 `data-route` 标注**保留**：来自 `portfolio_2026-11-05.json`（文件日期 = 数据日期），不依赖陈旧预算数值
- 裁决①：多因子精选组合**不回主页**，不做「清单留 / 解释迁」拆分
- 裁决③（独立页日更 / 周更）**未答** → 暂按日更，C 组降频冻结
- 仍属 P1：焦点池 / 推荐跟踪 / Transformer / T+1T0 / 复盘点评等区块未逐块标「是否当日」；`factor_2026-10-29.md` 死链；`audit_round112_page_selftest.py` 的 B/C 组 40 条断言仍按旧单页检查主页（round132 迁移后的历史遗留死断言），本轮只修 G 组
- ❌ 不重跑历史产物、不动权重八处、不 push

---

## 六、验证与产物

| 项 | 结果 |
|---|---|
| `test_round133_regime_date_guard.py` | **18 / 18 通过**（守卫 6 / load_w_total 5 / 源码契约 4 / 产物 3） |
| 全量 unittest | **291 / 291 通过** |
| `verify_weights_round36` | PASS（八处权重一致、指纹未变） |
| `py_compile` | OK（6 个改动/新增 py） |
| 页面自检 G 组 regime 两条 | ✅ 陈旧数值未展示 |

新增/改动：`regime_date_guard.py`、`test_round133_regime_date_guard.py`、`pick_portfolio.py`、`portfolio_risk.py`、`gen_report_top200.py`、`audit_round112_page_selftest.py`、`index.html`、`top200_report_2026-11-05.html`、`analysis.html`、`analysis_2026-11-05.html`、`reports/round132_p1_scope_2026-11-06.md`。
