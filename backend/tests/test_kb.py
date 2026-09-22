"""KB 适配层 + 资源 KB 机械路径测试。

运行：python -m pytest backend/tests/test_kb.py -v
      （也可直接 python 运行本文件）

依赖 BuildPlan_KB/kb.db（本仓库随附），无需网络 / LLM。
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import kb
from pipeline.nodes import resource


# ==================== kb.py ====================
def test_l3_for_residential():
    l3s = kb.l3_for("residential")
    by_level = {}
    for x in l3s:
        by_level.setdefault(x["applicability_level"], set()).add(x["work_type_id"])
    # 新完整版 KB：residential 覆盖 30 个工程类型（REQUIRED 25 + OPTIONAL 5）。
    # 这里只断言"骨架稳定"的关键项，避免死记精确全集导致数据一刷新就崩。
    req = by_level["REQUIRED"]
    # 结构核心工种必须 REQUIRED
    assert {"concrete", "rebar", "formwork", "earthwork"} <= req
    # 完整版新覆盖的装饰/机电类也已 REQUIRED（旧库缺失，曾降级给 LLM）
    assert {"door_window", "electrical", "plumbing", "painting", "flooring"} <= req
    # material_transport 于第 38 轮由 REQUIRED 降为"可选"：名下 116 个 L4 全是「XX运输」、
    # 给不出可注入工序，标着"必含"只会让每次生成都误报一次（详见
    # devtools/migrate_material_transport_to_usual.py）。
    # A1 三档化（2026-09-21 用户裁定 ②）：枚举名统一为 OPTIONAL，USUAL 不再写入。
    assert by_level["OPTIONAL"] == {"demolition", "hvac", "masonry", "pile_foundation",
                                    "material_transport"}
    assert by_level["EXCLUDED"] == {"steel_structure"}
    # 反向守卫：库里不许再出现 USUAL
    assert "USUAL" not in by_level, by_level


def test_resolve_building_type():
    assert kb.resolve_building_type("10层框架住宅")[0] == "residential"
    assert kb.resolve_building_type("residential 项目")[0] == "residential"
    assert kb.resolve_building_type("一个没有类型的项目") is None


def test_resolve_structure_type():
    assert kb.resolve_structure_type("框架结构")[0] == "frame"
    assert kb.resolve_structure_type("剪力墙")[0] == "shear_wall"
    assert kb.resolve_structure_type("砖混")[0] == "masonry_conc"


# ==================== F2：结构/建筑类型解析（2026-09-21） ====================
# 修前实测：'框架剪力墙结构'→shear_wall、'框架-剪力墙结构'→shear_wall、
#           'frame_shear'（正确 ID）→frame、'高层住宅，框架剪力墙结构'→shear_wall。
# 根因：子串匹配 + 「首个命中即返回」+ 关键词表缺复合词。
# 修法：① ID 完全相等 ② 名称完全相等 ③ 归一化最长子串 ④ 关键词最长优先。

def test_resolve_structure_type_f2_regression():
    """F2 主表 4 个输入：各自返回修好后的正确值。"""
    # 用户最终验收输入正好踩中的一条
    assert kb.resolve_structure_type("框架剪力墙结构")[0] == "frame_shear"
    assert kb.resolve_structure_type("框架-剪力墙结构")[0] == "frame_shear"
    # 回归守卫：传的正是正确 ID，曾被 "frame" 子串吞成 frame
    assert kb.resolve_structure_type("frame_shear")[0] == "frame_shear"
    assert kb.resolve_structure_type("高层住宅，框架剪力墙结构")[0] == "frame_shear"
    # 返回的名称也必须是真值表里的那个
    assert kb.resolve_structure_type("框架剪力墙结构")[1] == "框架-剪力墙"


def test_resolve_structure_type_single_forms_not_broken():
    """F2 不许修坏既有解析：单一结构形式与既有断言照旧。"""
    assert kb.resolve_structure_type("框架结构")[0] == "frame"
    assert kb.resolve_structure_type("剪力墙结构")[0] == "shear_wall"
    assert kb.resolve_structure_type("剪力墙")[0] == "shear_wall"
    assert kb.resolve_structure_type("框剪结构")[0] == "frame_shear"
    assert kb.resolve_structure_type("高层住宅，剪力墙结构")[0] == "shear_wall"
    assert kb.resolve_structure_type("砖混结构")[0] == "masonry_conc"
    assert kb.resolve_structure_type("钢结构")[0] == "steel"
    assert kb.resolve_structure_type("排架结构")[0] == "bent"
    assert kb.resolve_structure_type("筒体结构")[0] == "tube"


def test_resolve_structure_type_normalized_forms():
    """归一化：去掉 -/－/—/空格 后一律判 frame_shear（含全角写法）。"""
    for text in ("框架-剪力墙结构", "框架剪力墙结构", "框架－剪力墙结构",
                 "框架—剪力墙结构", "框架 － 剪力墙 结构", "框架-剪力墙"):
        got = kb.resolve_structure_type(text)
        assert got is not None and got[0] == "frame_shear", (text, got)


def test_resolve_structure_type_all_seven_by_name_and_id():
    """7 个结构类型：中文名与纯 ID 双向都必须回到自己（逐条）。"""
    rows = kb._query_all("SELECT structure_type_id, structure_type_name "
                         "FROM Structure_Type_Dictionary")
    assert len(rows) == 7, rows
    for sid, sname in rows:
        assert kb.resolve_structure_type(sname)[0] == sid, ("名称", sname, sid)
        assert kb.resolve_structure_type(sid)[0] == sid, ("ID", sid)
    # ID 子串陷阱的正向守卫（frame ⊂ frame_shear）
    assert kb.resolve_structure_type("frame_shear")[0] != "frame"
    assert kb.resolve_structure_type("Frame_Shear")[0] == "frame_shear"   # 大小写无关
    assert kb.resolve_structure_type("不存在的东西") is None
    assert kb.resolve_structure_type("") is None


def test_match_structure_type_pure_function():
    """纯函数（不查库）：extractor.py 将改为调用它，顺序也必须是「最长优先」。"""
    assert kb.match_structure_type("框架剪力墙结构") == "frame_shear"
    assert kb.match_structure_type("框架-剪力墙结构") == "frame_shear"
    assert kb.match_structure_type("框架－剪力墙结构") == "frame_shear"
    assert kb.match_structure_type("剪力墙结构") == "shear_wall"
    assert kb.match_structure_type("框架结构") == "frame"
    assert kb.match_structure_type("框剪结构") == "frame_shear"
    assert kb.match_structure_type("砖混结构") == "masonry_conc"
    assert kb.match_structure_type("钢结构") == "steel"
    assert kb.match_structure_type("排架结构") == "bent"
    assert kb.match_structure_type("筒体结构") == "tube"
    assert kb.match_structure_type("无关文本") is None
    assert kb.match_structure_type("") is None
    # 纯函数不查库：只有 ID 的文本不应命中（ID 层由 resolve_* 负责）
    assert kb.match_structure_type("frame_shear") is None


def test_match_building_type_pure_function():
    assert kb.match_building_type("住宅") == "residential"
    assert kb.match_building_type("写字楼") == "office"
    assert kb.match_building_type("厂房") == "industrial"
    assert kb.match_building_type("无关文本") is None
    assert kb.match_building_type("") is None


def test_resolve_building_type_longest_first_not_broken():
    """建筑类型同法修（完全相等 → 归一化最长 → 关键词），既有解析保持不变。"""
    assert kb.resolve_building_type("10层框架住宅")[0] == "residential"
    assert kb.resolve_building_type("residential 项目")[0] == "residential"
    assert kb.resolve_building_type("一个没有类型的项目") is None
    assert kb.resolve_building_type("") is None
    # 名称层最长优先：'综合体' 比 '商业' 长 → mixed_use（修前也是这个结果）
    assert kb.resolve_building_type("商业综合体")[0] == "mixed_use"
    assert kb.resolve_building_type("综合体")[0] == "mixed_use"
    assert kb.resolve_building_type("工业厂房")[0] == "industrial"
    # ID 完全相等优先（大小写无关）
    assert kb.resolve_building_type("RESIDENTIAL")[0] == "residential"


def test_equipment_norms_found():
    rows = kb.equipment_norms("CONC_NEW_FOUND")
    pump = [r for r in rows if "泵车" in (r.get("machine_combination_json") or "")]
    assert pump, "基础浇筑应有泵车机械定额"
    assert float(pump[0]["machine_shift_norm_json"].strip("[]")) == 0.055
    assert pump[0]["quantity_basis"] == 10.0


def test_equipment_norms_exposes_condition_combination():
    """D2 机械侧条件精筛：`equipment_norms()` 必须原样带出 `condition_combination`。

    只加列：原有列仍按 key 取值，返回结构/排序不变。
    """
    rows = kb.equipment_norms("CONC_NEW_FOUND")
    assert rows, "测试前提：该活动应有机械台班定额"
    assert all("condition_combination" in r for r in rows)

    # 真实表实测：键必须存在；值允许为空（2 行 NULL/空、部分为 '{}'）
    all_rows = kb._query_all("SELECT condition_combination FROM Norm_Equipment_Table")
    assert all_rows, "测试前提：Norm_Equipment_Table 非空"
    filled = sum(1 for (v,) in all_rows
                 if v is not None and str(v).strip() not in ("", "{}"))
    assert filled > 0, "KB 里应至少有一行带非空条件组合"

    # 抽一个 DB 里确有非空条件的活动，确认值真的被带出来
    picked = kb._query_all(
        "SELECT activity_id FROM Norm_Equipment_Table "
        "WHERE IFNULL(condition_combination, '') NOT IN ('', '{}') "
        "ORDER BY activity_id LIMIT 1")
    assert picked, "KB 里应至少有一个带条件的机械定额活动"
    rows = kb.equipment_norms(picked[0][0])
    assert rows and all("condition_combination" in r for r in rows)
    assert any((r["condition_combination"] or "").strip() not in ("", "{}")
               for r in rows)


def test_activity_info_labor():
    assert kb.activity_info("REBAR_NEW_COL")["recommended_production_mode"] == "labor_driven"


# ==================== resource.py KB 机械路径 ====================
def test_compute_kb_machinery():
    m = resource.compute_kb_machinery("CONC_NEW_FOUND", 15600, 15)
    assert m is not None
    assert "混凝土输送泵车" in m
    pump = m["混凝土输送泵车"]
    assert pump["per_day"] == 6, f"⌈15600/10×0.055/15⌉ 应为 6，实得 {pump['per_day']}"
    assert pump["total_days"] == 85.8  # 总台班固定，不随工期漂移


def test_resource_kb_machinery_path():
    wbs = {"phases": [{"phase": "地下结构", "work_packages": [
        {"id": "1.1", "name": "混凝土", "sub_packages": [
            {"id": "1.1.1", "name": "基础浇筑", "kb_activity_id": "CONC_NEW_FOUND",
             "duration_days": 15, "quantity": 15600, "unit": "m³",
             "work_type": "混凝土工程"},
        ]}]}]}
    flat = resource.compute_flat(wbs, {"total_concrete": 15600}, None)
    t = flat["resource_demand"]["tasks"][0]
    # 机械走 KB：泵车 6 台/天（总台班 85.8 固定）
    assert t["混凝土输送泵车_per_day"] == 6
    assert t["混凝土输送泵车_total_days"] == 85.8
    # 人工仍走产能表：混凝土工 52 人/天（20 m³/人/天）
    assert t["混凝土工_per_day"] == 52
    # 旧产能路径的 泵车 已移除，不重复
    assert "泵车_per_day" not in t
    assert flat["resource_demand"]["_kb_machinery_count"] == 1


def test_resource_no_kb_unchanged():
    """无 kb_activity_id 的任务：机械仍走现有产能表（回归护栏）。"""
    wbs = {"phases": [{"phase": "地下结构", "work_packages": [
        {"id": "1.1", "name": "混凝土", "sub_packages": [
            {"id": "1.1.1", "name": "底板混凝土",
             "duration_days": 15, "quantity": 15600, "unit": "m³",
             "work_type": "混凝土工程"},
        ]}]}]}
    flat = resource.compute_flat(wbs, None, None)
    t = flat["resource_demand"]["tasks"][0]
    assert t["泵车_per_day"] == 13  # 产能表：⌈1040/80⌉
    assert "_kb_machinery_count" not in flat["resource_demand"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n全部 {len(tests)} 个 KB 用例通过 ✔")
