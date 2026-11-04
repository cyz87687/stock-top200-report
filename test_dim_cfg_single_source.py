"""test_dim_cfg_single_source.py —— 守卫「dim_max 单一来源」铁律。

round125 血案：8 个分析脚本各自硬编码 DIM_MAX，且全部漏 `score_fund: 5.0`
（因那些方案里 fund 权重=0，"看不见"它）。漏项不报错，只在某天给 fund 非零权重时
把 fund 按 10.0 量程归一 → 分数被压半，静默错。

本测试守三件事：
  T1  config 文件里的 dim_max 与 dim_cfg._SAFETY_DEFAULT **逐项相等**
      （兜底表漂移 = 配置不可读时静默回退旧口径）
  T2  dim_cfg.load_dim_max() 读出来的值 == 配置文件里的值（没被兜底表盖掉）
  T3  **全仓扫描**：不允许任何 .py（除 dim_cfg.py 自身 / step2 的既有默认表 /
      测试文件）再出现 `DIM_MAX = {` 硬编码字面量。

T3 是这条铁律真正的执法者 —— 它会在下次有人图省事再抄一份时立刻失败。
"""
import ast
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dim_cfg  # noqa: E402


# 允许保留字面量 DIM_MAX 的文件（必须有正当理由，新增须在此登记 + 写原因）
_ALLOWLIST = {
    # dim_cfg.py 自身：它就是那个「单一来源」，兜底表必须写在这里
    "dim_cfg.py": "dim_max 单一来源的兜底表，本测试 T1 逐项校验它",
    # step2 是生产评分主程序，_DIM_MAX_DEFAULT 是它自己的兜底表，
    # 由 verify_weights_round36.py 校验与配置一致（既有机制，不并入本测试）
    "step2_stockscorer_v2.py": "_DIM_MAX_DEFAULT 兜底表，verify_weights_round36.py 已校验",
    # 本测试文件里出现字面量是在做「反例」说明，不参与运行
    "test_dim_cfg_single_source.py": "本测试自身",
    # round108f 用无 score_ 前缀的短键名体系（历史遗留），与 dim_cfg 键名不通用。
    # 已修正其中 `"fund": 10` → `5.0`；该键在本脚本内从未被读取，历史输出未受污染。
    "round108f_rule_preview.py": "短键名体系不兼容 dim_cfg；fund 错值已修正为 5.0",
}


def _iter_py_files():
    for fn in sorted(os.listdir(HERE)):
        if fn.endswith(".py"):
            yield fn, os.path.join(HERE, fn)


class TestDimMaxSingleSource(unittest.TestCase):

    def test_t1_safety_default_equals_config(self):
        """T1: 兜底表与 factor_weights.json 的 dim_max 逐项相等。"""
        with open(dim_cfg.WEIGHTS_FILE, encoding="utf-8") as f:
            cfg = (json.load(f).get("dim_max") or {})
        self.assertTrue(cfg, "factor_weights.json 缺 dim_max 块")

        bad = []
        for k, v in dim_cfg._SAFETY_DEFAULT.items():
            cv = cfg.get(k)
            if not isinstance(cv, (int, float)):
                bad.append(f"{k}: 配置缺失（兜底 {v}）")
            elif abs(float(cv) - float(v)) > 1e-9:
                bad.append(f"{k}: 配置 {cv} ≠ 兜底 {v}")
        self.assertEqual(bad, [], "兜底表与配置不一致：\n  " + "\n  ".join(bad))

        extra = [k for k in cfg if k not in dim_cfg._SAFETY_DEFAULT]
        # 配置里有、兜底表没有 = 新增维度未登记兜底 → 配置不可读时该维按 10.0 走
        self.assertEqual(
            extra, [],
            f"配置新增维度未登记进 dim_cfg._SAFETY_DEFAULT：{extra}")

    def test_t2_loader_reads_config_not_default(self):
        """T2: load_dim_max() 的结果来自配置文件。"""
        with open(dim_cfg.WEIGHTS_FILE, encoding="utf-8") as f:
            cfg = (json.load(f).get("dim_max") or {})
        dm = dim_cfg.load_dim_max()
        for k, v in cfg.items():
            if isinstance(v, (int, float)) and v > 0:
                self.assertAlmostEqual(dm[k], float(v), places=9,
                                       msg=f"{k} 未按配置返回")
        # 关键：fund 必须是 5.0（round125 漏的就是它）
        self.assertEqual(dm["score_fund"], 5.0,
                         "score_fund 量程必须是 5.0，按 10.0 处理会让该维分数压半")

    def test_t3_no_hardcoded_dim_max_literal(self):
        """T3: 全仓不允许再出现 DIM_MAX = { ... } 硬编码。"""
        offenders = []
        for fn, path in _iter_py_files():
            if fn in _ALLOWLIST:
                continue
            with open(path, encoding="utf-8") as f:
                src = f.read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for tgt in node.targets:
                    if not isinstance(tgt, ast.Name):
                        continue
                    if tgt.id not in ("DIM_MAX", "DIM_MAX_"):
                        continue
                    if isinstance(node.value, ast.Dict) and node.value.keys:
                        offenders.append(
                            f"{fn}:{node.lineno}  {tgt.id} = {{...}} 硬编码 "
                            f"（{len(node.value.keys)} 项）")
        self.assertEqual(
            offenders, [],
            "发现硬编码 dim_max —— 违反「单一来源」铁律，"
            "改用 `from dim_cfg import load_dim_max`：\n  " + "\n  ".join(offenders))

    def test_t4_loader_catches_missing_dim(self):
        """T4: strict 模式对未知维度必须抛错，不能静默 10.0。"""
        with self.assertRaises(KeyError):
            dim_cfg.load_dim_max(["score_不存在的维度"], strict=True)
        # 非 strict 才允许回退
        self.assertEqual(
            dim_cfg.load_dim_max(["score_不存在的维度"]), {"score_不存在的维度": 10.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
