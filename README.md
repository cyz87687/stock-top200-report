# A股成交额 TOP200 复盘评分系统

> 基于当日真实行情数据，对全市场成交额 TOP200 主力池 + 200~1000 名观察池进行**五维加权评分**、AI 复盘评估与组合推荐，每日收盘后自动生成单页 HTML 报告并发布至 GitHub Pages。

**线上报告**：https://cyz87687.github.io/stock-top200-report/

| 项目 | 值 |
|---|---|
| 报告层版本 | **v2.45** |
| 评分模型版本 | **stock-scorer v2.40**（五维 · AI 增强 · 含板块动量） |
| 数据日期 | 见线上报告页眉（最新交易日） |
| 主力池 | 全A成交额 TOP200 |
| 观察池 | 全A成交额 200~1000 名（800 只，每日 AI 精选需复盘标的） |
| 自动更新 | 工作日 15:30（北京时间）via GitHub Actions |
| 本地深度复盘 | 收盘后由 `run_local_review.sh` 编排，逐公司联网核查覆盖页面结论 |

---

## 一、评分模型（v2.40 五维）

总分按其五个维度的加权和计算，再放大 4 倍：

```
total = ( 消息面×0.20 + 技术面×0.20 + 基本面×0.30 + 题材热度×0.18 + 板块动量×0.12 ) × 4
```

| 维度 | 权重 | 说明 |
|---|---|---|
| 基本面 | 0.30 | 基础分×0.6 + 业绩一阶导(增速)×0.2 + 业绩二阶导(growth−cagr3)×0.2 |
| 消息面 | 0.20 | AI 赋分 0.7×LLM + 0.3×算法 |
| 技术面 | 0.20 | 均线/RSI/趋势等 |
| 题材热度 | 0.18 | AI 赋分 + 当日涨跌修正 `day_mod = clamp(pct_chg/10, -1, +1)` |
| 板块动量 | 0.12 | 申万二级板块 60日/10日动量排名（0.6×R60档位 + 0.4×R10档位） |

**评级**：S≥17 ｜ A 13~17 ｜ B 9~13 ｜ C 5~9 ｜ D <5

> **板块动量赋分方向**：`apply_sector_momentum.py --mode evidence`（默认）依据 2021–2026 回测证据——申万二级板块 R60 与后续 20 日超额呈负单调，故动量末段得分更高；`--mode momentum` 为用户原始思路（动量越强分越高）。

---

## 二、每日数据链路（严格按序）

```
1. fetch_top200_sina.py              → top200_all_a.json（主力池TOP200）
                                       + top1000_all_a.json（观察池200~1000名）
2. step2_stockscorer_v2.py <in> <out>  五维基础评分（腾讯前复权K线 + 东财资金 + akshare财务，约15分钟）
3. select_watchlist.py               → watchlist_<date>.json（观察池规则预筛120只 → LLM精选20~35只）
4. step2_stockscorer_v2.py <watchlist> <watch_scored>  观察池评分
5. generate_ai_assessment.py <scored> ai_assessment.json    LLM 盘面评估（失败自动规则兜底）
6. enhance_scores.py <scored> ai_assessment.json            基本面导数 + AI赋分增强
7. sector_momentum_daily.py          → sector/sector_momentum_<date>.json（申万二级板块动量）
8. apply_sector_momentum.py <scored> --mode evidence         并入第五维（主力池 + 观察池各一次）
9. pick_portfolio.py ... --per-parent 2 --min-total 12       A动量/B反转双路线选股
10. gen_portfolio_report.py                                  组合报告
11. gen_report_top200.py <scored> <out.html>                 主报告（含观察池区块）→ cp 为 index.html
```

自动化：`.github/workflows/daily-refresh.yml`（工作日 15:30 触发，带 3 次整体重试 + push rebase 重试）。

---

## 二·五、本地深度复盘链路（v2.45，无需 LLM 密钥）

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

---

## 五、文件保留策略

为避免仓库体积膨胀，**仅保留以下文件**（其余历史文件由工作流 `prune` 步骤每日清理）：

- 最新 1 份主报告 HTML（`top200_report_<最新日期>.html` + `index.html`）
- 最新 **2 份**主力池评分 JSON（`gen_report_top200.py` 依赖上一份做评级变化追踪）
- 最新 1 份观察池评分 / watchlist / 组合 JSON / 组合报告 HTML
- 最新 1 份板块动量 + `sector_momentum_latest.json` + 静态映射 `sw_map.json` / `backtest_ref.json`

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
├── sector/
│   ├── sw_map.json               # 申万二级行业映射（静态）
│   ├── backtest_ref.json         # 板块动量回测依据（静态）
│   ├── sector_momentum_latest.json
│   ├── portfolio_<date>.json
│   └── portfolio_report_<date>.html
├── *.py                          # 各环节脚本
└── .github/workflows/daily-refresh.yml
```
