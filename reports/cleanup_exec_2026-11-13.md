# 本地文件清理执行记录 · 2026-11-13

> 依据：老大裁决「根据默认建议来处理」= `reports/cleanup_scan_2026-11-13.md` 第 5 节默认建议
> **① 清 A 类 + ② 把 11-04 及更早的 bak/snap 移入 `archive/orphan_snapshots/`，而非删除**；`panel_shards` 保留。
> 执行纪律：先备份 → **只移不删，全程未使用 `rm`** → 每批 ≤10 项并逐批核验 size / 文件数 → 前后各跑一次单测与验收脚本。

## 1. 实际处置

| 类别 | 条目 | 体积 | 去向 | 动作 |
|---|---|---|---|---|
| 旧快照（mtime ≤ 2026-11-04） | **60 项 / 60 项全部成功** | **2634.51 MB** | `archive/orphan_snapshots/20261113_default/old_snapshots/<原相对路径>` | `mv`，6 批 ×10，逐批核验 |
| A 类机器产物 | **8 项 / 2161 文件** | **30,185,148 B（30.19 MB）** | 原件 `~/.Trash/stock-top200-A-20261113_default/`，备份同目录 `_backup/` | 先 `cp -r` 核验，再移入 macOS 废纸篓 |

A 类明细：`node_modules/`(1856 文件 / 21.57 MB)、`__pycache__/`(300 文件 / 8.60 MB)、`zmxlky3r`/`z8h61j_5`/`ywd73zn8`(各 4 B)、`.admin_server.log`(0 B)、`.DS_Store`(8196 B)、`.piptmp/`(空目录)。

归档清单（可回退索引）：`archive/orphan_snapshots/20261113_default/manifest.json`，逐项记录 `src / dst / size / nfiles / mtime / batch`，共 77 条记录。

## 2. 主动排除（未动）

| 项 | 体积 | 理由 |
|---|---|---|
| `top200_scored_2026-11-02.json.snap_r108_before` | 12.78 MB | `compare_round108.py:18` **实际读取**，一次性对照脚本的复核来源 |
| `.snap_index_before_admin.html` | 8.51 MB | `admin_server.py:439` 写入目标，保留避免干扰活体脚本 |
| `.snap_round128_pre_c1/` | 1.57 MB | `apply_round128_c1.py:43` 字面引用 |
| `archive/git_backup/index.lock.stale_20261103` | 0 B | 位于 `archive/` 保护域（报告 2.3 明定禁动） |
| 11-05 及以后全部 bak/snap（21 项） | ≈290 MB | 「保留最近 7 天」口径，比报告阈值更保守（多留 11-05/11-06） |
| `data/panel_shards/` 952 MB、两个 `_delist_bad` 16.2 MB、44 个 log、`archive/` 既有 325 MB、全部 C 类 | — | 默认建议明确保留 |

**未触碰**：git 跟踪的 614 个文件、权重 `factor_weights.json`、唯一内核、日链脚本、`.gitignore`、定时任务、`reports/round*.md` 披露证据、229 个一次性研究脚本。**未执行任何 commit / push。**

## 3. 执行前后核验（同口径对比）

| 检查项 | 执行前 | 执行后 | 判定 |
|---|---|---|---|
| `python -m unittest discover -p "test_*.py"` | Ran 405 tests / **OK** | Ran 405 tests / **OK** | 一致 |
| `verify_round142.py` | **18/22 PASS，4 FAIL** | **18/22 PASS，4 FAIL**（同 4 项） | 一致 |
| 权重 sha256 | `9347d48ff91c41acc…` | `9347d48ff91c41acc…` | 未变 |
| `kernel_id` | `1b5770b7152d14a6` | `1b5770b7152d14a6` | 未变 |
| `orphan_scan.py`（只读） | 214 json / ref 212 / guard 2 / orphan 0 | 214 / 212 / 2 / **0** | 一致 |
| git | HEAD `42ae477`，仅 `?? reports/cleanup_scan_2026-11-13.md` | 同左 | 未变 |
| 关键活体文件 | 8 件齐全 | 8 件齐全（`panel_round124_long.json` 997.8 MB、`index.html` 8.37 MB 等） | 未变 |

`verify_round142.py` 的 4 个 FAIL 是**已知的一次性快照基线过期**（工作区存在未跟踪报告、HEAD 已推进到 `42ae477`、主页已发布 11-12 报告），执行前后完全相同，与本次清理无关；未擅自修改该脚本的基线。

## 4. ⚠️ 勘误：默认建议并未真正释放磁盘空间

原扫描报告第 105 行写「可释放约 2.4 GB」——**该口径有误，在此更正**：

- 归档目录 `archive/orphan_snapshots/` **仍在同一项目、同一卷内**，`mv` 只改目录项、不复制也不删除数据块 → 2.63 GB 从「活跃区」搬进「归档区」，**项目总体积与磁盘占用都不变**。
- 实测：项目 `du -sh` 由 `7.0G` → `6.9G`，减少的只有 A 类移出项目的 **30 MB**；磁盘可用量 147Gi → 144Gi（系统波动，非本操作所致，不做归因）。
- 因此本轮的真实成果是：**活跃区清理 + 100% 可回退的留证归档**，不是空间回收。

若老大确需回收空间，需**另行授权**以下任一动作（本轮均未执行）：
1. 将 `old_snapshots/` 压缩为 tar.gz 后移除未压缩原件（JSON/NPZ 压缩率需实测，预计可省 1~2 GB）——涉及「移除原件」，超出「只移不删」纪律；
2. 把整个归档目录迁出项目到外部卷 / 云端归档；
3. 清空 macOS 废纸篓（仅能再回收 A 类的 30 MB）。

## 5. 回退方法

- 旧快照：按 `manifest.json` 中 `action=old_snapshot_moved` 的 `src` / `dst` 反向 `mv` 即可原位还原（路径结构与原相对路径一一对应，无覆盖、无同名冲突）。
- A 类：从 `~/.Trash/stock-top200-A-20261113_default/` 拖回项目根目录；`_backup/` 是第二份副本。两类文件本身均可自动/按需重建。
- **回退前请勿清空废纸篓**；归档目录已被 `.gitignore:91` 的 `archive/` 规则忽略，不影响 git 工作区。
