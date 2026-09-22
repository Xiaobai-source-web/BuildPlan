# -*- coding: utf-8 -*-
"""WS2 §7「无定额工序防荒谬上限」的**退役记录** + 未随之改变的口径回归。

历史（旧口径）：**无定额**（`match_type in ('ai','unbound')`）**且无组织层结果**时，
人数取**偏大一档**的同族上限：`Workface_Capacity_Rule` 按 `work_type_l3` 取
`crew_max` 的 max（只会更宽、绝不把正常班组压小）；目的**只是防荒谬**
（用户实测：9.2.3 沥青混凝土路面 125 人、9.2.2 84、9.3.3 50、5.1.x.3 67）；
打标 `_resource_source[工种]["origin"] == "ai_crew"`，`_workface_note` 写
"班组由模型自定（无定额依据），已按同族上限封顶"。

**第 6 批 / 域 1.6（用户裁定"方案 A 彻底退役"，且"无定额任务的防荒谬上限可以取消、
不另立新源"）**：删除了 `Workface_Capacity_Rule` 表；资源层的
`_crew_max_values()` 恒返回 `[]`、`_family_crew_ceiling()` 恒返回 `None`
——即**同族上限与防荒谬封顶一并取消**。本文件中为该项能力而存在的用例
（"按同族上限封顶"、"未超上限时不谎称封顶"、"同族有标定行时用同族"、
"同族未知时退全表"、"compute_flat 也封顶"）**整条删除**。

保留下来的三条钉住**没有随退役改变**的口径：
  · 真实定额行（`match_type=kb/exact/default`）算多少就是多少、不许被压小；
  · 组织层结果（§6）是唯一真源，资源层不许产出第二个更小的上限；
  · 叶子**自带** `workface_capacity` 时封顶照常生效（`cap_source == "workface_capacity"`）。

运行：python -m pytest backend/tests/test_final_ws2_ai_crew.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes import resource as R            # noqa: E402

ORG = {"cadence_days": 7.0, "n_faces": 2, "crew_per_face": 19, "crew_total": 38,
       "crew_source": "org_curve", "source": "cadence", "duration_days": 7}


def _ai_leaf(qty=2000.0, days=8, unit="m²", norm=0.5, productivity=2.0, **over):
    """一个"无定额"叶子：KB 无定额行 → AI 经验产能（`norm=0.5 工日/单位`）。"""
    t = {"id": "9.2.3", "name": "沥青混凝土路面施工", "quantity": qty, "unit": unit,
         "duration_days": days, "kb_activity_id": None, "work_type": "道路工程",
         "workface_capacity": None,
         "norm_binding": {"mode": "labor", "norm_value": norm,
                          "productivity_value": productivity,
                          "unit": "工日/%s" % unit, "source_code": "",
                          "match_type": "ai", "usable": False, "norm_is_evidence": False,
                          "condition_text": "经验产能估算（无 KB 定额）",
                          "quantity_basis": 1.0, "crew": {}, "labor_types": []}}
    t.update(over)
    return t


# 迁移测试说明（第 6 批）：原先这里的 `_whole_table_ceiling()` 辅助函数取
# `max(R._crew_max_values(""))`。域 1.6 删表后 `_crew_max_values()` 恒返回 `[]`，
# `max([])` 会抛 `ValueError` —— 辅助函数连同依赖它的用例一并删除。


def test_真实定额行不受防荒谬上限影响():
    """不得把正常班组压小：`match_type=kb/exact/default` 的行一个数都不许动。"""
    leaf = _ai_leaf(qty=1000.0, days=5)
    leaf["norm_binding"] = dict(leaf["norm_binding"],
                                match_type="kb", source_code="LD_T72_7_2008",
                                norm_value=0.05, productivity_value=20.0,
                                usable=True, norm_is_evidence=True)
    d = R.compute_norm_resources(leaf, leaf["norm_binding"], 1000.0, 5)
    assert d["普工_per_day"] == 10, "真实定额行算多少就是多少（⌈1000/5/20⌉=10）"
    assert not d.get("_workface_capped"), "没有上限就不许压"
    assert d["_resource_source"]["普工"]["origin"] != "ai_crew"
    assert "班组由模型自定" not in (d.get("_workface_note") or "")


def test_组织层结果优先于防荒谬上限():
    """§6 组织层是唯一真源：它给的班组（38 人）不许被 §7 的同族上限（16 人）压掉。"""
    leaf = _ai_leaf()
    d = R.compute_norm_resources(leaf, leaf["norm_binding"], 2000.0, 8, org=dict(ORG))
    assert d["普工_per_day"] == 38
    assert d["_organization_crew"]["cap_per_face"] == 19
    assert d["_resource_source"]["普工"]["origin"] == "org_layer"
    assert "ai_crew" not in str(d["_resource_source"])
    assert not d.get("_workface_capped")


def test_已有按段上限时只紧不松():
    """叶子自带 `workface_capacity`（按段 3 人）时：取 min（3），不放松到同族上限。"""
    leaf = _ai_leaf(workface_capacity={"max_labor": 3, "max_machine": None,
                                       "unit_basis": "每施工段",
                                       "source_type": "ai_estimate", "confidence": "LOW"})
    d = R.compute_norm_resources(leaf, leaf["norm_binding"], 2000.0, 8)
    assert d["普工_per_day"] == 3, "既有封顶不许被 §7 放松"
    cap = (d.get("_workface_capped") or [None])[0]
    assert cap and cap["capped_per_day"] == 3
    assert cap["cap_source"] == "workface_capacity", "约束来自按段上限，不是 §7"


# 迁移测试说明（第 6 批）：本节原有 3 条用例
#   test_同族有标定行时用同族而不是全表（_crew_max_values 恒空 → 现永远 skip）
#   test_同族未知时退全表上限（断 _family_crew_ceiling == max(全表)）
#   test_无定额且无组织层时走compute_flat也封顶（断行数 == 全表上限 + origin=ai_crew）
# 断的都是已退役的「无定额防荒谬/同族上限」能力：表已删，
# `_crew_max_values()` 恒 `[]`、`_family_crew_ceiling()` 恒 `None`，
# 没有任何"上限"可断 → **整条删除**（留着只会是一条永远 skip 的死用例）。
