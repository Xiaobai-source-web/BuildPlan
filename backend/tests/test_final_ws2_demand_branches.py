# -*- coding: utf-8 -*-
"""WS2 §5 + 跨流 D4：`compute_flat` 里**每一条**产出行都必须从同一个底板长出来。

起因（父代理的确定性冻结重放，2026-09-20）：第一轮我只给 3 处 demand 加了
`unit`/`measure_scope`，重组装出来的 304 行里就有 **40 行漏掉**（全是"绑定了但定额不可用"
→ 落到遗留产能表路径的行）。这个文件把"收敛到唯一底板"钉死：
  · 6 条分支（定额 / `_warning` / `_scale_flagged` / `_norm_flagged` / 跳过 / 遗留）
    每一行都要有 `unit`（经 `kb_units.normalize_unit` 归一）与 `measure_scope`（取不到写 `''`）；
  · **无定额**行还要**显式**写出 `_norm_applied: None` —— 键"不存在"与"为 None"在
    WS3 的 D4「无定额暴露」统计里含义不同（跨流要求）；
  · 冻结的参考实现区域（`docs/资源定额.txt` 的对拍样本 = 没有绑定节点产出的 binding）
    形状**逐字段不变** —— 那是 `test_algorithm_parity.py` / `test_crew_bind.py` 的锁。

运行：python -m pytest backend/tests/test_final_ws2_demand_branches.py -q
"""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes import resource as R            # noqa: E402

# 绑定节点（WS1 `norm_bind`）产出的 binding 必带这两个留痕键 —— WS2 用它区分
# "流水线产出的绑定"（要带 §5/D4 的键）与"手写的畸形 binding"（参考实现区域，不许动）。
_OLD_LEGACY_KEYS = {"task_id", "task_name", "quantity", "planned_duration_days"}


def _pipe_binding(**over):
    """一份**绑定节点口径**的 binding（带 `leaf_unit`/`norm_is_evidence`）。"""
    b = {"task_id": "X", "mode": "labor", "norm_value": 0.5, "productivity_value": 2.0,
         "unit": "工日/m²", "source_code": "LD_TEST", "match_type": "kb",
         "usable": True, "norm_is_evidence": True, "quantity_basis": 1.0,
         "crew": {"普工": 4}, "crew_kind": {"普工": "labor"}, "labor_types": ["普工"],
         "leaf_unit": "m²", "unit_check": {"verdict": "same"}}
    b.update(over)
    return b


def _leaf(**over):
    t = {"id": "1.1.1", "name": "场地平整", "quantity": 100.0, "unit": "㎡",
         "duration_days": 5, "kb_activity_id": None, "work_type": "土方工程"}
    t.update(over)
    return t


def _wbs(leaves):
    return {"phases": [{"phase": "施工准备", "work_packages": [
        {"name": "场地准备", "sub_packages": leaves}]}]}


def _rows(leaves, params=None, boundaries=None):
    flat = R.compute_flat(_wbs(leaves), params, boundaries)
    return {r["task_id"]: r for r in flat["resource_demand"]["tasks"]}


# ============ ① 六条分支都从同一底板拿到 `unit` + `measure_scope` ============
def _unusable_binding(**over):
    """流水线产出但**推不出定额**的 binding（`match_type='unbound'`）→ 走遗留/跳过分支。"""
    return _pipe_binding(match_type="unbound", norm_value=None, productivity_value=None,
                         usable=False, norm_is_evidence=False,
                         not_usable_reason="活动绑定不一致", **over)


def test_六条产出分支都带unit与measure_scope():
    """一条 WBS 同时踩满 6 条分支，逐行核对（判据：全量任务都从同一处拿到这两个键）。"""
    leaves = [
        # 定额路径（binding 可用、可作证据）
        _leaf(id="A1", name="场地平整", quantity=1000.0, unit="m²",
              norm_binding=_pipe_binding(task_id="A1")),
        # `_warning` 行（施工类任务用"1项"且推不出总量）
        _leaf(id="A2", name="外架搭设", quantity=1.0, unit="项", work_type="脚手架工程",
              norm_binding=_pipe_binding(task_id="A2")),
        # `_norm_flagged` 行（有绑定但不可作证据）
        _leaf(id="A3", name="沥青混凝土路面施工", quantity=2000.0, unit="m²",
              norm_binding=_pipe_binding(task_id="A3", norm_is_evidence=False,
                                         not_usable_reason="单位不可用：任务「m²」 vs 定额分母「m」",
                                         method_conflict="机械 vs 人工")),
        # `_scale_flagged` 行（量级不可信）
        _leaf(id="A4", name="定位放线", quantity=128000.0, unit="㎡",
              norm_binding=_pipe_binding(task_id="A4")),
        # 跳过行（quantity==1 且 unit=="项"，且不是施工类；定额推不出来才会走到这里）
        _leaf(id="A5", name="临时用电接入", quantity=1.0, unit="项", work_type="临时设施",
              norm_binding=_unusable_binding(task_id="A5")),
        # 遗留路径（有绑定但推不出定额）
        _leaf(id="A6", name="地下室周边回填", quantity=3000.0, unit="㎡",
              work_type="土方工程",
              norm_binding=_unusable_binding(task_id="A6")),
    ]
    rows = _rows(leaves, {"total_area": 14200})

    got = set(rows)
    assert got == {"A1", "A2", "A3", "A4", "A5", "A6"}, got
    assert rows["A3"].get("_norm_flagged") and rows["A4"].get("_scale_flagged")
    assert rows["A2"].get("_warning")
    assert "_matched_keyword" in rows["A6"], "A6 必须走遗留路径：%s" % rows["A6"]

    for tid, row in sorted(rows.items()):
        assert isinstance(row.get("unit"), str) and row["unit"], \
            "%s 必须有非空 unit：%s" % (tid, row)
        assert "measure_scope" in row and isinstance(row["measure_scope"], str), \
            "%s 必须有 measure_scope（未知写 ''，不许 None）：%s" % (tid, row)

    # 单位一律归一（A4/A6 叶子写的是 ㎡ U+33A1）
    assert rows["A4"]["unit"] == "m²" and rows["A6"]["unit"] == "m²"
    assert rows["A1"]["unit"] == "m²" and rows["A6"]["unit"] != "㎡"
    # ⚠️ 只核 `unit`/`measure_scope` 两个字段：`_scale_flagged` 的**原因文案**里可能带用户
    # 原文的 `㎡`，那串字由 `scheduler.scale_violation()` 产出（不是 WS2 的单位通路）。
    for tid, row in sorted(rows.items()):
        assert "㎡" not in (row["unit"] + row["measure_scope"]), \
            "%s 的单位字段不许出现 U+33A1：%r" % (tid, row)


# ============ ② 无定额行：`_norm_applied` 必须显式存在且为 None ============
def test_无定额行的_norm_applied显式存在且为None():
    """跨流 D4：`_norm_applied` 的**存在性**是判据（键缺失 ≠ 键为 None）。"""
    leaves = [
        _leaf(id="B1", name="地下室周边回填", quantity=3000.0, unit="m³",
              norm_binding=_unusable_binding(task_id="B1")),
        _leaf(id="B2", name="临时用电接入", quantity=1.0, unit="项", work_type="临时设施",
              norm_binding=_unusable_binding(task_id="B2")),
        _leaf(id="B3", name="外架搭设", quantity=1.0, unit="项", work_type="脚手架工程",
              norm_binding=_pipe_binding(task_id="B3")),
        _leaf(id="B4", name="沥青混凝土路面施工", quantity=2000.0, unit="m²",
              norm_binding=_pipe_binding(task_id="B4", norm_is_evidence=False,
                                         not_usable_reason="单位不可用",
                                         method_conflict="机械 vs 人工")),
    ]
    rows = _rows(leaves)
    assert set(rows) == {"B1", "B2", "B3", "B4"}, sorted(rows)
    for tid in sorted(rows):
        row = rows[tid]
        assert "_norm_applied" in row, "%s 的无定额结论必须显式落键：%s" % (tid, row)
        assert row["_norm_applied"] is None, \
            "%s 写 None（dict 会被 delivery 的真值判据当成'有定额'）：%r" % (
                tid, row["_norm_applied"])


def test_定额路径的_norm_applied仍是完整字典():
    """反面：定额真的用上了 → `_norm_applied` 是字典（D4 据此不把它算成"无定额"）。"""
    rows = _rows([_leaf(id="C1", name="基础钢筋", quantity=100.0, unit="t",
                        norm_binding=_pipe_binding(task_id="C1", unit="工日/t"))])
    na = rows["C1"]["_norm_applied"]
    assert isinstance(na, dict) and na.get("mode") == "labor", na
    assert bool(na) is True, "真值必须为真，否则 D4 会把定额行也算成无定额"


# ============ ③ 冻结的参考实现区域：形状逐字段不变 ============
def _meta_keys(row):
    """去掉 `*_per_day` / `*_total_days` 资源键后的"任务级"键集合。"""
    return {k for k in row if not k.endswith(("_per_day", "_total_days"))}


def test_参考实现区域的手写绑定形状不变():
    """`docs/资源定额.txt` 的对拍样本（手写/无 binding）不许新增任何顶层键。

    `test_algorithm_parity.py`（无绑定）与 `test_crew_bind.py`（手写畸形绑定）锁的就是
    这条界线；这里用小样例把它钉在本文件里，说明闸门为什么必须是
    "绑定节点产的 binding"（`leaf_unit`/`norm_is_evidence`）而不是"有没有 norm_binding"。
    """
    hand = {"task_id": "D1", "mode": "labor", "norm_value": 0, "source_code": "",
            "match_type": "ai"}                            # 手写：没有 leaf_unit / norm_is_evidence
    for leaf in (_leaf(id="D1", name="底板混凝土", quantity=15600.0, unit="m³",
                       work_type="混凝土工程"),
                 _leaf(id="D2", name="地下室周边回填", quantity=3000.0, unit="m³",
                       work_type="土方工程", norm_binding=hand)):
        rows = _rows([leaf])
        row = rows[leaf["id"]]
        assert _meta_keys(row) <= (_OLD_LEGACY_KEYS | {"_matched_keyword"}), \
            "参考实现区域的键集合不许变：%s" % sorted(row)
        assert "unit" not in row and "measure_scope" not in row
        assert "_norm_applied" not in row
        assert R._has_pipeline_binding(leaf) is False


def test_流水线绑定判定只看绑定节点的留痕():
    """`_has_pipeline_binding`：有绑定节点留痕 → True；手写/空绑定 → False。"""
    assert R._has_pipeline_binding(_leaf(norm_binding=_pipe_binding())) is True
    assert R._has_pipeline_binding(
        _leaf(norm_binding={"mode": "labor", "norm_value": 1.0})) is False
    assert R._has_pipeline_binding(_leaf()) is False
    assert R._has_pipeline_binding({"norm_binding": None}) is False
    assert R._has_pipeline_binding({"norm_binding": "坏了"}) is False
