# -*- coding: utf-8 -*-
"""第 32 轮回归：给用户的编号选择 + 参数名的中文口径

对应用户三条实测反馈：
  1. 「我建议第十步你给选项一个代号，比如 1. 每层一段+粗粒度 2. 五层一段+粗粒度。
     让用户直接输入数字选择，而非还要自己打字」→ plan_level 门发 `picker`，
     后端 `_parse_picker` 按同一份编号解析；
  1b.（第 38 轮）「不要做成"X+X"两轴选项，**直接给用户六个选项**」→ 编号 1-6
     每个都是一整档（三种楼层分段 × 两种工序细度），敲一个数字就定完；
  2. 「不要刻意使用一些英文和专业术语」→ 门上原来露的是 `total_concrete`、
     `building_count`、`floors` 这类内部键名，现在一律走中文名；
     交付物里的「不可用于施工」横幅同样改中文。

运行：python -m pytest backend/tests/test_ui_naming.py -q
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
for p in (str(BACKEND), str(ROOT / "terminal")):
    if p not in sys.path:
        sys.path.insert(0, p)

import renderer  # noqa: E402
from pipeline import quantity as q  # noqa: E402
from pipeline.nodes import boundary as B  # noqa: E402
from pipeline.nodes.param_review import _gate_message  # noqa: E402
from pipeline.nodes.plan_level import PlanLevelNode, _picker_payload  # noqa: E402

PLAIN = renderer.strip_ansi


# ======================================================================
# 1. 参数中文名（唯一真源在 boundary.PARAM_LABELS）
# ======================================================================
def test_内部键名都要有中文名():
    keys = (list(B.REQUIRED_KEYS) + list(B.DEFAULT_KEYS) + list(B.FALLBACK_KEYS)
            + list(B.LABEL_ONLY_KEYS) + ["building_count", "floors"])
    missing = [k for k in keys if B.param_label(k) == k]
    assert not missing, "这些键名没有中文名，会直接露给用户：%s" % missing


def test_查不到的键原样返回不编():
    assert B.param_label("some_future_key") == "some_future_key"
    assert B.param_label(None) == ""


def test_参数列表用顿号连接():
    assert B.param_label_list(["floors", "total_area"]) == "层数、总建筑面积(m²)"
    assert B.param_label_list([]) == ""


def test_参数门提示里不许出现内部键名():
    """门上的那句"以下参数缺失…"曾经是 `total_concrete、total_rebar`。"""
    comp = {"ok": True,
            "missing_default": ["building_count"],
            "missing_fallback": ["total_concrete", "total_rebar", "total_pile"],
            "missing_required": []}
    msg = _gate_message(comp, 1)
    assert "混凝土总量(m³)" in msg and "钢筋总量(吨)" in msg and "栋数" in msg, msg
    # 【第 2 批 · 域 2】`total_pile` 的中文名就是「桩」（**刻意不带单位** —— 该键不预设单位）
    assert "桩" in msg, msg
    for raw in ("total_concrete", "total_rebar", "total_pile", "building_count"):
        assert raw not in msg, "内部键名 `%s` 不该出现在门上：\n%s" % (raw, msg)


def test_缺必要参数时的提问也是中文():
    comp = {"ok": False, "missing_required": ["floors", "total_area"],
            "note": "缺层数将按配置默认层数推算", "missing_default": [],
            "missing_fallback": []}
    msg = _gate_message(comp, 1)
    assert "层数" in msg and "总建筑面积(m²)" in msg, msg
    assert "floors" not in msg and "total_area" not in msg, msg


def test_真实完备性报告里的说明不含内部键名():
    """⚠️ 【第 2 批收口】补的是一个**真洞**，不是重复上面两条。

    上面两条（`test_参数门提示里不许出现内部键名` / `test_缺必要参数时的提问也是中文`）
    用的都是**手搓的 comp dict**：一条给 `"ok": True`，一条自己写死了中文 note ——
    于是它们**完全绕过** `boundary.params_completeness()` 自己生成 note 的那一行。
    实测后果（门第 1 轮原文）：说明里直接甩出

        【floors】缺层数将按配置默认层数推算；【total_area】缺总建筑面积…；
        【foundation_type】…；【structure_type】…

    正是用户投诉过的「不要刻意使用一些英文和专业术语」。本用例走**真函数**，
    并同时覆盖 `note`（硬必要档）与 `default_note`（取默认值档）两条路径。
    """
    comp = B.params_completeness({})
    for raw in ("floors", "total_area", "foundation_type", "structure_type"):
        assert raw not in comp["note"], \
            "内部键名 `%s` 不该出现在缺项说明里：\n%s" % (raw, comp["note"])
    for zh in ("层数", "总建筑面积", "基础类型", "结构形式"):
        assert zh in comp["note"], comp["note"]

    # 取默认值档（note 之外的另一条路径）同样不许露键名。
    comp2 = B.params_completeness({"floors": 1, "total_area": 1,
                                   "foundation_type": "筏板基础",
                                   "structure_type": "框架结构"})
    assert "building_count" not in comp2["default_note"], comp2["default_note"]
    assert "栋数" in comp2["default_note"], comp2["default_note"]


# ======================================================================
# 2. 终端的参数表：键名中文 + 空参数不占版面 + 枚举值转中文
# ======================================================================
def _params():
    return {"project_name": None, "total_area": 1500, "total_concrete": None,
            "building_count": 1, "floors": 3,
            "building_type": "residential", "structure_type": "shear_wall",
            "planned_start_date": "2026-03-01", "quality_target": None}


def test_参数表用中文键名且空参数不占行():
    out = PLAIN(renderer.format_params(_params()))
    assert "总建筑面积(m²)：1500" in out
    assert "栋数：1" in out and "层数：3" in out
    # 空的可选参数（项目名称 / 混凝土总量 / 质量目标）不再各占一行
    assert "项目名称" not in out
    assert "混凝土总量" not in out
    assert "quality_target" not in out and "project_name" not in out
    assert out.count("（缺，请补）") == 0, out


def test_必须补的参数仍然逐行标缺():
    """硬必要的键为空时必须逐行提示（否则用户不知道缺哪个）。"""
    out = PLAIN(renderer.format_params({"floors": None, "total_area": None}))
    assert out.count("（缺，请补）") == 2, out
    assert "层数" in out and "总建筑面积" in out


def test_枚举值转成中文():
    out = PLAIN(renderer.format_params(_params()))
    assert "建筑类型：住宅" in out and "结构形式：剪力墙结构" in out
    assert "residential" not in out and "shear_wall" not in out


def test_没有参数时的退化路径一字不变():
    assert "未提取到参数" in PLAIN(renderer.format_params({}))
    assert "未提取到参数" in PLAIN(renderer.format_params(None))
    # 全空表也不会打出一片空白
    assert "未提取到参数" in PLAIN(renderer.format_params({"project_name": None}))


def test_A6两条通道不把内部结构甩给用户():
    """⚠️ 【第 2 批收口】补的是**真洞**（实测终端原文）。

    修前参数表打出：
        `exclusions：[]`
        `floor_areas：{'source': 'average_assumption', 'unit': 'm²', 'buildings':
         {'default': {...一大坨嵌套字典...}}, 'needs_review': False, 'notes': [...]}`
    —— 裸键名 + Python 数据结构，与用户投诉的
    「不要刻意使用一些英文和专业术语」是同一条毛病。
    `floor_areas` **永远不为空**（用户什么都不给也会带"均摊假设"骨架），
    所以只加进 `_OPTIONAL_PARAMS` 躲不掉，必须专门渲染。
    """
    from pipeline.nodes.extractor import normalize_params
    p = normalize_params({}, "某住宅项目，共12栋，地上38层，总建筑面积12.8万平米")
    out = PLAIN(renderer.format_params(p))
    # 键名不许裸奔
    assert "exclusions" not in out, out
    assert "floor_areas" not in out, out
    # 空容器不占版面
    assert "明确排除项" not in out, out
    # 嵌套字典不许出现（内部结构键名就是它的指纹）
    for raw in ("'source'", "'buildings'", "'needs_review'", "{'default'"):
        assert raw not in out, "内部结构 `%s` 不该出现在参数表里：\n%s" % (raw, out)
    # 但要有中文名 + 一句人话
    assert "分层面积" in out, out
    assert "均摊" in out, out


# ======================================================================
# 3. 第十步：编号选项 + 数字直选
# ======================================================================
def _matrix():
    return {q.DEPTH_COMPONENT: {q.FLOOR_PER_FLOOR: 415, q.FLOOR_PER_5: 116,
                                q.FLOOR_WHOLE: 25},
            q.DEPTH_COARSE: {q.FLOOR_PER_FLOOR: 338, q.FLOOR_PER_5: 99,
                             q.FLOOR_WHOLE: 22}}


def _picker():
    return _picker_payload(_matrix(), q.DEPTH_COMPONENT, q.FLOOR_PER_5)


def test_六个组合选项一次定完():
    """第 38 轮（用户原话）：不要做成"X+X"两轴选项，直接给用户六个选项。"""
    pk = _picker()
    assert [o["no"] for o in pk["options"]] == [1, 2, 3, 4, 5, 6]
    assert pk["max"] == 6
    assert all(o["axis"] == "combo" for o in pk["options"]), pk["options"]
    # 三种楼层分段为外层、两种细度为内层（用户说的"三种施工段 × 细度"）
    assert [(o["floor_grouping"], o["depth"]) for o in pk["options"]] == [
        (q.FLOOR_PER_FLOOR, q.DEPTH_COMPONENT), (q.FLOOR_PER_FLOOR, q.DEPTH_COARSE),
        (q.FLOOR_PER_5, q.DEPTH_COMPONENT), (q.FLOOR_PER_5, q.DEPTH_COARSE),
        (q.FLOOR_WHOLE, q.DEPTH_COMPONENT), (q.FLOOR_WHOLE, q.DEPTH_COARSE)]
    # 每个选项都带真实行数与一句人话（用户要"看得懂"），且行数与矩阵一致
    assert [(o["no"], o["rows"]) for o in pk["options"]] == [
        (1, 415), (2, 338), (3, 116), (4, 99), (5, 25), (6, 22)], pk["options"]
    assert all(o["note"] for o in pk["options"]), pk["options"]


def test_推荐项是组合里的某一个号():
    pk = _picker()
    # 推荐 = 每 5 层一组 + 工序级 = 3 号
    assert pk["recommend"] == [3], pk["recommend"]
    assert pk["max"] == 6
    # 每个选项都带真实行数与一句人话（用户要"看得懂"）
    assert all(o["rows"] > 0 and o["note"] for o in pk["options"]), pk["options"]


@pytest.mark.parametrize("text,expect_depth,expect_group", [
    ("1", q.DEPTH_COMPONENT, q.FLOOR_PER_FLOOR),
    ("2", q.DEPTH_COARSE, q.FLOOR_PER_FLOOR),
    ("3", q.DEPTH_COMPONENT, q.FLOOR_PER_5),
    ("4", q.DEPTH_COARSE, q.FLOOR_PER_5),
    ("5", q.DEPTH_COMPONENT, q.FLOOR_WHOLE),
    ("6", q.DEPTH_COARSE, q.FLOOR_WHOLE),
    ("[3]", q.DEPTH_COMPONENT, q.FLOOR_PER_5),   # 带括号也认
])
def test_数字直选(text, expect_depth, expect_group):
    depth, group, picked = PlanLevelNode._parse_picker(text, picker=_picker())
    assert picked is True, text
    assert depth == expect_depth and group == expect_group, (text, depth, group)


def test_打多个数字以最后一个为准():
    """一个号就是一整档，所以"改口"= 用最后那个号（不再是"两个轴各定一个"）。"""
    depth, group, _ = PlanLevelNode._parse_picker("1 3 6", picker=_picker())
    assert (depth, group) == (q.DEPTH_COARSE, q.FLOOR_WHOLE), (depth, group)


def test_单轴老编号仍能解析():
    """兼容第 32 轮的单轴编号（1-3 楼层、4-5 深度），旧回放不炸。"""
    pk = {"options": [
        {"no": 1, "axis": "floor_grouping", "key": q.FLOOR_PER_FLOOR},
        {"no": 2, "axis": "floor_grouping", "key": q.FLOOR_PER_5},
        {"no": 3, "axis": "floor_grouping", "key": q.FLOOR_WHOLE},
        {"no": 4, "axis": "depth", "key": q.DEPTH_COMPONENT},
        {"no": 5, "axis": "depth", "key": q.DEPTH_COARSE}]}
    assert PlanLevelNode._parse_picker("2", picker=pk)[1] == q.FLOOR_PER_5
    assert PlanLevelNode._parse_picker("5", picker=pk)[0] == q.DEPTH_COARSE


def test_越界编号不认_回落老路径():
    depth, group, picked = PlanLevelNode._parse_picker("9", picker=_picker())
    assert picked is False and depth is None and group is None


def test_长句子里的数字不当选项号():
    """防误判：长句（>12 字）里的数字不认编号（与 L3/L4 的短输入规则一致）。"""
    depth, group, picked = PlanLevelNode._parse_picker(
        "总建筑面积 12 万平米，地上 4 层楼", picker=_picker())
    assert picked is False, (depth, group)


def test_没有picker时编号解析不生效():
    """老后端 / 探针不带 picker → 保持改造前行为（回落 L3/L4 与人话解析）。"""
    assert PlanLevelNode._parse_picker("2", picker=None)[2] is False
    assert PlanLevelNode._parse_picker("", picker=_picker())[2] is False


def test_人话写法继续可用():
    """编号是**新增**路径，老的「整栋」「工种级」写法一个都不许坏。"""
    pk = _picker()
    assert PlanLevelNode._parse_picker("整栋", picker=pk)[2] is False     # 不认编号
    assert PlanLevelNode._parse_choice("工种级") == "L3"
    assert PlanLevelNode._parse_choice("L4") == "L4"
    assert PlanLevelNode._parse_floor_grouping("整栋") == q.FLOOR_WHOLE
    assert PlanLevelNode._parse_floor_grouping("每5层") == q.FLOOR_PER_5


def test_门正文把编号印出来且带推荐星标():
    data = {"purpose": "plan_level", "message": "请选择计划展示粒度",
            "params": {"L3": {"rows": 338}, "L4": {"rows": 415}, "recommend": "L4",
                       "note": "选「工种级（L3）」后，就不能再逐个调具体工序（L4）的班组与定额了"},
            "options": {
                "floor_grouping": [{"key": g, "label": q.FLOOR_LABELS[g],
                                    "rows": _matrix()[q.DEPTH_COMPONENT][g]}
                                   for g in q.FLOOR_GROUPINGS],
                "depth": [{"key": d, "label": q.DEPTH_LABELS[d],
                           "rows": _matrix()[d][q.FLOOR_PER_5]} for d in q.DEPTHS],
                "recommend": {"depth": q.DEPTH_COMPONENT, "floor_grouping": q.FLOOR_PER_5,
                              "rows": 116},
                "recommend_reason": "按层展示 415 行，超过 400 行阈值 —— 建议「每 5 层一组」",
                "blocked": ""},
            "picker": _picker(),
            "matrix": _matrix()}
    out = PLAIN(renderer.render_param_review(data))
    for no in ("1.", "2.", "3.", "4.", "5.", "6."):
        assert no in out, "编号 %s 必须印出来：\n%s" % (no, out)
    assert "六个选项" in out, out
    assert "每 5 层一组 · 工序级（细）" in out, out
    assert "116 行" in out and "99 行" in out and "22 行" in out, out
    assert "★推荐" in out
    assert "输入一个代号" in out, out
    # 六个选项本身就是那张行数矩阵，不再重复印一遍
    assert "行数矩阵" not in out, out
    # 门上不许出现 L3/L4 之外的内部编号当选项（用户只看到中文名）
    assert "L3" in out and "工种级" in out, "note 里的 L3 必须带中文解释"


def test_没有picker时门正文保持老写法():
    data = {"purpose": "plan_level", "message": "请选择计划展示粒度（两个维度）",
            "params": {"note": ""},
            "options": {
                "floor_grouping": [{"key": g, "label": q.FLOOR_LABELS[g], "rows": 10}
                                   for g in q.FLOOR_GROUPINGS],
                "depth": [{"key": d, "label": q.DEPTH_LABELS[d], "rows": 20}
                          for d in q.DEPTHS],
                "recommend": {"depth": q.DEPTH_COMPONENT,
                              "floor_grouping": q.FLOOR_PER_FLOOR, "rows": 10},
                "recommend_reason": "", "blocked": ""},
            "matrix": {}}
    out = PLAIN(renderer.render_param_review(data))
    assert "① 楼层分组（行数主杠杆）" in out
    assert "输 Y=用推荐值" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
