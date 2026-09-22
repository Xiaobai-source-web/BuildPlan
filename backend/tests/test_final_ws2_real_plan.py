# -*- coding: utf-8 -*-
"""WS2 §11 验收：拿**真实计划**的 WBS 重算一遍，逐条核对三项验收。

依据 `devtools/_dev-notes/终版修改_接口冻结.md` §11：
  ① `demand` 有 `unit` / `measure_scope`（**304/304 非空**）；
  ② `㎡` 计数 **0**；
  ③ 抹灰 `cap_per_face == org.crew_per_face`（另见 `test_org_res_crew.py`）。

⚠️ §11 原第 ④ 项（"无定额任务被防荒谬上限拦住且不超过上限"）**已随第 6 批退役**：
域 1.6（用户裁定"方案 A 彻底退役"）删除了 `Workface_Capacity_Rule`，
`resource._crew_max_values()` 恒返回 `[]`、`_family_crew_ceiling()` 恒返回 `None`
——同族/全表上限不复存在，`test_验收4_...` 与 `test_全量无定额行...` 两条用例
（其断言以 `max(_crew_max_values(""))` 为前提，现会 `ValueError`）**整条删除**。

输入是仓库里的真实计划产物 `backend/plans/plan_run_1789895021.json`：
  · `wbs`（304 个叶子，带 `norm_binding`）、`meta.extracted_params` / `boundary_conditions`；
  · `resource_demand.tasks[*].planned_duration_days` → 排程工期（与当时一致）；
  · `all_tasks_schedule[*]._organization` → 组织层结果（136 条）。
⚠️ 只把这份 JSON 当**输入**（老的 `resource_demand` 是旧政策的产物）；这里断言的是
**重算后**的行。文件缺失时跳过（让测试在有产物的仓库里跑）。

运行：python -m pytest backend/tests/test_final_ws2_real_plan.py -q
"""

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from pipeline import kb_units                       # noqa: E402
from pipeline.nodes import resource as R            # noqa: E402

PLAN = BACKEND / "plans" / "plan_run_1789895021.json"
ARCHIVED_ROWS = 304
# 迁移测试说明（第 6 批）：原先这里的 `ABSURD = {"9.2.3": 125, ...}`
# （用户点名的荒谬班组）只被 `test_验收4_无定额任务被防荒谬上限拦住` 使用。
# 域 1.6 删表后"防荒谬/同族上限"整条退役，该常量与两条依赖它的用例一并删除。


def _leaves(node, out):
    if isinstance(node, dict):
        if node.get("id") and "quantity" in node and "unit" in node:
            out[node["id"]] = node
        for v in node.values():
            _leaves(v, out)
    elif isinstance(node, list):
        for v in node:
            _leaves(v, out)
    return out


@pytest.fixture(scope="module")
def replayed():
    if not PLAN.exists():
        pytest.skip("仓库里没有 %s（真实计划产物）" % PLAN)
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    days = {t["task_id"]: t["planned_duration_days"]
            for t in plan["resource_demand"]["tasks"]}
    org = {r["task_id"]: r["_organization"] for r in plan.get("all_tasks_schedule", [])
           if isinstance(r.get("_organization"), dict) and r["_organization"]}
    flat = R.compute_flat(plan["wbs"], plan["meta"].get("extracted_params"),
                          plan["meta"].get("boundary_conditions"),
                          schedule_days=days, schedule_org=org)
    return plan, flat["resource_demand"]["tasks"], org


def test_验收1_demand全行有unit与measure_scope(replayed):
    plan, tasks, _ = replayed
    assert len(tasks) == ARCHIVED_ROWS, "叶子数变了？实测 %d" % len(tasks)
    missing_unit = [t["task_id"] for t in tasks if not t.get("unit")]
    missing_scope = [t["task_id"] for t in tasks if "measure_scope" not in t]
    assert missing_unit == [], "这些行没有 unit：%s" % missing_unit
    assert missing_scope == [], "这些行没有 measure_scope 键：%s" % missing_scope


def test_验收2_单位写法归一无兼容字形(replayed):
    plan, tasks, _ = replayed
    leaves = _leaves(plan["wbs"], {})
    bad = [t["task_id"] for t in tasks
           if t["unit"] != kb_units.normalize_unit(leaves[t["task_id"]]["unit"])]
    assert bad == [], "这些行的 unit 与叶子（归一后）不一致：%s" % bad
    assert "㎡" not in json.dumps(tasks, ensure_ascii=False), \
        "整份 resource_demand 里不该再出现 ㎡(U+33A1)"


def test_验收3_组织层行cap_per_face单源(replayed):
    plan, tasks, org = replayed
    assert org, "这份真实计划里应当有组织层结果"
    checked = 0
    for t in tasks:
        oc = t.get("_organization_crew")
        if not oc:
            continue
        checked += 1
        src = org[t["task_id"]]
        assert oc["cap_per_face"] == src["crew_per_face"], \
            "%s：cap_per_face 必须等于 org.crew_per_face" % t["task_id"]
        assert oc["cap_total"] == oc["crew_total"] == src["crew_total"]
        assert oc["resource_cap_below_org"] is False, \
            "%s：资源层不再产出第二个更小的上限" % t["task_id"]
        note = t.get("_workface_note") or ""
        assert "组织层" in note and "为准" in note, note
        assert "两套" not in note and "不再封顶" not in note, \
            "%s：单源后不许再出现自相矛盾的两套上限文案：%s" % (t["task_id"], note)
        assert "_workface_capped" not in t, \
            "%s：组织层接管时资源层不许再封顶" % t["task_id"]
    assert checked == len(org), "组织层结果 %d 条，落到资源行的只有 %d 条" % (len(org), checked)


# 迁移测试说明（第 6 批）：原 `test_验收4_无定额任务被防荒谬上限拦住` 与
# `test_全量无定额行都不超过同族上限` 两条用例，前提都是
# `ceiling = max(R._crew_max_values(""))`（同族/全表 `crew_max` 上限）。
# 域 1.6 删表后 `_crew_max_values()` 恒返回 `[]` → `max([])` 直接 `ValueError`；
# "无定额防荒谬上限"经用户裁定取消（不另立新源），
# 两条用例**整条删除**（§11 因此只剩 ①②③ 三项）。
