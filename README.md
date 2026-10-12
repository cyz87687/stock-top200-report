# A股成交额 TOP200 复盘评分系统

> 基于当日真实行情数据，对全市场成交额 TOP200 主力池 + 200~1000 名观察池进行**五维加权评分**、AI 复盘评估与组合推荐，每日收盘后自动生成单页 HTML 报告并发布至 GitHub Pages。

**线上报告**：https://cyz87687.github.io/stock-top200-report/

| 项目 | 值 |
|---|---|
| 报告层版本 | **v2.47** |
| 评分模型版本 | **stock-scorer v2.41-ic**（五维 · AI 增强 · 含板块动量 · **IC 驱动权重**） |
| 数据日期 | 见线上报告页眉（最新交易日） |
| 主力池 | 全A成交额 TOP200 |
| 观察池 | 全A成交额 200~1000 名（800 只，每日 AI 精选需复盘标的） |
| 自动更新 | 工作日 15:30（北京时间）via GitHub Actions |
| 本地深度复盘 | 收盘后由 `run_local_review.sh` 编排，逐公司联网核查覆盖页面结论 |
| 因子有效性评估 | Rank IC（T+1/T+5/T+20），Newey-West t + Bonferroni 校正，`factor_eval.py` 每日更新 |
| 评分权重 | IC 实证驱动（`factor_weights.json`），样本外走步验证通过后生效 |
| 数据新鲜度守卫 | `check_data_fresh.py`（workflow Step 1.5，陈旧数据熔断） |

---

## 一、评分模型（v2.41-ic 五维 · IC 驱动权重）

总分按其五个维度的加权和计算，再放大 4 倍。权重由 **Rank IC 实证反向配置**（v2.47，见「二·七」）：

```
现行权重（v2.41-ic，样本外走步验证通过）：
total = ( 消息面×0.396 + 技术面×0.044 + 基本面×0.396 + 题材热度×0.044 + 板块动量×0.12 ) × 4

先验权重（v2.40，IC 优化前）：
total = ( 消息面×0.20 + 技术面×0.20 + 基本面×0.30 + 题材热度×0.18 + 板块动量×0.12 ) × 4
```

| 维度 | 先验权重 | 现行权重 | 该维度 T+1 IC | 说明 |
|---|---|---|---|---|
| 基本面 | 0.30 | **0.396** | +0.049 | 基础分×0.6 + 业绩一阶导(增速)×0.2 + 业绩二阶导(growth−cagr3)×0.2 |
| 消息面 | 0.20 | **0.396** | +0.049 | AI 赋分 0.7×LLM + 0.3×算法 |
| 技术面 | 0.20 | **0.044** | −0.035 | 均线/RSI/趋势等（TOP200 关注度池内呈均值回复，降权至地板而非删除） |
| 题材热度 | 0.18 | **0.044** | −0.041 | AI 赋分 + 当日涨跌修正 `day_mod = clamp(pct_chg/10, -1, +1)`（同上） |
| 板块动量 | 0.12 | 0.12（维持） | 样本积累中 | 申万二级板块 60日/10日动量排名（0.6×R60档位 + 0.4×R10档位） |

**评级**：S≥17 ｜ A 13~17 ｜ B 9~13 ｜ C 5~9 ｜ D <5（v2.41 起报告层按当日截面动态分位定级）

> **权重生效规则**：`factor_weights.json` 由 `factor_eval.py --optimize-weights` 生成（走步验证通过才写入），
> `step2_stockscorer_v2.py` 与 `apply_sector_momentum.py` 启动时读取；文件缺失或校验失败自动回退先验权重。
> 权重变更**自下一次全新评分跑生效**，历史数据的总分仍按当时权重计算（页眉评分层版本以数据自身 model 字段为准）。

> **板块动量赋分方向**：`apply_sector_momentum.py --mode evidence`（默认）依据 2021–2026 回测证据——申万二级板块 R60 与后续 20 日超额呈负单调，故动量末段得分更高；`--mode momentum` 为用户原始思路（动量越强分越高）。

---

## 二、每日数据链路（严格按序）

```
1. fetch_top200_sina.py              → top200_all_a.json（主力池TOP200）
                                       + top1000_all_a.json（观察池200~1000名）
1.5 check_data_fresh.py              数据新鲜度守卫（指数日线日期 + 价格陈旧率，异常则熔断 exit≠0）
2. step2_stockscorer_v2.py <in> <out>  五维基础评分（腾讯前复权K线 + 东财资金 + akshare财务，约15分钟；
                                       权重读 factor_weights.json，缺失回退先验）
3. select_watchlist.py               → watchlist_<date>.json（观察池规则预筛120只 → LLM精选20~35只）
4. step2_stockscorer_v2.py <watchlist> <watch_scored>  观察池评分
5. generate_ai_assessment.py <scored> ai_assessment.json    LLM 盘面评估（失败自动规则兜底）
6. enhance_scores.py <scored> ai_assessment.json            基本面导数 + AI赋分增强
7. sector_momentum_daily.py          → sector/sector_momentum_<date>.json（申万二级板块动量）
8. apply_sector_momentum.py <scored> --mode evidence         并入第五维（主力池 + 观察池各一次）
9. pick_portfolio.py ... --per-parent 2 --min-total 12       A动量/B反转双路线选股
10. gen_portfolio_report.py                                  组合报告
11. factor_eval.py                                           Rank IC 因子有效性评估 → factor_ic.json
    （--optimize-weights 权重再拟合，由人工/Agent 周期性触发，不进每日自动链路）
12. gen_report_top200.py <scored> <out.html>                 主报告（含观察池区块）→ cp 为 index.html
```

自动化：`.github/workflows/daily-refresh.yml`（工作日 15:30 触发，带 3 次整体重试 + push rebase 重试）。

---

## 二·四、行业数据有效性（v2.46）

**历史问题**：`get_sec()` 原为名称静态字典（348 条），TOP200 中 30~40% 个股未命中而落入
`sector=综合`——题材热度被按中性模板评分、细分题材显示"综合/其他"（如大金重工实为**风电设备**）。
观察池输入自带的 `sector` 是申万二级名（如"化学制药"），THEME_DB 无此键，同样整体落入"综合"模板。

**修复**：以 `sector/sw_map.json`（申万二级，覆盖全市场 5218 只）为**权威源**：

```
resolve_sector(name, code) → (主题键, 申万二级名, 来源)
  ① code → 申万二级行业（sw_map）→ SW2THEME 映射到 THEME_DB 主题键（131 个二级行业全覆盖）
  ② 未命中 → 静态字典兜底 → STATIC2THEME 归一
  ③ 仍未命中 → 综合（语义上确为中性常规赛道：房地产/建筑/环保/贸易等）
```

- THEME_DB 补齐 13 个此前缺失的主题（电力设备/高端制造/航天军工/医药生物/金融/计算机软件/
  交通运输/传媒娱乐/汽车/消费/周期资源/农业/电力）
- 细分题材（`sub_theme`）无 SUB_THEME 条目时**优先展示真实申万二级名**（大金重工 → 风电设备，
  不再显示"综合/Other"；展示层清洗"Ⅱ"后缀）
- 周期股判定（`is_cyclic`）增加申万二级口径 `SW_CYCLICAL`，避免粗分类丢失周期属性
- 结果记录 `sector_src`（sw / static / fallback）供审计

**实测（2026-10-12 数据）**：`综合` 占比 **79 只（41%）→ 6 只（3%）**，189/195 走申万权威映射。

### 现价 vs 目标价并列对比（v2.46）

个股 S/A 级详情卡片在「个股信息旁」展示当前市场价格，并与两类目标价并列对比：

```
💰 现价 47.10 → 🎯 技术目标 48.63 ｜ 📐 价差 +1.53 ｜ 📈 预期 +3.25% ｜ 🏦 机构目标 55.00（上调）（较现价 +16.77%）
```

- **现价**：scored json 真实收盘价；**技术目标**：`build_trade_plan()` 按最近阻力/2R 推导（真实数据，非编造）
- **机构目标价**：来自本地复盘逐公司联网核查（`review_research_merged_<date>.json` 的 `target_price`），
  查不到为 null 则不展示——**严禁编造**
- 价差与预期涨跌幅按 A 股惯例红涨绿跌；AI 主观线组合同步展示该对比

---

## 二·五、因子有效性评估 · Rank IC（v2.46 → v2.47 口径修正）

`factor_eval.py` 从量化视角衡量"评分因子/推荐"的预测能力，产出 `factor_ic.json`，
报告层渲染「📐 因子有效性评估」区块。

**指标口径**

| 指标 | 定义 |
|---|---|
| Rank IC(t→t+h) | Spearman(因子值在 t 日截面秩, 个股未来 h 个**真实交易日**收益)，h ∈ {1, 5, 20} |
| IC 均值 / 标准差 | 因子平均预测能力 / IC 序列波动 |
| t 统计量 | **Newey-West HAC 调整**（Bartlett 核，lag=h−1）：h=5/20 相邻截面 IC 观测共享 80%/95% 收益窗口，普通 t 把 n 当独立样本会虚高显著性 |
| Bonferroni 校正 | 5 因子 × 3 周期 = 15 次检验，\|NW t\|≥2.97 记为 ★ 显著（宽松阈值 \|t\|≥2 仅作参考） |
| IC 胜率 | IC>0 的截面占比（方向一致性） |
| 分层多空 | 每截面按因子分 5 组，头部组均值收益 − 尾部组均值收益（检验单调性） |
| 子区间稳定性 | 半期拆分（前半→后半 IC 均值与方向一致率），检测制度依赖 |
| 因子间相关 | 跨截面平均 Spearman 相关矩阵，披露"五维"实际独立信息量 |

**评估对象**：① 五维因子全截面 IC（total / 题材热度 / 技术面 / 基本面 / 消息面 / 板块动量）
② 推荐组合层 IC（`recommendation_track.json` 中当日推荐的 total 与未来收益）。

**样本与数据来源**：`factor_history.json` 逐日追加紧凑截面快照（约 22KB/日）；
本地首次用 `python3 factor_eval.py --bootstrap-from-git` 从 **git 提交历史**引导
（历史 scored json 虽被 prune 出工作区，但 blob 仍在 git 历史中；Actions checkout 默认
depth=1 无历史，故历史文件随仓库分发）。

**v2.47 口径修正（三大缺陷，修正后结论反转）**：

| 缺陷 | v1 口径问题 | 修正方式 |
|---|---|---|
| 非交易日陈旧快照 | 按快照下标 i+h 假设连续交易日；假日/接口陈旧快照污染索引（09-10 快照价格实为 09-09 旧值） | 快照日期必须是真实交易日（腾讯日线 dates 即交易日序列）；价格与上一快照 ≥80% 相同的判为接口陈旧并剔除（实测剔除 7 个，有效截面 69 个） |
| 留存偏差 | 只对"仍能在 TOP200 找到"的个股算未来收益，跌出榜单即消失（T+20 流失 34% 且非随机） | 回填 **709 只 × 143 个交易日前复权日线**（`--bootstrap-prices`，2 分 23 秒 709/709 成功），收益对 t 日全部 TOP200 个股计算 |
| 重叠窗口 t 虚高 | h=20 相邻观测共享 95% 数据，普通 t 高估显著性 | Newey-West HAC + Bonferroni 双阈值披露 |

修正前后对比（69 有效截面，2026-06-26~10-12）：v1 曾判「综合总分/消息面负向」，
修正后**总分全周期为正**（+0.024/+0.027/+0.053）、**基本面与消息面三周期全正**、
题材（−0.041~−0.094）与技术（−0.035~−0.062）为负向。

**有效性判定规则**：样本 < 20 个截面 → "样本积累中"；否则按 |IC| 与 |NW t| 分档
（显著有效 / 弱有效 / 方向为正·不显著 / 负向·可作反向指标 / 无预测力），
综合判定取 |t| 最大的周期（不同因子有效周期不同，如基本面在 T+20 显著而 T+1 不显著）。

> ⚠️ 报告内解读须知：本名单为「当日成交额 TOP200」，高总分个股往往是刚放量上涨的动量股，
> 5~20 日维度的负 IC 更可能是**动量回落/均值回归**与**样本筛选偏差**；IC 衡量排序能力而非绝对收益，
> 未扣交易成本。

---

## 二·六、本地深度复盘链路（v2.45，无需 LLM 密钥）

上面的 Actions 链路在 15:30 先产出一版页面，其「盘面复盘点评 / 关注方向 / 组合」在未配置
云端 Secrets 时由**规则引擎**拼接兜底。本地复盘链路在其之上做**逐公司联网核查覆盖**：
把兜底点评换成真实的逐公司核查结论，全程**不调用在线大模型**。

```
① Actions 产出 top200_scored_<date>.json
② ./run_local_review.sh prepare          阶段0  导出候选公司 + 生成待填模板
③ 【Agent】逐公司联网查资料 → 填写 review_research_<date>.json    阶段1
④ ./run_local_review.sh finalize         阶段2  校验汇总 → 阶段3 注入页面
⑤ ./run_local_review.sh push             提交推送
```

**阶段0** `export_review_input.py` —— 理出「当日需要复盘的公司代码」，四类来源去重：

| 来源 | 说明 | 复盘深度 |
|---|---|---|
| 主力池 S/A 级 | 评级最高，买入决策依赖它 | deep |
| 前向组合标的 | momentum / reversal 两条量化线实际持仓 | deep |
| 观察池精选 A/B 级 | 200~1000 名中筛出的次日候选 | light |
| 主力池异动股 | 涨跌幅 \|x\| ≥ 9.5% 但未落入上面三类 | light |

产出 `review_input_<date>.json` 含市场画像、板块/题材聚合、每只候选的 `focus[]`（按真实字段
推导的「该查什么」规则清单，非结论）。

**阶段2** `merge_review_research.py` —— 汇总逐公司资料并**校验 9 条规则**，防编造与张冠李戴：

| 规则 | 内容 | 级别 |
|---|---|---|
| E1 | date 与候选清单一致 | error |
| E2 | deep 档候选 100% 覆盖 | error |
| E3 | code 不得越界（编造代码） | error |
| E4 | code→name 与评分产物一致 | error |
| E5 | verdict / findings 必填 | error |
| E6 | deep 档至少 1 条含 http(s) URL 的来源 | error |
| E7 | price / pct_chg 与评分产物一致（容差 0.5%） | error |
| E8 | verdict 枚举合法 | warn |
| E9 | findings 长度下限（deep≥80字 / light≥30字） | warn |

**阶段3** `generate_ai_assessment.py --from-review` —— 装配 `ai_assessment.json`：

- `source = "local-review"`、`fallback = False`，与「在线 LLM 生成」「规则引擎兜底」严格区分
- 题材热度 = 当日真实均涨幅基准 + 逐公司 verdict 修正（上调 +0.6 / 维持 +0.1 / 下调 −0.6 / 规避 −1.0）
- **含核查个股的题材 100% 保留**（否则深度核查的公司落不进 themes，enhance_scores 无法命中，本地结论白做）
- 组合 = 深度复盘且 verdict 为「维持/上调」的标的，附核查结论与机构目标价
- `review_meta` 记录核查规模与校验状态，页面「🔍 本地复盘过程」区块如实展示

**与 Actions 的协同（重要）**：本地复盘会改变评级分布，故 `track_recommendations.py` 在
本地链路中以 `--replace-day` 运行——**替换**当日推荐快照而非追加，避免同日出现两套推荐
导致样本虚增、胜率污染。

---

## 二·七、IC 驱动权重优化与数据新鲜度守卫（v2.47）

### 权重优化方法（`factor_eval.py --optimize-weights`）

以 Rank IC 实证反向配置五维权重，核心设计防过拟合：

```
① 仅 IC>0 的维度按 IC 比例分配；IC≤0 的维度降至地板 5%（保留下限维持五维结构）
② 向先验收缩：w = λ·w_IC + (1−λ)·w_prior，λ 由走步验证选择
③ 走步验证：训练窗≥30 截面、每 5 截面重拟；训练窗因子 IC 仅用 T+1 收益完整落在
   训练窗内的观测（max_base_idx 防边界泄漏）；样本外与先验【同日期集合】公平比对
④ 样本外复合 IC 提升 >0.005 才采用，否则维持先验
⑤ score_sector 有效截面<20（2026-09-21 起入库），权重维持 0.12，随积累后评估
```

**优化结果（2026-10-13，69 有效截面）**：

| 指标 | 先验权重 | IC 驱动权重 |
|---|---|---|
| 消息面 | 0.20 | **0.396**（T+1 IC +0.049） |
| 基本面 | 0.30 | **0.396**（T+1 IC +0.049） |
| 技术面 | 0.20 | **0.044**（T+1 IC −0.035，地板） |
| 题材热度 | 0.18 | **0.044**（T+1 IC −0.041，地板） |
| 板块动量 | 0.12 | 0.12（样本积累中） |

- **样本外走步验证**：复合总分 IC 由 **+0.0248 → +0.0445**（t=2.22，38 个样本外截面，λ=1.0）
- 全周期复合 IC 三周期全部改善：T+1 +0.033→+0.054（NW t 3.44）、T+5 +0.043→+0.075（NW t 3.63）、
  T+20 +0.071→+0.099（NW t 3.08，胜率 80%）
- λ 扫描（0~1.0 共 6 档）单调：0.0248→0.0445，全部 λ≥0.2 均优于先验——"IC 驱动优于先验"的
  结论对 λ 选择不敏感（选择性披露：λ 扫描本身构成对样本外的多次使用，表中样本外值为乐观上界）
- 子区间稳定性：两半期均正向（T+1 +0.056/+0.053，T+20 +0.140/+0.057），无制度切换
- 产出落档 `factor_weights.json`（version=v2.41-ic），step2 / apply_sector_momentum 启动读取

> **为何技术/题材是负向却只降权不删除**：TOP200 是高换手关注度筛选池，负 IC 反映「关注度
> 脉冲后的均值回复」而非因子完全无效；地板 5% 保留下限以维持五维结构与展示完整性。
> 这与独立因子回测工程结论互证（RSI<30 正超额、动量类负 α）。

### 数据新鲜度守卫（`check_data_fresh.py`，workflow Step 1.5）

| 检查 | 方式 | 失败行为 |
|---|---|---|
| 指数日线日期 | 新浪 `sh000001` 日线最后一根日期 < 北京当日交易日 | exit 2，workflow 重试 3 次后失败（`result=stale_data`） |
| 价格陈旧率 | 新 `top200_all_a.json` 与上一 scored json 快照价格比对，≥80% 相同判为接口陈旧 | exit 3，同上 |

防止节假日/接口故障时把陈旧数据当"当日数据"评分发布（09-10 曾出现快照价格实为 09-09 旧值）。
Step 1.5 设为**可重试**（30s×3）：收盘后指数日线偶有几分钟延迟，重试通常恢复；真假日则干净失败，
由 17:30 补偿 cron 或次日任务兜底。

> **已知限制（如实披露）**：守卫采用「数据日期必须 = 运行日期」的严格语义——若 schedule 触发被
> 排队延迟跨午夜（周五 15:30 拖到周六执行），该 run 会被拒而非发布错标报告。代价是延迟跨午夜的
> run 当天缺报（下一个交易日恢复正常）；主触发路径（WorkBuddy 15:40 `workflow_dispatch`）
> 不走 schedule 队列，正常情况不受影响。根治需让全链路按"数据真实日期"标注
> （fetch 的 `date` 字段与 step2 输出名目前取运行时钟），属独立重构项，需全链路验证后单独上线。

---

## 三、数据源与容错

| 用途 | 主源 | 备源 |
|---|---|---|
| 成交额排行 | 新浪财经全市场接口 | akshare 东财快照 |
| 前复权K线 | 腾讯 `ifzq.gtimg.cn`（多域名轮询） | 东财 push2his → 新浪日线（不复权） |
| 资金流 | 东财 push2 | — |
| 财务 | akshare | — |
| 板块映射 | `sector/sw_map.json`（申万二级，`fetch_sw_map.py` 周更） | — |

> ⚠️ **腾讯K线域名**：`web.ifzq.gtimg.cn` 会被 WAF 拦截，脚本按 `ifzq.gtimg.cn` → `web.ifzq.gtimg.cn` → `proxy.finance.qq.com/ifzqgtimg` 轮询，并有新浪兜底。

---

## 四、数据纪律

- 所有数字来自真实接口（新浪/腾讯/东财/akshare），**严禁编造**。
- LLM 输出的 `themes[].stocks` 限定在当日 TOP200 在榜名单内，脚本自动清洗名单外个股。
- 生成后抽查模型引用的个股涨跌幅与 scored json 实际值一致性。
- **AI 点评日期校验**：报告只采纳与当日数据日期一致的点评文件（`ai_assessment.json` / 回退文件），日期不匹配则跳过并回退模板文本，防止陈旧点评冒充当日（历史 `ai_review_today.json` 已停用）。
- **本地复盘三态来源标注**：页面点评来源如实区分为「本地复盘（逐公司联网核查）」/「AI 生成」/「规则引擎生成」，并附生成者串可追溯，不得混淆。
- **复盘资料九条校验**（见第二·五节）：code 越界、名称张冠李戴、数值与评分产物不符、无来源
  均判 error 并中止，从机制上阻断编造入库。
- **目标价纪律**：机构目标价仅来自本地复盘逐公司联网核查所得，查不到必须留空（null），
  不得为凑展示而估算；技术目标价由 K 线结构真实推导并标注推导口径。
- **行业归属纪律**：行业以申万二级映射（`sector/sw_map.json`）为权威源，静态字典仅兜底；
  解析来源记入 `sector_src` 供审计，禁止把未匹配个股一律归入"综合"了事。
- **权重纪律**：五维权重只能由 `factor_eval.py --optimize-weights` 经样本外走步验证
  （提升 >0.005）后写入 `factor_weights.json`，禁止手工拍脑袋改权重；脚本读取时校验
  字段齐全/非负/Σ≈1，失败自动回退先验。
- **口径诚实纪律**：IC 的 t 值必须 Newey-West HAC 调整 + Bonferroni 校正披露；
  选择性披露（λ 扫描）必须在页面注明；陈旧/非交易日快照必须剔除，不得混入截面。

---

## 五、文件保留策略

为避免仓库体积膨胀，**仅保留以下文件**（其余历史文件由工作流 `prune` 步骤每日清理）：

- 最新 1 份主报告 HTML（`top200_report_<最新日期>.html` + `index.html`）
- 最新 **2 份**主力池评分 JSON（`gen_report_top200.py` 依赖上一份做评级变化追踪）
- 最新 1 份观察池评分 / watchlist / 组合 JSON / 组合报告 HTML
- 最新 1 份板块动量 + `sector_momentum_latest.json` + 静态映射 `sw_map.json` / `backtest_ref.json`
- `factor_history.json`（Rank IC 逐日截面快照 + 前复权价格回填，约 22KB/日，**只增不改**）
  + `factor_ic.json`（最新评估结果）+ `factor_weights.json`（IC 驱动权重决策记录）

> **不入库文件**：`refresh_log.txt`（CI 日志改由 `upload-artifact` 工件保留 30 天）、`ai_review_today.json`（已停用）、`upload826.py`（早期手动上传工具，已被 workflow `git push` + Pages 自动部署取代）。

---

## 六、本地运行

```bash
# 依赖
pip install -r requirements.txt

# LLM 凭据（本地 .llm_env，已在 .gitignore 忽略；CI 用 GitHub Secrets）
#   LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

# 全链路
python3 fetch_top200_sina.py && python3 step2_stockscorer_v2.py top200_all_a.json top200_scored_$(date +%F).json
# ... 详见第二节
```

> **云端 LLM 配置**：GitHub 仓库需配置 Secrets `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`，否则云端自动任务将走规则引擎兜底（`generated_by` 会显示 fallback）。

> **推送提示**：若环境存在不可用代理，推送前需 `unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy` 走直连。

---

## 七、目录结构

```
├── index.html                     # 线上主报告（最新）
├── top200_report_<date>.html      # 当日主报告
├── top200_scored_<date>.json      # 主力池五维评分（保留最新2份）
├── top200_scored_watch_<date>.json# 观察池评分
├── watchlist_<date>.json          # 观察池 AI 筛选结果
├── ai_assessment.json            # AI 盘面评估层
├── factor_history.json          # Rank IC 逐日截面快照 + 前复权价格回填（只增）
├── factor_ic.json               # 因子有效性评估结果（最新）
├── factor_weights.json          # IC 驱动评分权重（v2.41-ic，走步验证落档）
├── check_data_fresh.py          # 数据新鲜度守卫（workflow Step 1.5）
├── sector/
│   ├── sw_map.json               # 申万二级行业映射（静态）
│   ├── backtest_ref.json         # 板块动量回测依据（静态）
│   ├── sector_momentum_latest.json
│   ├── portfolio_<date>.json
│   └── portfolio_report_<date>.html
├── *.py                          # 各环节脚本
└── .github/workflows/daily-refresh.yml
```
