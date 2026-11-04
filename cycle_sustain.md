# 周期行业盈利持续性校验模块（cycle_sustain）

> 模块定位：适配现有估值锚打分引擎，**区分【永续成长】与【周期景气脉冲 / 一次性收益】**，修正盈利持续性分 `G_sustain`，并对持续性不足的周期标的**约束估值锚上限**。
> 设计目标：解决"福达合金类"——主业周期属性强、盈利可持续性低，却因估值锚高分拿到接近满分的失真问题。
> 状态：**v1.1 已激活（2026-10-28 老大指令①）**——`cycle_sustain.val_anchor_limit` 已写入 `recalc_v412.py`（耐久，后续 recalc / 每日刷新自动复施）。当前为「降级模式」：仅 `is_cycle 判定 + 估值锚封顶 8.5` 生效，Step1–4 因输入缺口全部 SKIP（详见 §6）。
> **实测结果**：池内周期股 907 只，估值锚被封顶 8.5 的 **124 只**；重算后首页 `total>75` 名单由 64 → **60** 只（亚钾国际 / 闰土股份 / 鹏辉能源 / 天齐锂业 4 只跌出，均为靠未封顶高估值锚虚抬者）；分位评级分布恒定（页面口径 Δ=0）。

---

## 0. 前置约定

| 项 | 约定 |
|---|---|
| 量纲 | `G_sustain` 与 `score_ep` 统一为 **0–10 分**制 |
| 仅对周期行业生效 | 非周期行业（常规成长 / 防御）**不触发**本模块任何惩罚或封顶 |
| 顺序约束 | 五步校验**顺序不可调换，且只降不升**（每步最多下调，绝不上调） |
| 降级纪律 | 缺输入的步骤**一律 SKIP（不施加惩罚）**，并在返回 `steps_skipped` 中标注；**禁止用占位值 / `or 0` / 编造**顶替缺失源 |
| 数据源铁律 | 所有输入必须来自真实接口 / 文件；宁可少罚，不可错罚 |

---

## 1. 行业分类清单

映射对齐本项目申万一级（`sector_parent`）/ 二级（`sector_l2`），优先沿用 step2 既有 `is_cyclic`（含 `SW_CYCLICAL` 兜底），再按本清单补判准周期。

### 1.1 强周期（强制全套五步校验）
| 申万一级 | 覆盖二级细分 |
|---|---|
| 有色金属 | 金属新材料（福达合金即此） |
| 煤炭 | 煤炭开采、焦炭 |
| 石油石化 | 油气开采、油服工程、炼化及贸易 |
| 钢铁 | — |
| 基础化工 | 化学纤维、农化制品、玻璃玻纤 |
| 建筑材料 | 水泥 |
| 交通运输 | 航运、港口 |
| 机械设备 | 工程机械 |
| 非银金融 | 证券（券商） |
| 房地产 | — |

### 1.2 准周期（建议开启，本轮默认开启）
| 申万一级 | 覆盖二级细分 |
|---|---|
| 电力设备 | 电池、光伏设备、风电设备 |
| 电子 | 半导体 |
| 国防军工 | 航空装备 |
| 农林牧渔 | 养殖业、饲料 |

> 判定优先级：`is_cyclic=True` → 沿用 step2；否则按本清单 parent / l2 补判。step2 已判周期的，若父级属强周期按 `strong`、属准周期按 `quasi`，均未命中则**从严按 `strong`** 处理。

---

## 2. 五步校验流程（顺序不可调换，只降不升）

设 `G_sustain` 初始值 = 既有 `score_sustain`（step2 启发式 1.0–5.0，视作 0–10 量纲；缺则取中性 3.0）。

### Step1 · 营收–利润增速匹配校验
- **输入**：`rev_growth`（营收同比增速）、`net_growth_adjusted`（扣非净利增速，缺则用 `growth` 近似）
- **规则**：若 `net_growth > rev_growth + 50` → `G_sustain ×= 0.70`（利润靠一次性 / 非主营拉动，持续性打折）
- **通过**：增速差 ≤ 50 → 不调整

### Step2 · 毛利率与核心产品价格联动校验
- **输入**：`gross_margin_trend`（连续 4 季度毛利率趋势：`up`/`down`/`flat`）、`commodity_price_trend`（行业核心产品价格趋势：`up`/`down`/`flat`）
- **规则**：若 `gross_margin_trend == up` 且 `commodity_price_trend ∈ {flat, down}` → `G_sustain ×= 0.75`（毛利改善靠降本而非需求，景气不可持续）
- **通过**：同向或无背离 → 不调整

### Step3 · 一次性收益硬拦截
- **输入**：`one_time_flag`（一次性收益标记：归母与扣非差额异常 / 公告标记）
- **规则**：若 `one_time_flag == True` → `G_sustain = 2.0`（强制低分，直接否定持续性）
- **通过**：标记存在且为 False → 不调整

### Step4 · 景气高位封顶惩罚
- **输入**：`sector_margin_percentile`（行业毛利率 5 年分位）
- **规则**：若 `sector_margin_percentile > 0.8` → `G_sustain = min(G_sustain, 6.0)`（景气接近历史顶部，未来回归概率高）
- **通过**：分位 ≤ 0.8 → 不调整

### Step5 · 输出边界约束
- `G_sustain_final = clip(G_sustain, 0, 10)`

---

## 3. 联动估值锚约束规则

> 目的：用修正后的 `G_sustain_final` 反向约束估值锚 `score_ep`，根治"周期股低持续却估值满分"。

| 条件 | 动作 |
|---|---|
| 非周期行业 | 不启用封顶（估值锚保持原分） |
| 周期行业 且 `G_sustain_final ≥ 7.0` | 持续性充足，估值锚正常 |
| 周期行业 且 `G_sustain_final < 7.0` 且 `score_ep > 8.5` | **估值锚封顶 8.5**（`VAL_ANCHOR_CAP`） |
| 周期行业 且 `G_sustain_final < 7.0` 且 `score_ep ≤ 8.5` | 不调整 |

**参数表**

| 参数 | 值 | 含义 |
|---|---|---|
| `G_SUSTAIN_CAP` | 6.0 | 景气高位持续性硬上限（Step4） |
| `VAL_ANCHOR_CAP` | 8.5 | 周期股持续性不足时估值锚上限 |
| `SUSTAIN_THRESHOLD` | 7.0 | 估值锚满分所需持续性门槛 |
| `GAP_THRESHOLD` | 50 | Step1 利润–营收增速差阈值（百分点） |

---

## 4. 伪代码

```python
def cycle_sector_sustain_correction(row):
    cyc, tier = is_cycle_sector(row)          # §1 行业判定
    if not cyc:
        return row["score_sustain"], info("非周期,跳过")   # 仅周期触发

    g = row.get("score_sustain", 3.0)         # 基准 G_sustain (0-10)

    # Step1 营收-利润增速匹配
    if rev_growth is not None and net_g is not None:
        if net_g > rev_growth + GAP_THRESHOLD:
            g *= 0.70
    else: SKIP                                # 缺输入不罚

    # Step2 毛利-价格联动
    if gm_trend == "up" and cpt in ("flat","down"):
        g *= 0.75
    elif gm_trend is None or cpt is None: SKIP

    # Step3 一次性收益硬拦截
    if one_time_flag is True: g = 2.0
    elif one_time_flag is None: SKIP

    # Step4 景气高位封顶
    if smp is not None and smp > 0.8: g = min(g, G_SUSTAIN_CAP)
    elif smp is None: SKIP

    # Step5 边界
    g_final = clip(g, 0, 10)
    return g_final, info

def val_anchor_limit(row, g_final):
    cyc, _ = is_cycle_sector(row)
    val = row["score_ep"]
    if not cyc or val is None: return val, False
    if g_final >= SUSTAIN_THRESHOLD: return val, False
    if val > VAL_ANCHOR_CAP: return VAL_ANCHOR_CAP, True
    return val, False
```

---

## 5. 策略文档说明文案（可直接贴入因子白皮书）

> **周期盈利持续性校验（cycle_sustain）**
> 为防范周期股"景气脉冲 / 一次性收益"被误判为永续成长、进而在估值锚维度拿高分，本系统对强周期与准周期行业叠加五步盈利持续性校验：依次核验营收–利润增速匹配度、毛利率与核心产品价格联动、一次性收益、行业景气分位，逐层下调盈利持续性分 `G_sustain`（仅降不升）。最终以修正后的 `G_sustain` 反向约束估值锚 `score_ep`：持续性低于阈值（<7.0）的周期股，估值锚最高封顶 8.5 分。该机制在**不改动非周期标的**的前提下，专门压制"低持续却高估值分"的周期失真。

---

## 6. 数据契约与当前缺口（⚠️ 上线关键阻塞）

实测 `top200_scored_2026-10-27.json`（1895 只）字段可得性：

| 输入字段 | 用途 | 状态 |
|---|---|---|
| `is_cyclic` | 行业判定 | ✅ 命中 320 只 |
| `score_sustain` | G_sustain 基准 | ✅ 1895 只 |
| `growth` | Step1 net 近似 | ✅ 1895 只（扣非净利增速低基数清洗） |
| `rev_growth` | Step1 营收增速 | ❌ **缺失**（需营收增长源） |
| `gross_margin_trend` | Step2 毛利趋势 | ❌ **缺失**（需季度毛利序列） |
| `commodity_price_trend` | Step2 价格趋势 | ❌ **缺失**（需商品/原料价格源） |
| `sector_margin_percentile` | Step4 景气分位 | ❌ **缺失**（需行业毛利 5 年历史） |
| `one_time_flag` | Step3 一次性收益 | ❌ **缺失**（需归母/扣非差额或公告标记） |

**现状**：Step1–4 全部 SKIP，仅 `is_cycle 判定 + val_anchor_limit 封顶 8.5` 生效。降级模式已能压制福达合金类满分问题（演示：福达合金 `score_ep 9.73 → 8.5`、威海广泰 `8.79 → 8.5`），但**未能释放 Step1–4 的精细持续性强弱区分**。

**补齐任一项输入后，对应步骤自动激活，无需改代码。**

---

## 7. 接入方式（已裁定）

| 方案 | 说明 | 状态 |
|---|---|---|
| A. 审计披露 | 模块独立运行，仅诊断 | 未采用 |
| B. 写回 step2（含 G_sustain 回写） | 改 `score_sustain` + 封顶 `score_ep` | 暂缓（Step1–4 输入缺口未补，降级模式回写无增量） |
| **C. 仅激活估值锚封顶 ✅** | 写回 `score_ep` 封顶 8.5（不改 `score_sustain`），已注入 `recalc_v412.py` | **2026-10-28 上线** |

**方案 C 落地细节（2026-10-28）**
- 注入点：`recalc_v412.py` 在 `ep_size_ind` 产出 `score_ep` 之后、total 重算之前，调用 `cycle_sustain.is_cycle_sector` + `cycle_sector_sustain_correction` + `val_anchor_limit`。
- 仅用真实已有字段（`is_cyclic` / `score_sustain` / `score_ep`），**不编造**；降级模式 `G_sustain_final == score_sustain`（Step1–4 全 SKIP）。
- 审计留痕：封顶股票写 `score_ep_raw`（封顶前原值）、`ep_capped=True`；scored 顶层 `cycle_anchor_cap` 记录 `{applied, n_capped, cap, as_of}`。
- 回归守卫：重算前旧字段复现 total 误差 **0.000000 / 1895**（EM 缓存无漂移），封顶为唯一变动（下降 124 只 / 上升 0 只，最大 −1.79）。
- 耐久性：因注入 recalc，后续任何 recalc / `.github/workflows/daily-refresh.yml` 自动刷新都会复施，不会被冲掉。

> 无论后续是否升级到方案 B，均须遵守"先在本池回测、再决定是否纳入权重"的工程纪律（参见项目记忆 P0-4 混合量尺结论：绝对分可比性受分母影响，不影响 rank IC）。
