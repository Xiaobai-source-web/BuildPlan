# -*- coding: utf-8 -*-
"""F2 回归：机械/人工「主导方式」矛盾必须被发现，且不得用来重算工期。

背景（真实缺陷，已修）：
  `kb_activity_id` 是模型给的，它可能把「机械挖基坑土方 3500 m³」绑到 KB 里标注为
  **人工主导**、条件写着"坑底面积≤2.5m²，深度≤3m"的**人工挖小坑**定额（EARTH0032）。
  `norm_bind` 忠实地按活动标注走人工口径 —— **没有任何环节去比对"任务说的"和
  "活动标的"是否一致**，于是人工定额被当成这条机械任务的依据：

      工日需求 = 3500 × 0.827 = 2,894 工日（真实机械挖土方约 10~30 工日）
      工期     = 2894 ÷ 20 人 = 145 天（WBS 给的是 18 天）

  更糟的是它把人力预算吃光（普工占 42%），连带把模板/瓦工/钢筋的班组压到 1~3 人。

修法：
  · `norm_bind` 检出矛盾 → 在 binding 上写 `method_conflict`；
  · `scheduler` 把它当作"非有据可查"（与 AI 估算同等）→ 沿用 WBS 工期，
    并在定额覆盖率里单列为「定额口径不符」。

运行：python -m pytest backend/tests/test_norm_method_conflict.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes import norm_bind as NB  # noqa: E402
from pipeline.nodes import scheduler as S  # noqa: E402

_LABOR_ACT = {"activity_id": "EARTH0032", "activity_name": "挖基坑土方",
              "recommended_production_mode": "labor_driven", "construction_method": "人工"}
_MACHINE_ACT = {"activity_id": "GD_A11_机械挖土方", "activity_name": "机械挖土方",
                "recommended_production_mode": "equipment_driven", "construction_method": "机械"}


# ---------------- 1. 机械词判定 ----------------
def test_机械词判定():
    cases = [
        ("机械挖基坑土方（含地下室范围及工作面）", "土方工程", True),
        ("地下连续墙施工（含导墙、成槽、钢筋笼吊装、水下混凝土浇筑）", "支护工程", True),
        ("锚索施工（含钻孔、锚固、张拉锁定）", "支护工程", True),
        ("旋挖灌注桩成孔（含泥浆制备与运输）", "桩基工程", True),
        ("人工挖土方", "土方工程", False),
        ("Ⅰ区 1-1层 钢筋绑扎", "钢筋工程", False),
        ("基坑变形监测（含沉降、位移、支撑轴力等）", "监测", False),
    ]
    for name, wt, want in cases:
        assert NB._expects_machine(name, wt) is want, (name, want)


def test_出现人工字样时不以机械论():
    assert NB._expects_machine("人工配合机械挖土方", "土方工程") is False


# ---------------- 2. 冲突判定 ----------------
def test_机械任务绑到人工活动要报冲突():
    note = NB._method_conflict_note(
        "机械挖基坑土方（含地下室范围及工作面）",
        {"kb_activity_id": "EARTH0032", "work_type": "土方工程"}, _LABOR_ACT)
    assert note and "EARTH0032" in note
    assert "非机械主导" in note


def test_人工任务绑人工活动不报冲突():
    assert NB._method_conflict_note(
        "Ⅰ区 1-1层 钢筋绑扎",
        {"kb_activity_id": "REBAR_NEW_SLAB", "work_type": "钢筋工程"}, _LABOR_ACT) is None


def test_机械任务绑机械活动不报冲突():
    assert NB._method_conflict_note(
        "机械挖基坑土方", {"kb_activity_id": "GD_A11", "work_type": "土方工程"},
        _MACHINE_ACT) is None


def test_没有活动信息时不报冲突():
    assert NB._method_conflict_note("机械挖土方", {"work_type": "土方工程"}, None) is None


# ---------------- 3. 冲突的定额不得用来重算工期 ----------------
def _wbs(conflict_note):
    binding = {
        "mode": "labor", "norm_value": 0.827, "quantity_basis": 1.0,
        "productivity_value": None, "unit": "工日/m³",
        "source_code": "LD_T72_2_2008", "match_type": "default",
        "labor_types": ["普工"], "crew": {},
        "condition_text": "坑底面积≤2.5m²，深度≤3m，三类土",
        "provenance": {"origin": "kb", "ref": "LD_T72_2_2008", "confidence": "中"},
    }
    if conflict_note:
        binding["method_conflict"] = conflict_note
    return {"phases": [{"phase": "土方", "work_packages": [{
        "id": "3.2", "name": "土方", "sub_packages": [{
            "id": "3.2.1", "name": "机械挖基坑土方", "quantity": 3500.0, "unit": "m³",
            "duration_days": 18, "work_type": "土方工程", "_crew_design": 15,
            "kb_activity_id": "EARTH0032", "norm_binding": binding,
        }]}]}]}


def _days(wbs, version="resource_ok"):
    out = S.compute_schedules(wbs, [], {}, {}, None)
    ver = (out.get("schedule_versions") or {}).get(version) or {}
    rows = {str(r["task_id"]): r for r in (ver.get("schedule") or [])}
    r = rows["3.2.1"]
    return int(r["ef"]) - int(r["es"]), out


def test_冲突时沿用WBS工期且单列覆盖率():
    days, out = _days(_wbs("任务描述像机械作业，但绑定的活动标注为非机械主导"))
    assert days == 18, "冲突时应沿用 WBS 工期 18 天，实际 %d 天（说明仍在用错误定额）" % days
    cov = out.get("norm_coverage") or {}
    assert cov.get("by_reason", {}).get("定额口径不符") == 1, cov.get("by_reason")
    assert cov.get("bound") == 0, cov


def test_无冲突时该定额照常生效():
    """对照：域 1.6 删表后，**没有工作面容量可回退** → 工期沿用 WBS 的 18 天。

    因果链（已用 `scheduler.compute_schedules` 实测确认）：
      · 本叶子不带 `workface_capacity` → `S.resolve_workface(leaf) is None` →
        `S.workface_limits_from_rule(...) == (None, None)`
        （域 1.6（第 6 批）删了 `Workface_Capacity_Rule`，不再回查 KB）；
      · 组织层（MWI）段容量也取不到 → `_duration_for_item` 走兜底分支：没有可用
        容量就**如实报缺、不编人数**，工期沿用叶子原值 → `ef-es == 18`。
      · 定额本身**没有失效**：`norm_coverage["bound"] == 1`，人日需求（3500 × 0.827
        = 2894 工日）照算，缺的只是"除以哪个班组人数"的容量数据。

    旧口径：单面上限由已删的 `Workface_Capacity_Rule` 给出（经公式约 15 人/班）
    → 2894 工日 ÷ 15 人 ≈ 193 天。上限随表退役，193 天这个数不再存在。
    """
    days, out = _days(_wbs(None))
    assert days == 18, "无容量数据可回退 → 沿用 WBS 工期 18 天，实际 %d 天" % days
    cov = out.get("norm_coverage") or {}
    assert cov.get("bound") == 1, "定额本身仍然生效（只是没有容量可除）：%s" % cov
    ver = (out.get("schedule_versions") or {}).get("resource_ok") or {}
    row = {str(r["task_id"]): r for r in (ver.get("schedule") or [])}["3.2.1"]
    assert row.get("capacity_source") == "reported_missing", \
        "缺容量必须如实报 reported_missing：%s" % row
    assert row.get("crew") == {}, "报缺时不许编班组：%s" % row.get("crew")


# ---------------- 4. 主动补救：同工种内改绑机械活动 ----------------
from pipeline import kb  # noqa: E402
from pipeline.nodes import norm_bind as _NB  # noqa: E402

_TASK = "机械挖基坑土方（含地下室范围及工作面）"
_OLD = "EARTH0032"          # KB 里标注 labor_driven / 施工方法=人工 的「挖基坑土方」


def _fresh():
    return ({"task_id": "T", "mode": "labor", "norm_value": None, "unit": "",
             "condition_text": "", "quantity_basis": 1.0, "source_code": "",
             "match_type": "ai", "crew": {}, "provenance": {}},
            {"id": "T", "name": _TASK, "kb_activity_id": _OLD,
             "unit": "m³", "work_type": "土方工程"})


def test_原活动的确是非机械主导():
    """前置条件：证明改绑不是空转 —— 旧活动在 KB 里确实标的是人工。"""
    info = kb.activity_info(_OLD)
    assert info, "KB 里找不到 %s" % _OLD
    assert info.get("recommended_production_mode") == "labor_driven", info
    assert info.get("construction_method") == "人工", info


def test_机械任务绑到人工活动时改绑到机械活动():
    node = _NB.NormBindNode(llm=None)
    binding, leaf = _fresh()
    ok = node._reanchor_machine(_OLD, _TASK, binding, leaf, "m³")
    assert ok, "应能在同工种内改绑到机械活动"
    assert binding["mode"] == "machine"
    assert leaf["kb_activity_id"] != _OLD, "叶子的活动编号必须同步改掉"
    assert "method_conflict" not in binding, "改绑成功后冲突标记应清除"
    assert binding.get("reanchored_from") == _OLD, "必须留下改绑痕迹"
    note = str((binding.get("provenance") or {}).get("note") or "")
    assert "改绑机械活动" in note, note
    # 改绑到的活动本身必须是机械主导
    new_info = kb.activity_info(leaf["kb_activity_id"])
    assert new_info and new_info.get("recommended_production_mode") == "equipment_driven"


def test_改绑后的定额是台班口径不是工日():
    """这条最容易搞错：同一个 norm_value=1.68，台班口径下是「台班/1000m³」。"""
    node = _NB.NormBindNode(llm=None)
    binding, leaf = _fresh()
    assert node._reanchor_machine(_OLD, _TASK, binding, leaf, "m³")
    assert binding["mode"] == "machine"
    assert "台班" in str(binding.get("unit") or ""), binding.get("unit")
    assert binding.get("source_code"), "台班定额必须有来源"


def test_人工任务不会被改绑():
    node = _NB.NormBindNode(llm=None)
    binding, leaf = _fresh()
    leaf["name"] = "Ⅰ区 1-1层 钢筋绑扎"
    leaf["kb_activity_id"] = "REBAR_NEW_SLAB"
    assert node._reanchor_machine("REBAR_NEW_SLAB", leaf["name"], binding, leaf, "t") is False
    assert leaf["kb_activity_id"] == "REBAR_NEW_SLAB"


def test_没有机械候选时拒绝改绑():
    node = _NB.NormBindNode(llm=None)
    binding, leaf = _fresh()
    # 「基坑变形监测」没有任何机械词 → 不该硬凑一个机械活动
    leaf["name"] = "基坑变形监测（含沉降、位移、支撑轴力等）"
    assert node._reanchor_machine(_OLD, leaf["name"], binding, leaf, "项") is False
    assert leaf["kb_activity_id"] == _OLD
    assert "method_conflict" not in binding, "拒绝改绑时不该写改绑痕迹"


# ---------------- 5. 端到端：跑整个节点不许崩 ----------------
def _run_node_with_leaf(leaf):
    """把一条叶子塞进最小 WBS，跑完整 NormBindNode.run(ctx)。"""
    node = _NB.NormBindNode(llm=None)
    node._emit = lambda *a, **k: None
    ctx = {"wbs": {"phases": [{"phase": "土方", "work_packages": [{
        "id": "3.2", "name": "土方", "sub_packages": [leaf]}]}]},
        "prompt": "某项目 机械挖基坑土方 3500 m³", "extracted_params": {}}
    return node.run(ctx), ctx


def test_改绑路径端到端不崩且警告全是dict():
    """真实缺陷回归（用户实测报错）：

        ✖ 节点失败于节点 norm_bind: 'str' object has no attribute 'get'

    根因：`_bind_one` 在「机械改绑成功」这条**补救路径**上把**纯字符串** append 进
    warnings（其它都是 dict），`run()` 里 `warnings.extend(...)` 于是把字符串的
    **每个字符**当成一条警告，最后 `warnings.sort(key=lambda w: w.get(...))` 炸掉。

    这条路径以前的测试只单测了 `_reanchor_machine` 这个**私有方法**，从没跑过整个节点，
    所以一直没被发现。这里补上端到端断言。
    """
    leaf = {"id": "3.2.1", "name": _TASK, "quantity": 3500.0, "unit": "m³",
            "duration_days": 18, "work_type": "土方工程", "kb_activity_id": _OLD}
    result, ctx = _run_node_with_leaf(leaf)          # 不许抛异常

    warns = result.get("norm_warnings") or []
    assert warns, "改绑路径应产生一条提示警告"
    bad = [w for w in warns if not isinstance(w, dict)]
    assert not bad, "警告必须是 dict（不能混进字符串，否则排序时会崩）：%r" % bad
    assert all("reason" in w and "task_id" in w for w in warns), warns
    assert len(warns) == 1, "字符串被 extend 拆成多条的 bug 必须不复现：%d 条" % len(warns)

    # 改绑真的生效了：绑定到机械活动、台班口径
    b = (result.get("norm_bindings") or {}).get("3.2.1") or {}
    assert b.get("mode") == "machine", b
    assert leaf.get("kb_activity_id") != _OLD, leaf.get("kb_activity_id")
    assert "台班" in str(b.get("unit") or ""), b


def test_归一化防护把非dict警告包成dict():
    """即便将来又有人往 warnings 里塞字符串，也不能再打断整条流水线。"""
    node = _NB.NormBindNode(llm=None)
    leaf = {"id": "X", "name": "某工序"}
    got = node._as_warnings("一条字符串警告", leaf, "某工序")
    assert len(got) == 1 and isinstance(got[0], dict), got
    assert got[0]["reason"] == "一条字符串警告", got
    assert node._as_warnings(None, leaf, "x") == []
    assert node._as_warnings([{"a": 1}, "混进来的"], leaf, "x")[1]["reason"] == "混进来的"


def test_上游返回字符串警告时run不能崩(monkeypatch):
    """**证明防护是吃劲的**：把 `_bind_one` 换成返回字符串警告的桩。

    这条在去掉 `_as_warnings` 防护后必定以
    `AttributeError: 'str' object has no attribute 'get'` 失败 ——
    即它就是线上那个报错的最小复现。
    """
    node = _NB.NormBindNode(llm=None)
    node._emit = lambda *a, **k: None
    leaf = {"id": "T", "name": "某工序", "quantity": 10.0, "unit": "项"}
    binding = {"task_id": "T", "mode": "labor", "norm_value": 1.0, "unit": "工日/项",
               "condition_text": "", "quantity_basis": 1.0, "source_code": "",
               "match_type": "ai", "crew": {}, "provenance": {}}
    monkeypatch.setattr(node, "_bind_one",
                        lambda *a, **k: (binding, "旧的字符串式警告"))
    ctx = {"wbs": {"phases": [{"phase": "P", "work_packages": [
        {"id": "1", "name": "P", "sub_packages": [leaf]}]}]},
        "prompt": "", "extracted_params": {}}
    result = node.run(ctx)                       # 不许抛异常
    warns = result.get("norm_warnings") or []
    assert [w for w in warns if not isinstance(w, dict)] == [], warns
    assert len(warns) == 1, "字符串被 extend 拆成逐字符的 bug 不许复现：%r" % warns


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  %s" % name)
    print("全部通过")
