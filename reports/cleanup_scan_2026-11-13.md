# 本地文件清理候选扫描报告 · 2026-11-13

> **状态：只读扫描（scan-only）。本轮未移动、重命名、删除任何既有文件。**
> 唯一新增文件是本报告。除非老大明确圈定并确认，否则不对清单内任何文件采取行动。

## 0. 结论摘要

| 项 | 实测 |
|---|---|
| 项目总体积 / 文件数（排除 `.git` 内部） | **6.78 GB / 13,193 个文件** |
| git 跟踪文件 | **614 个**（`git status --short` 为空，HEAD `42ae477`，与 origin/main 同步） |
| 未跟踪本地文件 | ≈12,579 个 —— **「未被 git 跟踪」≠ 无效**，`.gitignore` 第 8 行忽略 `*.json`，而白名单注释明确写着「代码逻辑的直接输入，缺了会静默失效」 |
| 项目自带孤儿扫描（只读） | 根目录 214 个 JSON：**referenced 212 / guarded 2 / orphan 0**（昨日日更已跑过 `--apply`） |
| 最大可释放空间 | B 类备份/快照 ≈ **2.79 GB** + 分片中间件 ≈ **0.93 GB** |
| A 类无风险垃圾 | ≈ **28.8 MB**（pyc/缓存/系统残留/遗留 node_modules） |

**核心判断**：真正「无风险」的垃圾只有约 29 MB；体积最大的 3.7 GB 全部是**改脚本前留证快照、历史归档与研究证据**，删除会破坏「过程可迁、披露不可迁」的可复核链条。因此按 A/B/C 三级处置，不按体积直接删。

## 1. A 类 · 明确无信息量的机器产物（建议可清理，约 28.8 MB）

| 路径 / 模式 | 数量 | 体积 | 判据 |
|---|---|---|---|
| `__pycache__/` + 各处 `*.pyc` | 300 | 8.2 MB | Python 自动重建；`.gitignore:1-2` 已忽略 |
| `node_modules/` | 1,856 | 20.6 MB | 无 `package.json`、全项目源码零引用、最后修改 2026-10-31 → 遗留前端依赖 |
| `zmxlky3r` / `z8h61j_5` / `ywd73zn8` | 3 | 12 B | `.gitignore:102-105` 明确标注「blat 测试残留（4 字节 ASCII，非交付物）」 |
| `.admin_server.log` | 1 | 0 B | 空文件 |
| `archive/git_backup/index.lock.stale_20261103` | 1 | 0 B | 空文件，陈旧锁残留 |
| `.DS_Store` | 1 | ≈6 KB | macOS 系统文件，`.gitignore:6-7` 明确不入库 |
| `.piptmp/` | 空目录 | 0 B | pip 临时目录，`.gitignore:69` 忽略 |

## 2. B 类 · 体积大但带留证价值（**必须裁决，不得擅动**）

### 2.1 改脚本/改数据前的备份快照：189 个 / **2,791.3 MB**（占项目 41%）

按日期分布：11-04 一天即 **117 个 / 2,331.1 MB**；11-03 25 个 / 177 MB；11-09 5 个 / 111.3 MB；其余分散。项目铁律要求「跑前留证 `.snap_*`」，`.gitignore:79-89` 也把它们定义为「本地留证快照，属操作痕迹不是交付物」——两者并存，故需明确取舍（例如「只保留最近 7 天」）。

体积最大的逐项清单（合计约 2.5 GB）：

| 大小 | 日期 | 路径 |
|---|---|---|
| 951.8 MB | 11-04 | `data/panel_round124_long_pre_round128.json.bak` |
| 895.4 MB | 11-04 | `.snap_panel_r124_before_delist.json` |
| 122.6 MB | 11-04 | `.snap_panel_round123_before_covfix.json` |
| 121.9 MB | 11-03 | `.snap_panel_round123_before_revfix.json` |
| 82.2 MB | 11-04 | `data/panel_r124_mat_pre_round128.npz.bak` |
| 74.5 MB | 11-04 | `.snap_mat_before_delist.npz` |
| 56.5 MB | 11-04 | `.snap_klines_2020_before_qfqfix2.npz` |
| 53.5 MB | 11-04 | `.snap_klines_2020_before_delist.npz` |
| 53.5 MB | 11-04 | `.snap_klines_2020_before_qfqfix.npz` |
| 33.7 MB | 11-10 | `.snap_1111_klines_before.json` |
| 33.6 MB | 11-09 | `.snap_1110_klines_before.json` |
| 33.5 MB | 11-09 | `data/klines_long.json.snap_1109_before` |
| 33.4 MB | 11-09 | `data/klines_long.json.snap_round136_mid` |
| 33.3 MB | 11-05 | `data/klines_long.json.snap_round136_before` |

另有 23 个 `.snap_*/snap_*` **目录**（80.7 MB），多为 11-04 的 `gen_report_top200.py.bak` / `step2_stockscorer_v2.py.bak` 连续版本备份（实测 16 份 0.8 MB 文件内容完全相同）。

### 2.2 可再生但有重建代价

| 路径 | 体积 | 判据 |
|---|---|---|
| `data/panel_shards/`（6 个分片） | 952.0 MB | 仅 `build_panel_sharded.py` 写入并读回合并，合并产物 `data/panel_round124_long.json` 已存在；日链与 workflow 均不调用，仅 `reports/round124_long_h20_2026-11-04.md` 提及。理论可再生，但重跑 1653 天分片构建代价高 |
| `data/klines_2020_cache_delist_bad/`（239） + `data/fin_cache_em_delist_bad/`（243） | 16.2 MB | 全项目源码**零引用**，命名即「退市脏数据隔离」；11-04 修复留证 |

### 2.3 日志与归档

| 路径 | 体积 | 判据 |
|---|---|---|
| `*.log` 44 个 | 3.0 MB | 10-21 ~ 11-12 本地运行日志；`.gitignore:46` 注释写明「本地运行日志不入库，CI 日志走工件保留 30 天」。11-12 的 `step2_1112.log` / `factor_eval_1112.log` / `long_panel_ic_1112.log` 是昨日日更留痕，建议保留近 7 天 |
| `archive/` 133 个 | 325.0 MB | `pruned/` 158.4 MB、`dedup_20261105_002001/` 33.5 MB、`restore_20261104/` 12.0 MB、`fin_cache_full_20261028.tar.gz` 91 MB、`orphan_json/` 15 个。**`orphan_scan.py` 只移到这里、明确「孤儿不等于可删」** → 禁动 |

### 2.4 「重复文件」不能按重复率删

实测同尺寸且内容完全相同的 13 组（top12 合计可省 82.3 MB）：绝大多数是 `archive/dedup_*` 与 `archive/pruned/` 的成对备份、`.snap_admin_*` 的连续版本备份；`index.html` 与 `top200_report_2026-11-12.html` 完全相同是**设计**（主页即当日报告副本，sha 已在日更验收中核对），禁止删任一。

## 3. C 类 · 禁止动（生产输入 / 披露证据 / 凭据）

| 类别 | 代表路径 | 依据 |
|---|---|---|
| git 跟踪文件 | 全部 614 个 | 删除即破坏仓库一致性 |
| 白名单数据输入 | `config.json`、`factor_weights.json`、`factor_ic.json`、`factor_history.json`、`ai_assessment.json`、`sector/cycle_industry.json`、`data/trial_log.json` 等 | `.gitignore:10-39` 注释：「代码逻辑的直接输入，缺了会静默失效」 |
| 活跃缓存 | `data/fin_cache_em/_cache.pkl`(211.8 MB，被 `fund_split.py:102` 读)、`_all_cache.pkl`(211.8 MB，被 `ep_size_ind.py:55` 读，两者 sha 不同)、`data/fin_cache/`(3,303 文件 / 42.9 MB，79 处引用)、`data/klines_2020_cache/`(2,257 文件 / 213 MB) | 长面板 IC 重建的 `--skip-cached` 幂等命中依赖；删了要全量重抓 |
| 生产主数据 | `data/panel_round124_long.json`(951.6 MB，15 处引用)、`data/panel_r124_mat.npz`、`data/klines_long.json`(33.9 MB)、`data/fwd_hist/`(100 日)、`data/fund_universe/`、`data/bench/` | 日更链直接读写 |
| 披露 / 审计证据 | `reports/round*.md`(66 个)、`top200_report_*.html`、`analysis_*.html`、`audit_round*.json`、`test_*.py`(405 项单测) | 「过程可迁、披露不可迁」；`verify_round142.py` 与单测读这些产物 |
| 229 个「零字面引用」的 `.py` | `audit_round11~140.py`、`apply_round*.py`、`_probe_*.py`、`artifact_scope.py` 等 | **不能凭引用数判无效**：它们是 `reports/round*.md` 里每个数字的可复核来源，且大量通过一次性手动调用、动态 import 使用。`orphan_scan.py` docstring 明确记录过静态 grep 首版误判 95/202 的事故 |
| 运行时 / 凭据 / VCS | `.git/`、`.workbuddy/`、`.llm_env`、`.github/workflows/daily-refresh.yml`、全部定时任务的落点 | 删除会导致不可恢复损失 |

## 4. 若获确认，执行时遵守的纪律

1. **先备份**：对每批目标先 `cp -r` 到独立备份位置并核验成功，再动原件。
2. **只进废纸篓，不 `rm`**：macOS 用 `osascript`/`trash` 机制；项目已有的 `orphan_scan.py --apply`（只移不删，落到 `archive/orphan_json/`）优先于系统废纸篓。
3. **小批量**：每批 ≤10 个文件，逐批核验；任一批失败立即停止。
4. **不碰 C 类**，不改 `.gitignore`、不动权重 / 内核 / 日链 / 定时任务。
5. 执行前后各跑一次 `python -m unittest discover` 与 `verify_round142.py`，确认链路未受损；结果如实记录。

## 5. 待老大确认的处置选项

| # | 选项 | 预估释放 | 保留的风险 |
|---|---|---|---|
| ① | 只清 A 类 | ≈29 MB | 几乎为零（推荐先做） |
| ② | ① + 清理 11-04 及更早的 bak/snap（保留最近 7 天） | ≈2.4 GB | 失去 11-04 退市/round128 修复的原始对照 |
| ③ | ② + 清理 `panel_shards` 与两个 `delist_bad` | ≈3.4 GB | 长面板分片需重跑才能重建；脏源对照丢失 |
| ④ | 全部保留，仅出清单 | 0 | 无 |

**默认建议：① + ② 中的「先移入 `archive/orphan_snapshots/` 而非删除」**，可释放约 2.4 GB 且保留可回退路径；`panel_shards` 建议保留（重建代价高）。

## 6. 勘误（2026-11-13 执行时实测，原文保留不改）

上一行「可释放约 2.4 GB」**口径错误**：归档目录位于同一项目、同一卷内，`mv` 不删除数据块，不释放任何磁盘空间；实际只是把 2.63 GB 从活跃区搬进归档区，项目体积实测 `7.0G → 6.9G`（减少的 30 MB 来自 A 类移入废纸篓）。真正回收空间需另行授权「压缩后移除原件 / 移出到外部卷 / 清空废纸篓」。执行明细与核验见 `reports/cleanup_exec_2026-11-13.md`。
