"""计划细度门（pipeline.nodes.plan_level）与可信度口径修正的测试。

覆盖：
  1. 选 L4 → ctx["plan_level"]=="L4"，无 display_rollup
  2. 选 L3 → ctx["plan_level"]=="L3"，display_rollup 为 True
  3. 行数预估准确：rows_l4 == 真实叶子数，rows_l3 == quantity.estimate_row_counts
     （自造树 + 仓库里的真计划 backend/plans/plan_run_1789567958.json）
  4. passed=True（打 Y）→ 用推荐值 L4
  5. manual_input "3"/"L3" → L3；"L4" → L4（含大小写/中文/裸数字等怪格式）
  6. action=="abort" → 返回值里有 _stop
  7. 无 registry（单测场景）不崩、用推荐值
  8. 无 registry / 无交互时绝不抛异常
  9. 可信度口径：wbs_source="llm" → quantity.origin=="ai"；
     已有 provenance.quantity.origin=="user" → 保持 user 不被覆盖
 10. credibility 三个键固定 user/kb/ai 且相加 == 1.0（1e-6 内）

无需网络 / 真实 LLM；定额锚定部分只依赖仓库随附的 BuildPlan_KB/kb.db。
"""

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import quantity
from pipeline.nodes.norm_bind import NormBindNode
from pipeline.nodes.plan_level import LEVEL_L3, LEVEL_L4, PlanLevelNode

# 仓库里真实跑出来的计划（WBS 三层树，343 条 L4 叶子 / 86 条 L3 汇总行）
REAL_PLAN = BACKEND / "plans" / "plan_run_1789567958.json"


# ==================== 构造最小 WBS / 交互桩 ====================
def _leaf(tid, work_type="钢筋工程", qty=100, unit="t", dur=5, **kw):
    leaf = {"id": tid, "name": "任务" + tid, "quantity": qty, "unit": unit,
            "duration_days": dur, "work_type": work_type,
            "kb_activity_id": None}
    leaf.update(kw)
    return leaf


def _wbs():
    """4 条 L4 叶子、3 组 L3（1.1 下两个工种 → 2 组；1.2 下 1 组）。"""
    return {"phases": [{"phase": "主体结构", "work_packages": [
        {"id": "1.1", "name": "钢筋工程", "sub_packages": [
            _leaf("1.1.1", work_type="钢筋工"),
            _leaf("1.1.2", work_type="钢筋工"),
            _leaf("1.1.3", work_type="模板工", unit="m2"),
        ]},
        {"id": "1.2", "name": "混凝土工程", "sub_packages": [
            _leaf("1.2.1", work_type="混凝土工", unit="m3"),
        ]},
    ]}]}


def _all_leaves(ctx):
    return [s for ph in ctx["wbs"]["phases"]
            for wp in ph["work_packages"] for s in wp["sub_packages"]]


class _StubReg(object):
    """最小交互登记桩：register 记账，wait 直接返回预置决策（不阻塞）。"""

    def __init__(self, decision):
        self.decision = decision
        self.registered = []

    def register(self, key):
        self.registered.append(key)

    def wait(self, key, cancel_evt=None, timeout=600):
        return dict(self.decision)


def _plan_node(decision=None, with_registry=True):
    """返回 (节点, 事件列表)。decision=None → 等价"打 Y 通过"。"""
    node = PlanLevelNode()
    events = []
    node._emit = lambda event, data: events.append((event, data))
    node._run_id = "t_plan"
    if with_registry:
        node._registry = _StubReg({"passed": True} if decision is None else decision)
    return node, events


def _run_plan(ctx, decision=None, with_registry=True):
    node, events = _plan_node(decision, with_registry)
    out = node.run(ctx)
    return node, events, out


def _ctx(wbs=None, params=None):
    return {"wbs": wbs if wbs is not None else _wbs(),
            "extracted_params": params if params is not None else {}}


# ==================== 1 / 4. 选 L4 ====================
def test_choose_l4_no_rollup():
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": "L4"})

    assert ctx["plan_level"] == LEVEL_L4
    info = ctx["plan_level_info"]
    assert info["chosen"] == "L4"
    assert info["recommended"] == "L4"
    # 选 L4 时不许给下游交付环节留 display_rollup
    assert info.get("display_rollup") in (None, False)
    assert "display_rollup" not in info
    assert ctx.get("plan_level_warning") is None


def test_y_press_uses_recommended_l4():
    """打 Y → terminal 发 passed=True、无 manual_input → 采信推荐值。"""
    ctx = _ctx()
    _run_plan(ctx, {"passed": True})

    assert ctx["plan_level"] == LEVEL_L4
    assert ctx["plan_level_info"]["chosen"] == "L4"
    assert "display_rollup" not in ctx["plan_level_info"]
    assert ctx.get("plan_level_warning") is None, "打 Y 是正常路径，不该记 warning"


def test_empty_decision_uses_recommended_l4():
    """passed=False 且没有任何输入（EOF 路径）→ 用推荐值，不崩、不记 warning。"""
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": ""})

    assert ctx["plan_level"] == LEVEL_L4
    assert ctx.get("plan_level_warning") is None


# ==================== 2. 选 L3 ====================
def test_choose_l3_sets_display_rollup():
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": "L3"})

    assert ctx["plan_level"] == LEVEL_L3
    info = ctx["plan_level_info"]
    assert info["chosen"] == "L3"
    assert info["recommended"] == "L4"          # 推荐值仍是 L4，用户改选 L3
    assert info["display_rollup"] is True
    assert "无法逐个修改" in info["note"]


def test_node_never_touches_wbs_tree():
    """只记录选择，不改 WBS：选 L3 后叶子数、id 必须原样不变。"""
    wbs = _wbs()
    before = json.dumps(wbs, ensure_ascii=False, sort_keys=True)
    ctx = _ctx(wbs=wbs)
    _run_plan(ctx, {"passed": False, "manual_input": "L3"})

    assert json.dumps(ctx["wbs"], ensure_ascii=False, sort_keys=True) == before


# ==================== 3. 行数预估准确 ====================
def test_row_counts_match_quantity_module():
    ctx = _ctx()
    _run_plan(ctx, {"passed": True})

    l3, l4 = quantity.estimate_row_counts(ctx["wbs"])
    assert (ctx["plan_level_info"]["rows_l3"], ctx["plan_level_info"]["rows_l4"]) == (l3, l4)
    # 自造树：4 条叶子、3 组 L3
    assert ctx["plan_level_info"]["rows_l4"] == len(_all_leaves(ctx)) == 4
    assert ctx["plan_level_info"]["rows_l3"] == 3


@pytest.mark.skipif(not REAL_PLAN.exists(), reason="仓库里没有真计划文件")
def test_row_counts_on_real_plan_wbs():
    with open(str(REAL_PLAN), "r", encoding="utf-8") as f:
        plan = json.load(f)
    wbs = plan["wbs"]

    ctx = _ctx(wbs=wbs)
    _run_plan(ctx, {"passed": True})

    l3, l4 = quantity.estimate_row_counts(wbs)
    leaves = [s for ph in wbs["phases"]
              for wp in ph["work_packages"] for s in wp["sub_packages"]]
    info = ctx["plan_level_info"]
    assert info["rows_l4"] == l4 == len(leaves)          # L4 行数 = 真实叶子数
    assert info["rows_l3"] == l3
    assert info["rows_l4"] > info["rows_l3"] > 0
    # 与"计划细度"门自己的口径一致（不是估的，是数出来的）
    assert info["rows_l3"] == len({(wp.get("id"), (leaf.get("work_type") or "未分类"))
                                   for ph in wbs["phases"]
                                   for wp in ph["work_packages"]
                                   for leaf in wp["sub_packages"]})


# ==================== 5. 决策解析宽容 ====================
@pytest.mark.parametrize("text", ["L3", "l3", "l-3", "L 3", "三级", "选3", "要L3级"])
def test_manual_input_l3_variants(text):
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": text})
    assert ctx["plan_level"] == LEVEL_L3, text
    assert ctx["plan_level_info"]["display_rollup"] is True


@pytest.mark.parametrize("text", ["L4", "l4", "四级", "选4", "用L4计划", "保持 L4"])
def test_manual_input_l4_variants(text):
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": text})
    assert ctx["plan_level"] == LEVEL_L4, text
    assert "display_rollup" not in ctx["plan_level_info"]


# 第 38 轮语义变更（用户拍板「直接给用户六个选项」）：
# 门上印的是六个**组合**编号，所以**裸数字 = 打代号**，不再有"3=L3、4=L4"这套暗号。
# 想要单纯切细度请用 L3/L4/工种级/工序级（见上面两组），裸数字一律按门上的六个号解释。
@pytest.mark.parametrize("text,expect_depth,expect_group,expect_level", [
    ("1", quantity.DEPTH_COMPONENT, quantity.FLOOR_PER_FLOOR, LEVEL_L4),
    ("2", quantity.DEPTH_COARSE, quantity.FLOOR_PER_FLOOR, LEVEL_L3),
    ("3", quantity.DEPTH_COMPONENT, quantity.FLOOR_PER_5, LEVEL_L4),
    ("4", quantity.DEPTH_COARSE, quantity.FLOOR_PER_5, LEVEL_L3),
    ("5", quantity.DEPTH_COMPONENT, quantity.FLOOR_WHOLE, LEVEL_L4),
    ("6", quantity.DEPTH_COARSE, quantity.FLOOR_WHOLE, LEVEL_L3),
])
def test_bare_digit_means_the_printed_option_number(text, expect_depth,
                                                    expect_group, expect_level):
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": text})
    g = ctx["display_granularity"]
    assert (g["depth"], g["floor_grouping"]) == (expect_depth, expect_group), text
    assert ctx["plan_level"] == expect_level, text
    assert ctx["plan_level_info"]["chosen"] == expect_level


def test_printed_picker_is_actually_honored():
    """回归：终端**印出来**的那一列编号必须真的被解析。

    第 32 轮埋下、第 38 轮才发现的真缺陷：`_ask` 把 `picker` 塞进了 payload，`run()` 却用
    `self._parse_picker(manual, message, picker, ...)` **位置传参** —— 而签名是
    `(*texts, picker=None)`，于是 `picker` 被当成第 3 段文本、解析器里的 `opts` 永远是
    空，编号直选**静默失效**（用户敲 `2` 会得到"没看懂"+推荐值）。
    这条测试不认内部实现，只认"门上印了什么、敲它就得到什么"。
    """
    ctx = _ctx()
    _node, events, _out = _run_plan(ctx, {"passed": True})      # 先跑一遍拿门上的 picker
    data = [d for e, d in events if e == "param_review"][-1]
    picker = data["picker"]
    assert [o["no"] for o in picker["options"]] == [1, 2, 3, 4, 5, 6]
    for opt in picker["options"]:
        c2 = _ctx()
        _run_plan(c2, {"passed": False, "manual_input": str(opt["no"])})
        g = c2["display_granularity"]
        assert (g["depth"], g["floor_grouping"]) == (opt["depth"],
                                                     opt["floor_grouping"]), opt
        # 门上那行写的行数，与敲完之后真算出来的行数必须一致
        assert g["rows"] == opt["rows"], opt


def test_message_field_is_also_parsed():
    """有的上行走 message 而不是 manual_input —— 一样要认。"""
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "message": "我要 L3"})
    assert ctx["plan_level"] == LEVEL_L3


def test_last_mention_wins():
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": "先想 L3，还是 L4 吧"})
    assert ctx["plan_level"] == LEVEL_L4


def test_unparseable_input_falls_back_with_warning():
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": "随便啦你看着办"})
    assert ctx["plan_level"] == LEVEL_L4                  # 用推荐值
    assert "plan_level_warning" in ctx
    assert "随便啦你看着办" in ctx["plan_level_warning"]


def test_long_sentence_with_digit_is_not_misread():
    """长句里的裸数字不该被当成细度（'4 层楼' 不是选 L4）。"""
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": "这栋楼一共4层，进度你看着办就行"})
    assert ctx["plan_level"] == LEVEL_L4                  # 走推荐值（不是误判成 L4）
    assert "plan_level_warning" in ctx                    # 如实记一条"没看懂"


# ==================== 6. 取消 ====================
def test_abort_returns_stop():
    ctx = _ctx()
    _node, _events, out = _run_plan(ctx, {"action": "abort"})

    assert isinstance(out, dict)
    assert "_stop" in out
    assert "计划细度" in out["_stop"]
    assert "plan_level" not in ctx, "取消时不该写计划细度"
    assert "plan_level_info" not in ctx


# ==================== 7 / 8. 无 registry 不崩 ====================
def test_without_registry_uses_recommended_and_never_raises():
    ctx = _ctx()
    node, events, out = _run_plan(ctx, None, with_registry=False)

    assert ctx["plan_level"] == LEVEL_L4
    assert ctx["plan_level_info"]["chosen"] == "L4"
    assert out["plan_level"] == "L4"
    assert node.done_summary
    # 没有交互登记时不许发无人应答的 param_review 门
    assert all(e != "param_review" for e, _d in events)


@pytest.mark.parametrize("bad", [None, {}, {"wbs": None}, {"wbs": []},
                                 {"wbs": {"phases": "x"}},
                                 {"wbs": {"phases": [{"work_packages": None}]}}])
def test_node_survives_broken_ctx(bad):
    """ctx 里全是垃圾也不许抛异常（引擎会把异常升级成整条流水线失败）。"""
    n = PlanLevelNode()
    n._emit = lambda event, data: None
    result = n.run(bad)
    assert isinstance(result, dict)
    assert result.get("plan_level") == "L4"


# ==================== 两个独立维度：解析与写回 ====================
# 产品口径：展示粒度 = ① 工序拆解深度 × ② 楼层分组。二者正交。
# 实测（潭村 12 栋 415 条叶子）行数矩阵：
#     工序级  按层 415 ｜ 每5层 116 ｜ 整栋 25
#     工种级  按层 338 ｜ 每5层  99 ｜ 整栋 22
# 行数主杠杆在**楼层分组**（16 倍），L3/L4 只省 18%。
def test_parse_floor_grouping_recognizes_the_three_options():
    parse = PlanLevelNode._parse_floor_grouping
    for text in ("整栋", "全栋", "不分层", "整体汇总", "整栋展示"):
        assert parse(text) == quantity.FLOOR_WHOLE, text
    for text in ("每5层", "每五层", "5层一组", "五层一组"):
        assert parse(text) == quantity.FLOOR_PER_5, text
    for text in ("按层", "逐层", "每层", "分层展示"):
        assert parse(text) == quantity.FLOOR_PER_FLOOR, text


def test_parse_floor_grouping_accepts_bare_digits_but_not_ambiguously():
    """短输入里的裸数字：5→每5层，1→按层。**3/4 留给深度轴**（避免"整栋=3"打架）。"""
    parse = PlanLevelNode._parse_floor_grouping
    assert parse("5") == quantity.FLOOR_PER_5
    assert parse("1") == quantity.FLOOR_PER_FLOOR
    assert parse("3") is None, "3 是深度轴的写法，楼层轴不许抢"
    assert parse("4") is None


def test_parse_floor_grouping_returns_none_when_unrecognized():
    """认不出就返回 None（宁可用推荐值 + 记 warning，也不猜）。"""
    parse = PlanLevelNode._parse_floor_grouping
    for bad in ("", None, "随便啦你看着办", "好看一点", "L4"):
        assert parse(bad) is None, repr(bad)


def test_choosing_whole_building_keeps_depth_and_sets_rollup():
    """只说「整栋」→ 只动楼层轴，深度轴保持推荐值，并标记需要汇总渲染。"""
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": "整栋"})
    g = ctx["display_granularity"]
    assert g["floor_grouping"] == quantity.FLOOR_WHOLE
    assert g["depth"] == quantity.DEPTH_COMPONENT, "没提深度就不该动深度"
    assert ctx["plan_level"] == LEVEL_L4
    assert ctx["plan_level_info"]["display_rollup"] is True
    assert ctx.get("plan_level_warning") is None, "识别成功不该记 warning"


def test_choosing_both_axes_at_once():
    """「工种级 整栋」→ 两个轴同时生效。"""
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": "工种级 整栋"})
    g = ctx["display_granularity"]
    assert g["depth"] == quantity.DEPTH_COARSE
    assert g["floor_grouping"] == quantity.FLOOR_WHOLE
    assert ctx["plan_level"] == LEVEL_L3, "工种级对应 L3（向后兼容映射）"


def test_depth_axis_accepts_plain_chinese():
    """深度轴要认人话（工种级/粗 ↔ 工序级/细），不能只认 L3/L4。"""
    for text, want in (("工种级", LEVEL_L3), ("粗粒度", LEVEL_L3),
                       ("工序级", LEVEL_L4), ("明细", LEVEL_L4)):
        ctx = _ctx()
        _run_plan(ctx, {"passed": False, "manual_input": text})
        assert ctx["plan_level"] == want, text


def test_component_level_is_refused_not_silently_downgraded():
    """点名「构件级展开」→ 必须明确告知不可用，**不能**悄悄当成工序级放行。

    产品想要的"更细一档"是拆到柱/墙/梁/板，但参数里没有分部工程量比例。
    假装能拆就是在编数据。
    """
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": "构件级"})
    w = ctx["plan_level_warning"]
    assert "构件级" in w and "分部" in w, w
    assert ctx["display_granularity"]["depth"] != quantity.DEPTH_COARSE, \
        "拒绝之后应当仍用推荐深度，而不是掉到另一个档位"


def test_unrecognized_input_warns_about_granularity_not_params():
    """看不懂时提示语要说"展示粒度"，不能再说"计划细度"（口径变了）。"""
    ctx = _ctx()
    _run_plan(ctx, {"passed": False, "manual_input": "随便啦你看着办"})
    w = ctx["plan_level_warning"]
    assert "展示粒度" in w
    assert "工序级" in w or "按层" in w, "推荐值要写清是哪个组合"


def test_info_carries_the_row_matrix_and_blocked_note():
    """矩阵与受阻项都要写进 info（交付物与终端都要读）。"""
    ctx = _ctx()
    _run_plan(ctx, {"passed": True})
    info = ctx["plan_level_info"]
    m = info["row_matrix"]
    assert set(m) == {"component", "coarse"}
    for d in m:
        assert set(m[d]) == {"per_floor", "per_5", "whole"}
    assert info["rows_chosen"] >= 1
    assert "构件级" in info["blocked"], "受阻项必须如实告知"
    assert info["depth_label"] and info["floor_grouping_label"]


def _wbs_with_floors(floors=10):
    """10 层 × 每层 2 道工序 + 1 条不分层任务 = 21 条叶子，用来验证楼层分组。"""
    leaves = []
    for f in range(1, floors + 1):
        leaves.append(_leaf("5.1.%d.1" % f, work_type="钢筋工程", unit="t", qty=10,
                            location="Ⅰ区 %d-%d层" % (f, f),
                            name="Ⅰ区 %d-%d层 钢筋绑扎" % (f, f), _step_name="钢筋绑扎"))
        leaves.append(_leaf("5.1.%d.2" % f, work_type="模板工程", unit="m2", qty=200,
                            location="Ⅰ区 %d-%d层" % (f, f),
                            name="Ⅰ区 %d-%d层 模板安装" % (f, f), _step_name="模板安装"))
    leaves.append(_leaf("1.1.1", work_type="土建临建", unit="项", qty=1,
                        location="全楼", name="施工准备", _step_name="施工准备"))
    return {"phases": [{"phase": "主体结构", "work_packages": [
        {"id": "5.1", "name": "Ⅰ区主体", "sub_packages": leaves}]}]}


def test_rows_shrink_as_floor_grouping_gets_coarser():
    """真实性质：楼层分组越粗，行数越少（这就是给用户看那个矩阵的依据）。

    必须用**带楼层信息**的 WBS —— 没有楼层字样的任务归"分层外"、不参与楼层分组，
    那时三个档位行数相同，这条性质根本测不出来（夹具本身就把结论架空）。
    """
    ctx = _ctx(wbs=_wbs_with_floors())
    _run_plan(ctx, {"passed": True})
    m = ctx["plan_level_info"]["row_matrix"]["component"]
    assert m["per_floor"] > m["per_5"] > m["whole"], m


def test_recommend_grouping_falls_back_when_too_many_rows():
    """按层超过阈值 → 推荐每 5 层一组，并把三个档位的行数都写进理由。"""
    from pipeline.nodes.plan_level import ROW_THRESHOLD_L3, recommend_grouping_for
    big = {"component": {"per_floor": ROW_THRESHOLD_L3 + 1, "per_5": 116, "whole": 25}}
    g, reason = recommend_grouping_for("component", big)
    assert g == quantity.FLOOR_PER_5
    assert "116" in reason and "25" in reason
    small = {"component": {"per_floor": 20, "per_5": 6, "whole": 2}}
    g2, _ = recommend_grouping_for("component", small)
    assert g2 == quantity.FLOOR_PER_FLOOR


def test_recommend_grouping_handles_empty_matrix():
    from pipeline.nodes.plan_level import recommend_grouping_for
    for bad in ({}, None, {"component": {}}):
        g, reason = recommend_grouping_for("component", bad)
        assert g == quantity.FLOOR_PER_FLOOR and isinstance(reason, str)


# ==================== 事件载荷 ====================
def test_event_payload_has_row_counts_and_purpose():
    ctx = _ctx()
    _node, events, _out = _run_plan(ctx, {"passed": True})

    reviews = [d for e, d in events if e == "param_review"]
    assert len(reviews) == 1
    data = reviews[0]
    assert data["purpose"] == "plan_level"
    assert "展示粒度" in data["message"]
    assert data["review_id"].startswith("pl_t_plan_")
    # ⚠️ 老形状必须保持不变（向后兼容）——终端与既有调用方都依赖它
    params = data["params"]
    assert params["L3"]["rows"] == 3 and params["L4"]["rows"] == 4
    assert params["recommend"] == "L4"
    assert params["param_level"] == "unknown"
    # 第 32 轮：note 里的 L3/L4 编号都补了中文（用户不认识这两个编号）
    assert "不能再逐个调具体工序" in params["note"]


def test_event_payload_carries_the_two_axes():
    """载荷必须带上**两个独立维度**的选项与真实行数 —— 这是本门存在的意义。

    只给一个"L3/L4"开关表达不了"我要工序级、但楼层按五层归组"这种诉求；
    而实测行数的主杠杆恰好在楼层分组那一维（415→116→25）。
    """
    data = [d for e, d in _run_plan(_ctx(), {"passed": True})[1]
            if e == "param_review"][0]
    opts = data["options"]
    assert opts["blocked"] and "构件级" in opts["blocked"], "受阻项要如实说明"
    assert opts["recommend"]["depth"] in ("component", "coarse")
    assert opts["recommend"]["floor_grouping"] in ("per_floor", "per_5", "whole")
    groups = dict((o["key"], o["rows"]) for o in opts["floor_grouping"])
    assert set(groups) == {"per_floor", "per_5", "whole"}
    assert groups["per_floor"] >= groups["per_5"] >= groups["whole"], groups
    depths = dict((o["key"], o["rows"]) for o in opts["depth"])
    assert set(depths) == {"component", "coarse"}
    assert depths["component"] >= depths["coarse"], depths
    # 矩阵也要给全（六个数字）
    m = data["matrix"]
    assert set(m) == {"component", "coarse"}
    for d in m:
        assert set(m[d]) == {"per_floor", "per_5", "whole"}


def test_registry_key_is_registered_before_wait():
    node, _events = _plan_node({"passed": True})
    node.run(_ctx())
    assert len(node._registry.registered) == 1


# ==================== 参数层级判定 ====================
def test_param_level_detects_l3_totals():
    ctx = _ctx(params={"total_concrete": 8200, "total_rebar": 1200,
                       "building_type": "住宅"})
    _run_plan(ctx, {"passed": True})
    assert ctx["plan_level_info"]["param_level"] == "L3"


def test_param_level_detects_l4_activities():
    ctx = _ctx(params={"activities": [{"name": "梁钢筋", "quantity": 12}],
                       "total_concrete": 8200})
    _run_plan(ctx, {"passed": True})
    assert ctx["plan_level_info"]["param_level"] == "L4"


def test_param_level_unknown_when_only_metadata():
    ctx = _ctx(params={"building_type": "住宅", "planned_start_date": "2024-03-01"})
    _run_plan(ctx, {"passed": True})
    assert ctx["plan_level_info"]["param_level"] == "unknown"


def test_param_level_unknown_without_params():
    ctx = _ctx(params={})
    _run_plan(ctx, {"passed": True})
    assert ctx["plan_level_info"]["param_level"] == "unknown"


# ==================== 9. 可信度口径：工程量来源 ====================
def _norm_ctx(wbs, wbs_source=None):
    ctx = {"wbs": wbs, "prompt": "建一栋住宅楼。", "extracted_params": {}}
    if wbs_source is not None:
        ctx["wbs_source"] = wbs_source
    return ctx


def _run_norm(ctx):
    node = NormBindNode(llm=None)
    node._emit = lambda event, data: None
    node.run(ctx)
    return node


def test_quantity_origin_reflects_wbs_source_llm():
    """wbs_source=llm → 工程量是大模型给的，必须标 ai，不能再算成 user。"""
    wbs = {"phases": [{"phase": "主体", "work_packages": [
        {"id": "1.1", "name": "钢筋", "sub_packages": [_leaf("1.1.1")]}]}]}
    ctx = _norm_ctx(wbs, wbs_source="llm")
    _run_norm(ctx)

    q = wbs["phases"][0]["work_packages"][0]["sub_packages"][0]["provenance"]["quantity"]
    assert q["origin"] == "ai"
    assert q["ref"] == "WBS 由大模型生成"
    assert q["confidence"] == "低"
    assert q["value"] == 100


def test_existing_user_quantity_provenance_is_kept():
    """上游已标注 origin=user（如由用户总量推算）→ 必须原样保留，不许覆盖。"""
    keep = {"value": 8200, "origin": "user", "ref": "用户输入",
            "confidence": "高", "note": "用户给了总钢筋量"}
    leaf = _leaf("1.1.1", quantity=8200, provenance={"quantity": dict(keep)})
    wbs = {"phases": [{"phase": "主体", "work_packages": [
        {"id": "1.1", "name": "钢筋", "sub_packages": [leaf]}]}]}
    ctx = _norm_ctx(wbs, wbs_source="llm")
    _run_norm(ctx)

    q = leaf["provenance"]["quantity"]
    assert q["origin"] == "user", "已有的用户来源不许被 wbs_source 覆盖"
    assert q["value"] == 8200
    assert q["note"] == "用户给了总钢筋量"


def test_credibility_counts_ai_and_user_separately():
    """一条 llm 生成（ai）+ 一条上游标注的用户量（user）→ 两条都要如实计数。"""
    preserved = {"quantity": {"value": 50, "origin": "user", "ref": "用户输入",
                              "confidence": "高", "note": ""}}
    a = _leaf("1.1.1", qty=100)
    b = _leaf("1.1.2", qty=50, provenance=preserved)
    wbs = {"phases": [{"phase": "主体", "work_packages": [
        {"id": "1.1", "name": "钢筋", "sub_packages": [a, b]}]}]}
    ctx = _norm_ctx(wbs, wbs_source="llm")
    _run_norm(ctx)

    assert a["provenance"]["quantity"]["origin"] == "ai"
    assert b["provenance"]["quantity"]["origin"] == "user"
    cred = ctx["credibility"]
    assert set(cred) == {"user", "kb", "ai"}
    # 2 条 quantity(ai,user) + 2 条 norm(ai) + 2 条 duration(ai) = user 1 / ai 5
    assert cred["user"] == 0.17 and cred["ai"] == 0.83
    assert abs(sum(cred.values()) - 1.0) < 1e-6


def test_credibility_sums_to_one_and_keys_are_fixed():
    """default / unknown 归到 ai 里，键名永远只有 user/kb/ai，比例严格相加 = 1。"""
    tmpl = _leaf("1.1.1", work_type="模板工", unit="m2")
    unknown = _leaf("1.1.2", work_type="混凝土工", unit="m3")
    wbs_t = {"phases": [{"phase": "主体", "work_packages": [
        {"id": "1.1", "name": "模板", "sub_packages": [tmpl]}]}]}
    ctx_t = _norm_ctx(wbs_t, wbs_source="template")
    _run_norm(ctx_t)
    q_t = tmpl["provenance"]["quantity"]
    assert q_t["origin"] == "default" and "模板" in q_t["ref"]
    assert set(ctx_t["credibility"]) == {"user", "kb", "ai"}
    assert abs(sum(ctx_t["credibility"].values()) - 1.0) < 1e-6
    assert ctx_t["credibility"]["user"] == 0.0        # 模板量不再算成用户提供

    wbs_u = {"phases": [{"phase": "主体", "work_packages": [
        {"id": "1.1", "name": "混凝土", "sub_packages": [unknown]}]}]}
    ctx_u = _norm_ctx(wbs_u, wbs_source="")
    _run_norm(ctx_u)
    q_u = unknown["provenance"]["quantity"]
    assert q_u["origin"] == "unknown" and q_u["ref"] == "来源未标注"
    assert set(ctx_u["credibility"]) == {"user", "kb", "ai"}
    assert abs(sum(ctx_u["credibility"].values()) - 1.0) < 1e-6
    assert ctx_u["credibility"]["user"] == 0.0


def test_rollup_leaf_quantity_marked_ai():
    """L3 汇总行（带 _rollup）的量是汇总/拆分出来的 → origin=ai，note 写"L3 汇总行"。"""
    rollup = _leaf("1.1.L3.1", work_type="钢筋工", qty=300,
                   _rollup={"quantity": 300, "duration_rule": "时间跨度", "member_count": 2})
    wbs = {"phases": [{"phase": "主体", "work_packages": [
        {"id": "1.1", "name": "钢筋", "sub_packages": [rollup]}]}]}
    ctx = _norm_ctx(wbs, wbs_source="llm")
    _run_norm(ctx)

    q = rollup["provenance"]["quantity"]
    assert q["origin"] == "ai"
    assert q["note"] == "L3 汇总行"


def test_credibility_never_exceeds_one():
    """可信度三比例必须都在 [0,1] 且相加 = 1（浮点 1e-6 内）。"""
    wbs = {"phases": [{"phase": "主体", "work_packages": [
        {"id": "1.1", "name": "钢筋", "sub_packages": [
            _leaf("1.1.%d" % i, work_type="钢筋工") for i in range(1, 8)]}]}]}
    ctx = _norm_ctx(wbs, wbs_source="llm")
    _run_norm(ctx)

    cred = ctx["credibility"]
    assert set(cred) == {"user", "kb", "ai"}
    assert all(0.0 <= v <= 1.0 for v in cred.values())
    assert abs(sum(cred.values()) - 1.0) < 1e-6


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-q"]))
