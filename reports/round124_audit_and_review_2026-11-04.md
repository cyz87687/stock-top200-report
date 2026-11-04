# round124 — 数据遗漏复核 + 量化从业者视角再评估

生成时间：2026-11-04
评估对象：`data/panel_round123.json`（238 截面 / 2025-11-03~2026-10-27 / 域 2029）
现行模型：`v4.17-cycle-rules`，`target_horizon=20`，权重指纹 `c98df667b5f9353060bedb3d35866edcc06dc1de`
工作树状态：HEAD `4d76e26` 未 commit，无线上推送

---

## 零、先认三条错（都是我上一轮报的结论，被本轮审计推翻）

| # | 上一轮说法 | 本轮实测 | 成因 |
|---|---|---|---|
| 1 | h=20「IC=+0.0629 / NW t=2.08 刚过 T_LOOSE=2.0」 | **IC=+0.0580 / NW t=1.85，连 2.0 都没过** | `panel_round66` 建于 2026-10-29，`score_bias60`/`score_rsi_rev` 仍是 round121 **之前**的「绝对阈值分档」口径（仅 5 个整数值），回填时沿用未重派生；生产早已是池内分位连续口径（465~469 个值）→ 0.10 权重口径错配 |
| 2 | 「组合层超额 +0.85%/期，t=3.13」 | **+0.85% 中 106.8% 来自 2026Q3 单季；剔除后 −0.08%（转负）** | 未做 regime 分解就报了全样本均值 |
| 3 | 「9 维模型」 | **有效维度仅 ≈5.34/8** | `bias60`×`rsi_rev` ρ=+0.766、`tech_neg`×`bias60` ρ=+0.750、`tech_neg`×`rsi_rev` ρ=+0.735，三者最大特征向量载荷 0.530/0.564/0.565 几乎相同 → 名义 0.20 权重实际只是 1 个信号 |

**一句话定性**：在 218 个截面、跨 358 天的样本上，现行权重**没有任何周期通过 Bonferroni 3.8129**，且 h=20 **未过 T_LOOSE=2.0**，超额由单季支撑 → **不得宣称「已验证的 alpha」**。

---

## 一、数据遗漏检查

### 1.1 已查且通过的（8 项，全部实测）

| 审计项 | 实测结果 | 判定 |
|---|---|---|
| 域一致性 | domain 2029 只声明 = 2029 只出现，0 越界 / 0 幽灵 | ✅ |
| 股票覆盖 | 1880 只（92.7%）覆盖全 238 日 | ✅ |
| 幸存者偏差 | 仅 2 只退出（002827/600621 末次 2026-10-23）；34 只中途上市 | ✅ 轻微 |
| 价格完整性 | 501,570 观测：字段缺失 0 / 收盘≤0 0 / high<low 0 | ✅ |
| 价格不自洽 | close 或 open 超出 [low,high]：0 条 | ✅ |
| 日期连续性 | 238/238 与 K 线库一一对应；4 处长间隔全为法定假日 | ✅ |
| 前视检测 | 独立重算 lowvol20 与 panel 值 ρ=**+1.0000**（完全吻合） | ✅ 无泄漏 |
| 前视检测2 | ρ(lowvol20, 未来20日波动) = **−0.5114**（远未到 0.8 泄漏阈值） | ✅ |

### 1.2 新发现的遗漏（3 项，均为实质问题）

#### 遗漏 A：`score_growth`（0.10 权重）在读取层被丢弃 —— 数据已修好，代码没跟上

`panel_round123` **有** `score_growth` 列，且 **238/238 截面满覆盖、行级覆盖 100.0%**。但：

```python
# factor_validation_core.py:54  硬编码 9 维旧表
DIMS = ["score_fund", "score_quality", "score_ep", "score_lowvol20",
        "score_bias60", "score_rsi_rev", "score_tech_neg",
        "score_size_growth", "score_dominant"]        # ← 不含 score_growth
```

`load_panel_arrays()` 用 `DIMS` 作为矩阵 K 轴（`factor_validation_core.py:194,214`）：

```python
nD, nC, nK = len(use), len(codes), len(DIMS)
...
for k, dim in enumerate(DIMS):      # ← 只遍历 9 维，growth 列被静默跳过
```

**实测验证**：

```
用 panel_round123.json 加载 →
  截面 223 × 只 2029 × 维 9        ← K=9，不是 10
  返回维数 K = 9
  'score_growth' in F.DIMS → False
```

**后果**：`val_arrays_h20.npz` 的 `X` 形状是 `(219, 2029, 9)`。任何走这条链的评估（`weight_admission_gate.py:931`、全部 `audit_round9x~10x` 脚本）**在结构上不可能评估 growth 那 0.10 权重**。闸门自己也承认：`DIMS_BLIND = ['score_growth']`。

⇒ **这是「10% 权重从未被检验」，而不是「检验了但没过」。两者性质完全不同。**

#### 遗漏 B：npz 缓存只校验 `hop`，不校验面板内容

```python
# factor_validation_core.py:171-176
if os.path.exists(npz_cache):
    z = np.load(npz_cache, allow_pickle=False)
    if int(z["hop"]) == hop:        # ← 只比 hop
        print(f"  [缓存命中] {npz_cache}")
        return (...)                # ← 直接返回旧矩阵
```

**实测**：`val_arrays_h20.npz` 生成于 2026-10-29 11:58，源自 `panel_round66`（11:40）；覆盖 **219 截面（至 2026-09-22）**，而面板有 **238 截面（至 2026-10-27）**，差 19 个。

⇒ **即便把 `panel_path` 改成 round123、`DIMS` 修好，只要 npz 存在且 hop 相同，仍会静默返回 round66 时代的旧矩阵。** 修 `DIMS` 必须同时让缓存失效。

#### 遗漏 C：`meta.coverage` 与实算不一致

| 维 | metadata 报 | 实算 | 差异 |
|---|---|---|---|
| `score_rsi_rev` | 468,838 / 98.0% | **475,936 / 99.4%** | **未随 123b 口径修正同步** |
| `score_bias60` | 468,838 / 98.0% | 468,965 / 98.0% | 一致 |

原因：`fix_panel_reversal_dims.py` 重派生了 `rows`，但没有回写 `meta.coverage`。**元数据是披露的依据，不能落后于数据。**

### 1.3 已知但未修的数据缺陷

| 项 | 实测 | 优先级 |
|---|---|---|
| 601091 于 2026-09-18 收 20.80→57.77 = **+177.7%** | 沪市主板 ±10%/首日 ±44%，+177.7% 不可能 → 复权因子错乱 | P2（单只，影响有限） |
| 振幅 >50% 共 10 条 | 多为新股上市首日（不设涨跌幅），属正常 | 不修 |
| 涨停样本 2.00% | 剔除后 h1 IC +0.0241→+0.0285、h20 +0.0629→+0.0621，影响可忽略 | 不修 |

### 1.4 结构性缺失：panel 只有分数，没有原始字段

`panel_round123` 行内字段只有 11 个 `score_*`，**缺失**：

```
price / circ_mv / total_mv / amount / turnover_ratio / pe / pb
```

四大后果：

1. **`score_ep` 的 22.7% 缺失无法修补** —— 2026-10-27 有 ep 1,579 / 缺 448，缺的 448 全部是「财务有数据但缺股价」→ 重算需要 price，而 panel 没有
2. **无法做可交易性过滤**（涨跌停/停牌/低流动性）→ 回测收益系统性高估
3. **无法算换手率约束下的容量**（capacity）
4. **无法做行业中性的市值加权基准**（只能等权）

---

## 二、量化从业者视角再评估

### 2.1 最严重问题：三条评估链路给出三个互不相同的 h=20 答案

| 链路 | 数据源 | 截面数 | h=20 结论 | 是否在日链 |
|---|---|---|---|---|
| **A. `factor_eval.py`** | `factor_history.json`（91 快照） | 有效 **14**（h1） | **n=0 / usable=False / 完全不可复现** | ✅ 在（`run_daily_local.sh:326`） |
| **B. `weight_admission_gate.py`** | `panel_round66` + `val_arrays_h20.npz` | **219** | **growth 盲区 / 其余 8 维** | ❌ **不在**（`grep -c` = 0） |
| **C. 本轮 `recompute_ic_round123b.py`** | `panel_round123` | **218** | **IC=+0.0580 / NW t=1.85** | ❌ 不在（一次性脚本） |

**A 链为什么 h=20 是 n=0**（这是关键，且与我上一轮的诊断不同）：

`factor_history.json` 有 91 个快照，但 **76 个截面 rows<200**，中位仅 **194**：

```
rows: min 190 / 中位 194 / max 2696
>=200 的截面数: 15 / 91
<200  的截面数: 76 / 91
```

⇒ `min_cross_section=200` 不是「把大样本筛小了」，而是**上游母体本身就长期只有约 194 只**，这个门槛直接把 84% 的截面判死。**问题不在门槛，在母体。**

**这带来一个致命的一致性问题**：同一份 `factor_ic.json` 里，`cross_section_audit` 说 h=20 不可用（A 链），而 `remedy_round123.measured` 说 h=20 IC=+0.0580 / NW t=1.85（C 链）。**读者无法判断该信哪个。**

### 2.2 三周期真实 IC（C 链，口径修正后）

| h | n | IC | 普通 t | **NW t** | 虚高倍数 | 胜率 | 中位规模 |
|---|---|---|---|---|---|---|---|
| 1 | 237 | +0.0247 | 3.66 | **3.66** | 1.00 | 0.608 | 2013 |
| 5 | 233 | +0.0347 | 5.03 | **2.67** | 1.88 | 0.627 | 2010 |
| 20 | 218 | +0.0580 | 6.79 | **1.85** | **3.67** | 0.679 | 2009 |

- 无任何周期过 Bonferroni **3.8129**
- h=20 不过 T_LOOSE **2.0**
- 普通 t 在 h=20 虚高 **3.67 倍** —— 重叠窗口把 t 值吹了近 4 倍，这是重叠样本的教科书后果

### 2.3 超额完全由 2026Q3 单季支撑

| 季度 | n | IC 均值 | Top10% 超额 | 池 EW 收益 |
|---|---|---|---|---|
| 2025Q4 | 43 | +0.0753 | +0.46% | +6.66% |
| 2026Q1 | 56 | +0.0487 | +1.09% | +1.13% |
| **2026Q2** | 60 | **−0.0004** | **−1.56%** | +0.62% |
| **2026Q3** | 59 | +0.1137 | **+3.37%** | +0.52% |

Leave-one-quarter-out：

| 剔除 | n | IC | NW t | 超额 | 胜率 |
|---|---|---|---|---|---|
| 全样本 | 218 | +0.0580 | 1.85 | +0.85% | 60.6% |
| 2025Q4 | 175 | +0.0538 | 1.39 | +0.95% | 60.6% |
| 2026Q1 | 162 | +0.0612 | 1.47 | +0.77% | 54.9% |
| 2026Q2 | 158 | +0.0802 | 3.53 | +1.77% | 75.3% |
| **2026Q3** | 159 | +0.0373 | 1.33 | **−0.08%** | 51.6% |

⇒ **2026Q3 贡献 106.8% 的总超额。** 剔除后超额转负。

⚠️ 注意 **2026Q2 IC 与超额双负**（−0.0004 / −1.56%），而 2025Q4 池 EW 大涨 6.66% 时超额只有 +0.46%。**下跌市里该模型会失效**，与记忆里 reversal 维「2026Q2 为负 t=−2.48」的结论一致 —— 两处独立实证指向同一件事。

### 2.4 有效维度只有 5.34/8

| 维度对 | 截面相关 ρ |
|---|---|
| `score_bias60` × `score_rsi_rev` | **+0.766** 🔴 |
| `score_tech_neg` × `score_bias60` | **+0.750** 🔴 |
| `score_tech_neg` × `score_rsi_rev` | **+0.735** 🔴 |
| `score_size_growth` × `score_growth` | +0.541 ⚠️ |

特征值分析（8 维共同子集 n=518）：λ1=2.688，λ1/8=0.336 → **有效维度上界 ≈5.34/8（损失约 33%）**

最大特征向量载荷：

```
score_bias60      +0.565  ←
score_rsi_rev     +0.564  ← 三者载荷几乎相同
score_tech_neg    +0.530  ←
score_growth      +0.219
score_size_growth +0.135
score_dominant    -0.085
score_quality     +0.079
score_ep          -0.053
```

⇒ **`bias60` + `rsi_rev` + `tech_neg` 合计名义权重 0.20，有效维度接近 1。** 这三个不是「三个独立因子投票」，而是「同一个超跌反转信号被数了三次」。**这是权重设计层面的事实错误**，不是统计噪音。

### 2.5 组合层反而好于 IC 层

| h | n | Top10% 超额 | t | 胜率 |
|---|---|---|---|---|
| 5 | 233 | +0.20% | +1.99 | 57.5% |
| 20 | 218 | **+0.85%** | **+3.13** | 60.6% |

换手与成本（h=20）：

```
平均单期换手 9.4% / 20 交易日 → 年化 13 次 → 年换手 119%
单边 10bp → +0.84%   20bp → +0.82%   30bp → +0.80%   50bp → +0.76%  全部仍正
```

**解读纪律**：组合层 t=3.13 > IC 层 NW t=1.85，但这**不构成更强的证据**。组合层 t 未做重叠校正（20 日持仓、逐日调仓 → 相邻期高度重叠），与 IC 层的 NW t 不可直接比较。这只说明「信号在极端分位上更集中」，与「整体排序能力」是两件事。

### 2.6 统计功效：样本量根本不足以支撑 h=20 的结论

h=20 的 NW 校正 lag=19，有效独立观测数 ≈ 218/20 ≈ **11**。纯白噪声在 n=10/lag≈19 时的 95% 分位远高于 2.0（记忆实测：n=10/lag=4 → 3.63；lag 更大则更高）。

⇒ **NW t=1.85 在 lag=19 的零分布下毫无意义。** 这个数字既不能证明有效，也不能证明无效 —— **它只是没有信息量**。要得到有信息量的 h=20 结论，需要 ≥ 5~10 倍的时间跨度（约 5~10 年、2000~4000 个交易日）。

---

## 三、修改意见

排序原则：**先修能让结论成立的（口径一致性），再修能扩大样本的（数据），最后修包装（文档）。** 不给时间估算，给验证方式。

### P0 — 不修则任何 alpha 结论都不能引用

#### P0-1 三条链路统一到一份数据源，`cross_section_audit` 只许有一个 h=20 答案

**问题**：同一份 `factor_ic.json` 内，A 链说 h20 n=0 不可用，C 链说 h20 n=218 NWt=1.85。读者无法判断。

**改法**：
1. `factor_eval.py` 增加 `--panel` 参数，把主评估链从 `factor_history.json`（中位 194 只）切到 `panel_round123.json`（中位 2009 只）
2. `cross_section_audit` 每个 h 只保留**一个权威行**，A/C 两条链的结果分别命名（如 `legacy_snapshot_chain` / `long_panel_chain`），并在 `_summary` 里明写「以哪条为准、为什么」
3. `remedy_round123.measured` 不再作为平行结论存放，改为指向权威行的 `ref`

**验证**：`factor_ic.json` 里 `grep -c 'nw_t'` 后，h=20 只能找到一处结论性数值；`test_round117_p0_p1.py` 加断言「cross_section_audit 的 h20 行与 remedy 的 h20 数值必须一致」。

#### P0-2 修 `DIMS` 缺 `score_growth`，并让 npz 缓存失效

**问题**：`F.DIMS` 硬编码 9 维旧表，`load_panel_arrays` 用它作 K 轴 → panel 里 100% 覆盖的 `score_growth`（权重 0.10）被静默丢弃。且 npz 缓存只比 `hop`，不校验内容。

**改法**：
```python
# factor_validation_core.py:54
# 改为从 factor_weights.json 动态解析（weight_admission_gate._resolve_dims 已有实现，复用）
DIMS = [...]          # 保留作 fallback，但加注释说明「已非权威」
```
```python
# factor_validation_core.py:171  缓存加内容指纹
_sig = f"{os.path.getmtime(panel_path)}:{os.path.getsize(panel_path)}:{len(DIMS)}"
if os.path.exists(npz_cache):
    z = np.load(npz_cache, allow_pickle=False)
    if int(z["hop"]) == hop and str(z.get("sig", "")) == _sig:
        return ...
```
**必须同时删掉现有三个 npz**（`icir_arrays_h20/h5`、`val_arrays_h20`），否则旧矩阵继续命中。

**验证**：`load_panel_arrays('panel_round123.json', ...)` 返回 `X.shape[2] == 10`；`weight_admission_gate.DIMS_BLIND == []`。**这是唯一能证明 10% 权重被真正检验的方法。**

#### P0-3 三维共线必须处理（0.20 权重实为 1 个信号）

**问题**：`bias60`/`rsi_rev`/`tech_neg` 两两 ρ>0.73，载荷 0.53~0.57 几乎相同，有效维度≈1。

**两条路，必须选一条并留痕**：

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| **A. 合并为一维** | 三者截面 rank 后等权合成 `score_reversal_comp`，占权重 0.20 | 口径诚实，样本量合并后更高 | 改权重结构 → 八处同步 + 闸门重过 |
| **B. 正交化** | 以 `tech_neg` 为基，`bias60`/`rsi_rev` 对残差打分 | 保留三维可解释性 | 每次重算需重新拟合，可复现性下降 |

**建议 A**。理由：三者本质是同一信号的三个视角（偏离均线、RSI 超卖、技术面负面），合并后 IC 不会更差（记忆实测：两维合并 IC 0.0348 不优于任一单维 → 说明合并无损），且能立刻消除「0.20 权重假装是 3 个独立下注」的误导。

⚠️ **若选 A，权重结构改变 → 必须走完八处同步 + `weight_admission_gate` 五门 + `verify_weights_round36.py` PASS，且 `total` 与旧版本不可比。**

#### P0-4 `score_fund`（0.20 权重）仅 9/238 截面可用，必须显式降权或分段披露

**实测**：
```
有值截面 80/238（2026-06-26 ~ 2026-10-23）
但即使在有值截面上，覆盖率也只有 9.0%~22.5%：
  2026-06-26: 182/2015 =  9.0%
  2026-10-23: 457/2029 = 22.5%
全样本行级覆盖 16,920/478,650 = 3.5%
```

**根因**：主力净流入历史无快照，不可回填。

**三条路（选一条）**：
- **A（推荐）**：把 `score_fund` 从「总分维」降为「展示维」，权重 0.20 按比例分给其余 8 维。**必须重算全部分数并留 `.snap_*` 备份。**
- **B**：保留 0.20 但强制「fund 有值才计入总分，无值则该行总分置 None」→ 结果是 96.5% 的股票没有总分。**不可行。**
- **C**：不动权重，但页面每条评分旁强制标注「本次总分未含资金流（0.20）」→ 等于宣布模型只有 0.80 有效。

**建议 A**。现状是「20% 权重由 3.5% 的数据背书」，这在任何合规口径下都站不住。

---

### P1 — 影响结论可信度，但不阻断使用

#### P1-1 `usable` 判据加「不重叠观测数 + 时间跨度」双判据

**现状**：`factor_eval.py:1055-1087` 只检查 `n >= 2*(h-1)+2`（h=20 → 40）与 `n >= MIN_GATED_SNAPSHOTS`（5）。

**问题**：n=218 时判 `usable=True`，但有效独立观测只有 218/20≈**11**，lag=19 的 NW 估计在 n=11 下无意义。

**改法**：
```python
_n_eff = n / max(1, h)                      # 有效独立观测数
_min_n_eff = 30                             # 经验下限：NW 在 n_eff<30 时不可靠
if _n_eff < _min_n_eff:
    out["usable"] = False
    out["usable_note"] = (f"{n} 个截面 / h={h} → 有效独立观测仅 {_n_eff:.1f} (<{_min_n_eff})，"
                          f"NW lag={h-1} 的 t 值在此样本量下无信息量，不得引用")
```
**同时在输出里披露 `n_effective_obs`**，让人一眼看出「218 个截面 ≠ 218 个独立证据」。

**验证**：h=20 的运行结果 `usable=False`，且 `usable_note` 含「有效独立观测」。

#### P1-2 `panel_round123` 接入日链，并替换 `panel_audit` 的面板来源

**现状**：
```
grep -c 'build_panel' run_daily_local.sh → 0        # 不在日链
data/panel_round123.json  2026-11-04 00:09           # 手工产物，不会自动刷新
panel_audit.py:34  PANEL_CANDIDATES = ["data/panel_round66.json", "data/panel_round34.json"]
```

**问题**：`panel_audit` 读 round66，于是报告披露「growth 是盲区」；而 round123 **growth 满覆盖**。**披露与实际数据脱节**，且 round66 已停在 2026-10-29。

**改法**：
1. `run_daily_local.sh` 在 `step2` 之后、`panel_audit` 之前插入增量 panel 追加步骤（复用 `backfill_panel_round123.py` 的财务分段逻辑，日增只追加当日行）
2. `panel_audit.py:34` 的 `PANEL_CANDIDATES` 把 `panel_round123.json` 放首位
3. 面板新鲜度检查：panel 末日 != scored 末日 → 报告显式标「面板落后 N 日」

**验证**：连续两个交易日跑日链后，`panel_round123.json` mtime 更新且末日 == scored 末日。

#### P1-3 DSR 接入 `factor_eval._verdict_h`

**现状**：`backtest_core.py:175` 已实现 `deflated_sharpe`，`:350-356` 有 `dsr_veto` 逻辑，但 **`factor_eval.py` 里 `grep -n 'dsr\|DSR' ` 全空** → 这条否决权在主评估链上是死代码。仅 `test_round117_p0_p1.py:1807` 在测它。

**改法**：`_verdict_h` 判 `significant` 前先调 `deflated_sharpe`，未过则强制 `significant=False` + `dsr_veto=True`。

⚠️ 记忆铁律：**N>1 时「纯噪声≈0.5」不成立**（N=352 时纯噪声 DSR 中位仅 0.02，`DSR>0.95` 比例 0/200）→ **正确自证判据是「DSR>0.95 比例≈0」**，`sr_std=0.05` 是假设值须标 `sr_std_assumed`。

#### P1-4 修 `meta.coverage` 与数据不一致

`score_rsi_rev` metadata 报 98.0%、实算 99.4%。`fix_panel_reversal_dims.py` 重派生后未回写 `meta.coverage`。

**改法**：口径修正脚本末尾统一重算并覆盖 `meta.coverage`。
**验证**：写一个 `assert_coverage_consistent.py`，实算与 metadata 逐维差 <0.1pp。

---

### P2 — 已知缺陷，影响有限

| # | 项 | 处置 |
|---|---|---|
| P2-1 | 601091 于 2026-09-18 收 20.80→57.77（+177.7%），主板不可能 | 查复权因子并修该只；加日链断言「主板单日 \|r\|>11% → 告警」 |
| P2-2 | `run_daily_local.sh:325` 注释与 echo 粘连（`... (v3.5.5)echo "..."`）→ 整行被注释，`factor_eval` 的日志行从不打印 | 拆成两行（功能不受影响，只是少一行日志，但会误导排障） |
| P2-3 | README 613 行未对齐 v4.17 | 全量对齐 |
| P2-4 | panel 无原始字段（price/circ_mv/total_mv/amount/turnover_ratio/pe/pb） | 若要做容量/可交易性/市值加权基准则必须补；ep 的 22.7% 缺失修补也依赖 price |

---

## 四、必须写进页面/文档的披露（现在缺）

1. **「任何周期均未通过 Bonferroni 3.8129；h=20 未过 T_LOOSE=2.0」** —— 现状只写后者的一部分
2. **「超额 106.8% 来自 2026Q3 单季，剔除后转负」** —— 现状写「组合层超额 +0.85% t=3.13」，未提集中度
3. **「有效维度 ≈5.34/8，三维共线 ρ>0.73」** —— 现状称「9 维」
4. **「score_fund 0.20 权重仅 3.5% 行级覆盖」** —— 现状在闸门里报，但不在主结论
5. **「h=20 有效独立观测仅 ≈11，NW t=1.85 无信息量」** —— 现状未披露 n_eff
6. **「三条评估链路曾给出不同 h=20 答案，已统一为 X」** —— P0-1 完成后加

---

## 五、验证清单（改完后逐项过）

```bash
# 1. HEAD 与权重指纹未变
git rev-parse HEAD                                    # 4d76e26...
shasum factor_weights.json                            # c98df667...（除非走了 P0-3/P0-4 改权重）

# 2. 测试全绿
~/.workbuddy/binaries/python/envs/default/bin/python test_round123_panel_pit.py
~/.workbuddy/binaries/python/envs/default/bin/python test_round117_p0_p1.py

# 3. DIMS 含 growth 且缓存已失效
python -c "import factor_validation_core as F; assert 'score_growth' in F.DIMS"
python -c "import weight_admission_gate as G; assert G.DIMS_BLIND == []"

# 4. cross_section_audit 只有一处 h20 结论
python -c "import json;d=json.load(open('factor_ic.json'));print(d['cross_section_audit'].get('_summary'))"

# 5. 覆盖率元数据一致
python assert_coverage_consistent.py

# 6. 页面自测
python selfcheck_pipeline.py
```

---

## 附：本轮产出文件

| 文件 | 作用 |
|---|---|
| `fix_panel_reversal_dims.py` | 修正 bias60/rsi_rev 口径（5 离散 → 501 连续），6.9s |
| `recompute_ic_round123b.py` | 三周期 IC 重算 + 写更正声明 |
| `diag_regime_stability_round123b.py` | 分季度稳定性 |
| `diag_ic_concentration_round123b.py` | Leave-one-quarter-out |
| `backfill_panel_round123.py` | 修正 `DAILY_DIMS` 分类虚报（0.80→0.60） |
| `test_round123_panel_pit.py` | 19 test 全绿（修正断言方向） |
| `.snap_panel_round123_before_revfix.json` | 口径修正前备份 |
| `.snap_factor_ic_before_round123.json` | 审计写入前备份 |
