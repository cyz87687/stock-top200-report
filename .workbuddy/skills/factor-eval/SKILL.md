---
name: factor-eval
description: A股TOP200评分系统的因子有效性评估（Rank IC）与IC驱动权重优化工作流。当需要"每日跑完链路后登记因子IC""判断某个评分维度还有没有预测能力""要不要重新拟合十维权重""看因子间相关/推荐组合IC"时使用。触发词：因子IC、IC登记、因子有效性、权重优化、--optimize-weights、因子还有用吗、调权重、factor_eval、Rank IC、先验权重落档、prior_pending。封装 stock-top200-report/factor_eval.py 的全部用法、统计口径、权重优化纪律与数据避坑。
version: 1.0.0
origin: project
agent_created: true
---

# factor-eval — 因子有效性评估与权重优化

## 何时使用

- **日常**：每日链路 Actions Step 3.96 已自动调用 `factor_eval.py`（无需手动）；本地手动跑完整链路时同样自动执行（`run_daily_local.sh` Step 3.96）。
- **手动介入场景**：
  1. 老大问「最近因子/某个维度还灵不灵」「要不要调权重」→ 先跑 `factor_eval.py --show` 看 IC 表；
  2. 积累满 30 个有效截面且需复核权重 → `factor_eval.py --optimize-weights`（见下方纪律，勿随意触发）；
  3. 价格回填断裂/新机器初始化 → `--bootstrap-prices` / `--bootstrap-from-git`。

## 一、CLI 用法（5 个模式）

| 命令 | 作用 | 频率 |
|---|---|---|
| `python3 factor_eval.py` | 合并本地 `top200_scored_*.json` 截面（同日覆盖幂等）→ 算 Rank IC → 写 `factor_ic.json` | 每日（链路自动） |
| `python3 factor_eval.py --show` | 打印 IC 结果表（因子 × T+1/T+5/T+20 + 推荐组合 + 相关矩阵） | 随时查看 |
| `python3 factor_eval.py --optimize-weights` | 拟合新十维权重 → 写 `factor_weights.json` | **仅满30截面后人工/Agent触发** |
| `python3 factor_eval.py --bootstrap-prices` | 回填全量前复权日线（腾讯多域名轮询，网络密集） | 周期性/断裂时 |
| `python3 factor_eval.py --bootstrap-from-git` | 从 git 历史 scored json 初始化截面 | 仅本地新机 |
| `python3 factor_eval.py --pool-filter 500` | 池口径过滤（扩池 TOP200→TOP500 前后截面分布不同，**不可混算**） | 扩池后首次 |

解释器固定：`/Users/yzreal/.workbuddy/binaries/python/envs/default/bin/python`。

## 二、输出文件（均为 *.json，git 需 `git add -f`）

- `factor_ic.json`：日常产出。13 因子 × 3 周期的 IC 均值/标准差/NW t 值/胜率/分层多空/半期稳定性/Bonferroni 显著性 + 推荐组合 IC + 因子间截面相关矩阵 + 综合总分 IC。
- `factor_weights.json`：`--optimize-weights` 后产出，供 step2 / apply_sector_momentum 读取。**`version` 字段是评分层版本唯一来源**，升版必须同步（如 `v3.0-10dim`→`v3.5.1-10dim`）。
- `factor_history.json`：历史截面登记（每日 upsert），IC 计算的样本池。

## 三、统计口径（读表必知）

- **Rank IC(t→t+h)** = Spearman(因子值 t 日截面秩, 收益率 t+h)，h ∈ {1, 5, 20} 交易日。
- **Newey-West HAC** 修正重叠窗口自相关（h=5/20 相邻 IC 共享 80%/95% 收益窗口）；普通 t 值虚高，以 **NW t** 为准。
- **Bonferroni 阈值**：30 次检验（10 维 × 3 周期）家族校正，|NW t| ≥ **3.08** 为显著（★），宽松阈值 2.0 作对照。
- **verdict 分档**：IC≥0.05 且 t≥2.0 显著有效 / ≥0.03 且 t≥1.5 弱有效 / ≥0.02 方向为正·不显著。
- `score_theme`/`score_tech` 标注「已拆解·仅对照」（十维拆分后由 rsi_rev/vol_pulse/overbought/theme_intrinsic/attention 承载）。

## 四、权重优化纪律（--optimize-weights，硬规则）

1. **prior_pending 门槛**：新拆 7 维无池内截面 → 先验映射（`adopted=prior_pending_10dim`）；**必须满 30 个有效截面**才允许优化，勿冒充池内验证。
2. **仅 IC>0 维度**按 IC 比例分配，负向维度降到地板 `W_FLOOR=0.03`。
3. **先验收缩**：`w = λ·w_IC + (1−λ)·w_prior` 防过拟合（λ 扫描取 OOS 最优）。
4. **走步验证**：expanding window，训练≥30 截面、每 5 截面重拟，样本外比对。
5. **落档门槛**：样本外 IC 提升 **> 0.005** 才写入 `factor_weights.json`，否则维持先验并如实记录。
6. **目标周期 T+1**（与日报 operational 口径一致、无重叠、流失最低）；T+5/T+20 仅作稳健性门槛。
7. `score_sector` 有效截面 <20 → 权重固定 **0.10**，标记"样本积累中"。

## 五、数据口径避坑（v2 重写修正的三个缺陷）

1. **非交易日陈旧快照**：假日/接口故障日 workflow 仍可能产出 scored json。必须以回填的全市场前复权日线日期为真实交易日轴，快照对齐后取第 h 个后续交易日；非交易日快照整体剔除。最新一日常因"尚无 T+h 价格"而待结算（非假日），输出会分开标注。
2. **样本流失（TOP200 留存偏差）**：只对 t 与 t+h 都在榜个股算收益 → IC 是留存子样本的条件估计（T+20 流失约 34% 且非随机）。修正：回填全部历史代码前复权日线，对 t 日全部 TOP200 个股计算收益。前复权口径（7 月分红密集期不复权会制造伪跌幅）。
3. **重叠窗口 t 值虚高**：见 NW 修正。
4. **价格回填**：腾讯 `ifzq.gtimg.cn`（首选）→ `web.ifzq.gtimg.cn` → `proxy.finance.qq.com/ifzqgtimg` 多域名轮询；akshare 清代理。`PX_NEED=130` 根日线覆盖约 90 交易日。

## 六、工程纪律（本项目强规则）

- **每日登记入口唯一** = Actions Step 3.96；本地复盘链路 `run_local_review.sh` 的 finalize **已移除** factor_eval 调用，本地手动才跑（同日 upsert，不重复计样本）。
- **勿在复盘链路自动触发** `--optimize-weights`；权重再拟合由人工/Agent 周期触发，不进每日链路。
- **改脚本后必须实跑冒烟**：`py_compile` 只查语法不查名字（曾因 `glob` 未导入运行时 NameError）；workflow 各步 `if...else ⚠️` 守护会静默吞错，需 `selfcheck_pipeline.py`（Step 3.97）兜底。
- **iFinD 不可用污染**：本地无 iFinD 时 score_news 走算法兜底，此时 IC 表中消息面维度的解释力会偏离生产（Actions 同）——分析 IC 时注意此维度样本期口径。
- **数据纪律**：全部取自行程内真实文件/接口；样本不足如实标注，不补齐、不编造。投资有风险，本内容仅供研究与教育，不构成投资建议。

## 七、已知局限（必须在结论中声明）

股票池为当前存续股票 → 幸存者偏差；行业成分当前快照 → 轻微前视；未模拟滑点与冲击成本；停牌持有期顺延；扩池（TOP200→TOP500）前后截面分布不同，须 `--pool-filter` 区分，否则污染 IC 与权重复核；单一行业分类口径可能影响结论。
