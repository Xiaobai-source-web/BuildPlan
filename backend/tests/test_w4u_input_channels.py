# -*- coding: utf-8 -*-
r"""W4-U 输入侧四条通道的回归测试（一次跑完，分四节）。

运行：python -m pytest tests/test_w4u_input_channels.py -q --basetemp=_test_tmp\w4u

节次
----
1. 单位规范化贯通（G5 收口）：`㎡`(U+33A1) 只允许留在**输入侧**识别表/正则里；
   凡是打给用户看的产物文案与产物 JSON，单位一律规范形 `m²`。
2. 施工段用户覆盖通道（`segment_rule`）抽取侧：
   只产出消费侧（`org_plan._normalize_user_rule`）**真的认**的形状；
   `floor_overrides` 在抽取侧用 `floor_areas` 展平，展不出 → 待确认且**不产出规则键**。
3. `extractor` 改调 `kb.match_*` —— **本文件不测**（P1 的 `kb.py` 未落地，见报告 BLOCKERS）。
4. 模板 / 砌体参数管道（`total_formwork` / `total_masonry`）。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
for _p in (str(BACKEND), str(ROOT / "terminal")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipeline import kb                                           # noqa: E402
from pipeline import kb_units                                   # noqa: E402
from pipeline import org_plan                                   # noqa: E402
from pipeline import scope_inputs as SI                         # noqa: E402
from pipeline.nodes import boundary as B                        # noqa: E402
from pipeline.nodes.boundary import BoundaryNode                # noqa: E402
from pipeline.nodes.extractor import extract_by_regex, normalize_params  # noqa: E402
from pipeline.scope_inputs import build_floor_areas             # noqa: E402

U33A1 = chr(0x33A1)          # ㎡ —— CJK 兼容字形（输入侧宽容，输出侧禁用）
M2 = "m²"
SAMPLE3 = ROOT / "项目样例" / "示例3_住宅楼_对比版.txt"


def _u33a1_count(obj):
    """递归数产物里还有几处 U+33A1。"""
    if isinstance(obj, str):
        return obj.count(U33A1)
    if isinstance(obj, dict):
        return sum(_u33a1_count(k) + _u33a1_count(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return sum(_u33a1_count(v) for v in obj)
    return 0


class _StubLLM(object):
    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, system, user, temperature=0.3, retries=1):
        return self.payload


# ══════════════════════════════════════════════════════════════════
# 1. 单位规范化贯通（G5 收口）
# ══════════════════════════════════════════════════════════════════
class TestUnitNormalization:
    def test_参数标签用规范形m2且不再含U33A1(self):
        """断言语义：参数门上打给用户看的单位是 `m²`（U+00B2），不是 `㎡`（U+33A1）。"""
        assert B.param_label("total_area") == "总建筑面积(m²)"
        assert B.param_label("total_formwork") == "模板总量(m²)"
        assert B.param_label("total_masonry") == "砌体总量(m³)"
        bad = [k for k, v in B.PARAM_LABELS.items() if U33A1 in str(v)]
        assert not bad, "这些标签仍用 CJK 兼容字形：%s" % bad

    def test_终端renderer那张表同步改成m2(self):
        """断言语义：终端进程不 import 后端，两张 `_PARAM_LABELS` 必须同步。"""
        import renderer
        assert renderer._PARAM_LABELS["total_area"] == "总建筑面积(m²)"
        assert U33A1 not in renderer._PARAM_LABELS["total_area"]
        assert renderer._PARAM_LABELS["total_formwork"] == "模板总量(m²)"
        assert renderer._PARAM_LABELS["total_masonry"] == "砌体总量(m³)"

    def test_输入侧的U33A1识别能力不许被删掉(self):
        """断言语义：清零只针对**输出侧** —— 用户文档里写 `㎡` 照样要认出来。"""
        assert kb_units.normalize_unit(U33A1) == M2
        assert kb_units.normalize_unit("m2") == M2
        assert U33A1 in SI._UNIT            # 面积正则仍然容忍 ㎡

    def test_输入写U33A1也能抽出参数(self):
        """断言语义：`总建筑面积12万㎡` 必须抽出 120000（输入侧宽容）。"""
        p = extract_by_regex("总建筑面积12万" + U33A1 + "，地上 18 层")
        assert p["total_area"] == 120000, p

    def test_抽取产物里不再残留U33A1(self):
        """断言语义：`extract_by_regex` + `normalize_params` 的产物里 0 处 U+33A1。"""
        text = ("总建筑面积12万" + U33A1 + "，混凝土5.2万m³，钢筋7.5万吨，"
                "模板25000平方米，砌体3000立方米，地上 18 层")
        merged = normalize_params({"total_area": 120000}, text)
        assert _u33a1_count(merged) == 0, merged

    def test_材料汇总已删除且单位真源仍规范(self):
        """断言语义（【第 2 批 · 域 2 / 2.6】已改写）：`mat_map` **整段删除**。

        原来是"断言语义：`plan_assembler` 的材料汇总单位映射表里，面积/体积都已是
        规范形"（那是 `㎡` 残留的**真正来源**：`resource_plan.material_summary[*].unit`；
        证据：旧产物 `输出结果\\计划_plan_run_1789895021\\计划看板.html` 第 418 行印过
        `"name": "area", "total_quantity": 14200.0, "unit": "㎡"`）。

        本批把材料清单从输入/边界/计划/展示四处一起删除 → 那个残留来源**不复存在**。
        这里改为钉"它真的没了"，同时钉 G5 的产物侧要求（`m²` / `m³` 仍是唯一规范形，
        见 `test_plan_assembler_material_summary.py` 与下面的全链路 0 处 U+33A1 断言）。
        """
        src = (BACKEND / "pipeline" / "nodes" / "plan_assembler.py").read_text(encoding="utf-8")
        assert "mat_map" not in src, "`mat_map` 已随材料汇总一起删除"
        assert 'material_summary": material_summary' not in src, \
            "`resource_plan` 不该再写 material_summary"


@pytest.fixture(scope="module")
def _full_plan():
    """跑一次全链路（含 `㎡` 输入），给下面两条断言共用 —— 省一次几十秒的跑。"""
    from test_contracts import run_pipeline_full
    ctx, _events, finished = run_pipeline_full(
        # 【第 2 批收口】必写基础类型与结构形式（两者都是硬必要键 + 绝对必要）。
        "某住宅项目，共 12 栋，地上 38 层，基础类型：筏板基础，"
        "结构形式：框架-剪力墙结构，总建筑面积12.8万" + U33A1 + "，"
        "混凝土5.2万m³，钢筋7.5万吨，总劳动力峰值929人，开工2025-04-16")
    assert finished, "流水线未结束"
    assert ctx.get("plan_json") is not None
    return ctx["plan_json"]


class TestUnitNormalizationFullPlan:
    def test_全链路产物0处U33A1(self, _full_plan):
        """断言语义：含 `㎡` 的输入跑完整流水线，plan_json 里 0 处 U+33A1。"""
        assert _u33a1_count(_full_plan) == 0, "plan_json 里仍有 U+33A1"

    def test_资源计划不再有材料汇总(self, _full_plan):
        """断言语义（【第 2 批 · 域 2 / 2.6】已改写）：曾经印出 `14200.0 ㎡` 的那个字段
        `resource_plan.material_summary` **整体不再产出** —— 交付物改印
        「本计划不含材料计划。材料按"管够"处理，不参与工期与资源计算。」。
        """
        rp = _full_plan.get("resource_plan") or {}
        assert "material_summary" not in rp, rp.get("material_summary")


# ══════════════════════════════════════════════════════════════════
# 3. `extractor` 的建筑类型/结构形式兜底改调 `kb.match_*`（与 `kb.resolve_*` 同一处实现）
# ══════════════════════════════════════════════════════════════════
STRUCT_TEXTS = ("框架剪力墙结构", "框架结构", "剪力墙结构", "框剪结构")
BUILD_TEXTS = ("住宅", "办公楼", "商业综合体")


def _kb_db_ok():
    return kb.resolve_structure_type("框架结构") is not None


class TestTypeResolverConsistency:
    @pytest.mark.parametrize("text", STRUCT_TEXTS)
    def test_结构形式正则兜底与kb解析一致(self, text):
        """断言语义：`extract_by_regex(text)['structure_type']` **必须等于**
        `kb.resolve_structure_type(text)[0]` —— 两处不许各有一套实现。"""
        if not _kb_db_ok():
            pytest.skip("kb.db 不可用（本断言需要真库）")
        want = kb.resolve_structure_type(text)
        got = extract_by_regex(text).get("structure_type")
        assert got == want[0], (text, got, want)

    @pytest.mark.parametrize("text", BUILD_TEXTS)
    def test_建筑类型正则兜底与kb解析一致(self, text):
        """断言语义：建筑类型同上，两边必须一致。"""
        if not _kb_db_ok():
            pytest.skip("kb.db 不可用（本断言需要真库）")
        want = kb.resolve_building_type(text)
        got = extract_by_regex(text).get("building_type")
        assert got == want[0], (text, got, want)

    def test_框架剪力墙结构判成frame_shear(self):
        """断言语义（父代理实测缺陷）：`'框架剪力墙结构'` 必须是 `frame_shear`，
        既不是 `frame`（子串陷阱）也不是 `shear_wall`（行序陷阱）。"""
        got = extract_by_regex("项目概况：一栋高层住宅楼，框架剪力墙结构").get("structure_type")
        assert got == "frame_shear", got

    def test_框剪结构判成frame_shear(self):
        """断言语义：复合关键词 `'框剪结构'` 同样归 `frame_shear`。"""
        assert extract_by_regex("框剪结构，地上18层").get("structure_type") == "frame_shear"

    def test_剪力墙结构仍判shear_wall(self):
        """断言语义：修复不许把纯剪力墙误判成框剪（反向回归）。"""
        assert extract_by_regex("剪力墙结构").get("structure_type") == "shear_wall"

    def test_兜底不覆盖LLM已给的值(self):
        """断言语义：`p.setdefault` 语义不变 —— 正则兜底只在没有该键时写入。"""
        p = extract_by_regex("框架结构")
        assert p["structure_type"] == "frame"
        p.setdefault("structure_type", "shear_wall")
        assert p["structure_type"] == "frame"


# ══════════════════════════════════════════════════════════════════
# 2. 施工段用户覆盖通道（segment_rule）抽取侧
# ══════════════════════════════════════════════════════════════════
class TestSegmentRuleChannel:
    def test_没写分段规则时键不存在(self):
        """断言语义：用户没写 → `segment_rule` 键**不存在**（不是 None）。"""
        rule, pending, _n = SI.normalize_segment_rule(None, "总建筑面积15000平方米，地上18层")
        assert rule is None and pending == []

    def test_每层分两段被抽成段数并生效(self):
        """断言语义：`每层分 2 段` → 段数规则，且**消费侧真的认**它。

        形状是 `{"segment_count": 2}`（消费侧认的三种形状之一）而**不是裸 int**：
        裸 int 带不了 `source` 标记，`boundary` 用另一段正文再归一化时会认不出
        上游已落地的规则，把它静默丢掉（幂等缺陷）。
        """
        rule, pending, _n = SI.normalize_segment_rule(None, "每层分 2 段施工")
        assert rule is not None and pending == [], (rule, pending)
        assert rule["segment_count"] == 2, rule
        assert rule["source"] == "text"
        assert org_plan._normalize_user_rule(rule, 1000.0) is not None
        # 消费侧把它均匀切成 2 段
        norm = org_plan._normalize_user_rule(rule, 1000.0)
        assert norm["areas"] == [500.0, 500.0], norm

    def test_上游段数规则在另一段正文下不丢(self):
        """断言语义（幂等）：extractor 落地的段数规则，`boundary` 拿另一段（不含分段
        表述的）正文再归一化时**必须原样保住**，不许静默丢掉。"""
        rule0, _p, _n = SI.normalize_segment_rule(None, "每层分 2 段施工")
        assert rule0 is not None
        rule1, pending, _n = SI.normalize_segment_rule(rule0, "总建筑面积15000平方米")
        assert rule1 is not None and rule1["segment_count"] == 2, (rule1, pending)
        assert pending == []

    def test_段面积序列形状被抽成消费侧认的形状(self):
        """断言语义：`分 500 ㎡和 333 ㎡两段` → `{"segment_areas": [...]}` 且消费侧认。"""
        text = "分 500 " + U33A1 + "和 333 " + M2 + "两段"
        rule, pending, _n = SI.normalize_segment_rule(None, text)
        assert rule is not None and pending == [], (rule, pending)
        assert rule["segment_areas"] == [500.0, 333.0], rule
        assert org_plan._normalize_user_rule(rule, 1000.0) is not None

    def test_消费侧支持段数与段面积两种形状(self):
        """断言语义：钉住消费侧事实 —— 裸 int 与 `{"segment_count"}` 都认，
        `{"floor_overrides"}` **不认**（所以抽取侧必须展平，不许原样下发）。"""
        assert SI.segment_rule_supported(2) is True
        assert SI.segment_rule_supported({"segment_count": 2}) is True
        assert SI.segment_rule_supported({"segment_areas": [500.0, 500.0]}) is True
        assert SI.segment_rule_supported({"floor_overrides": {"1": 2}}) is False

    def test_逐层规则能展平就产出显式段面积(self):
        """断言语义：`floor_overrides` + 用户明写逐层面积 → 展平成一套显式段面积序列。"""
        fa = build_floor_areas(None, "1层 1000平方米，2~18层 1000平方米", None, 18)
        assert fa["source"] == "user"
        rule, pending, _n = SI.normalize_segment_rule(
            None, "1 层分 2 段，2 层以上分 2 段", fa)
        assert rule is not None, pending
        assert rule["segment_areas"] == [500.0, 500.0], rule
        assert org_plan._normalize_user_rule(rule, 1000.0) is not None

    def test_展平不出来就转待确认且不产出规则键(self):
        """断言语义（父代理裁定第 2 条）：缺逐层面积 → `needs_confirm` +
        **`segment_rule` 键不存在**（不是 None），消费侧自然退回 MSSA。"""
        rule, pending, _n = SI.normalize_segment_rule(
            None, "1 层 2 段，2 层以上 1 段", None)
        assert rule is None
        assert pending and all(p["needs_confirm"] is True for p in pending), pending
        assert all(p["confirm_reason"] for p in pending), pending
        # 均摊假设（不是用户明写的逐层面积）→ 同样展不出，**不许假设标准层都一样**
        fa_avg = build_floor_areas(None, None, 15000, 18)
        assert fa_avg["source"] == "average_assumption"
        rule2, pending2, _n = SI.normalize_segment_rule(
            None, "1 层 2 段，2 层以上 1 段", fa_avg)
        assert rule2 is None and pending2, (rule2, pending2)

    def test_MSSA上限与否定不静默(self):
        """断言语义：MSSA 覆盖（消费侧无此通道）与"不分段"一律转待确认。"""
        r1, p1, _n = SI.normalize_segment_rule(None, "按 400 平米分段")
        assert r1 is None and p1 and p1[0]["kind"] == "mssa", (r1, p1)
        r2, p2, _n = SI.normalize_segment_rule(None, "本项目不分段")
        assert r2 is None and p2 and p2[0]["kind"] == "negation", (r2, p2)

    def test_模型给的裸形状不被当成用户明写(self):
        """断言语义：模型/上游塞进来的 `{"segment_count": 2}` 不许直接生效。"""
        rule, pending, _n = SI.normalize_segment_rule(
            {"segment_count": 2}, "总建筑面积15000平方米")
        assert rule is None, rule
        assert pending and pending[0]["kind"] == "upstream", pending

    def test_节点把规则写进boundary_conditions(self):
        """断言语义：消费侧读的是 `boundary_conditions["segment_rule"]`
        （`scheduler.segment_rule_of`），所以节点必须同时写 params 与 boundary。

        注：BoundaryNode 的正文只看 `_manual_param_input` / `prompt`
        （`doc_content` 由上游 extractor 负责），所以这里把规则放进 prompt。
        """
        node = BoundaryNode(llm=_StubLLM({"boundary_conditions": {}}))
        ctx = {"doc_content": "每层分 2 段施工", "extracted_params": {},
               "prompt": "每层分 2 段施工"}
        node.run(ctx)
        assert ctx["boundary_conditions"]["segment_rule"] == {
            "segment_count": 2, "source": "text", "text": "每层分 2 段施工",
            "needs_confirm": False, "confirm_reason": ""}, ctx["boundary_conditions"]
        assert ctx["extracted_params"]["segment_rule"]["segment_count"] == 2

    def test_节点对展不出的规则只写待确认不写规则(self):
        """断言语义：展不出 → boundary 里**没有** `segment_rule`，params 里只有 pending。"""
        node = BoundaryNode(llm=_StubLLM({"boundary_conditions": {}}))
        ctx = {"doc_content": "1 层 2 段，2 层以上 1 段", "extracted_params": {},
               "prompt": "1 层 2 段，2 层以上 1 段"}
        node.run(ctx)
        assert "segment_rule" not in ctx["boundary_conditions"], ctx["boundary_conditions"]
        pend = ctx["extracted_params"].get(SI.SEGMENT_RULE_PENDING_KEY)
        assert pend and all(p["needs_confirm"] for p in pend), pend


# ══════════════════════════════════════════════════════════════════
# 4. 模板 / 砌体参数管道
# ══════════════════════════════════════════════════════════════════
class TestFormworkMasonryChannel:
    def test_最终验收输入能抽出模板与砌体(self):
        """断言语义：`示例3_住宅楼_对比版.txt` → 模板 25000 / 砌体 3000。"""
        text = SAMPLE3.read_text(encoding="utf-8")
        p = extract_by_regex(text)
        assert p["total_formwork"] == 25000, p
        assert p["total_masonry"] == 3000, p

    def test_归一化后两个量仍在且是数值(self):
        """断言语义：`normalize_params` 不丢这两个键，且按数值键转成 int。"""
        text = SAMPLE3.read_text(encoding="utf-8")
        merged = normalize_params({}, text)
        assert merged["total_formwork"] == 25000, merged
        assert merged["total_masonry"] == 3000, merged

    def test_三种面积写法都能认(self):
        """断言语义：`㎡` / `m²` / `m2` 都认（输入侧宽容）。"""
        for unit in (U33A1, M2, "m2", "平方米"):
            p = extract_by_regex("模板：约25000" + unit)
            assert p.get("total_formwork") == 25000, (unit, p)

    def test_砌体不误吞砌体墙面积(self):
        """断言语义：`砌体墙面积 3000 平方米` 是**面积**，不许被当成砌体体积 3000 m³。"""
        assert "total_masonry" not in extract_by_regex("砌体墙面积约3000平方米")
        # 真·体积写法仍然要认
        assert extract_by_regex("砌体：约3000立方米")["total_masonry"] == 3000
        assert extract_by_regex("砌筑 1200m³")["total_masonry"] == 1200

    def test_写了就用没写就是None不许静默填0(self):
        """断言语义：缺失 → `None`（不许静默填 0 或系数值）。"""
        merged = normalize_params({}, "总建筑面积15000平方米，地上18层")
        assert merged["total_formwork"] is None, merged
        assert merged["total_masonry"] is None, merged

    def test_用户给了就用用户的量(self):
        """断言语义：用户明写的量优先，不被正则/系数覆盖。"""
        merged = normalize_params({"total_formwork": 26000, "total_masonry": 2800},
                                  "模板：约25000平方米，砌体：约3000立方米")
        assert merged["total_formwork"] == 26000, merged
        assert merged["total_masonry"] == 2800, merged

    def test_两个键已进核心参数与可回退档(self):
        """断言语义：两个键进 CORE_KEYS（能随 boundary 流动）、进 FALLBACK_KEYS
        （缺失时标注"推算/默认"，不是"不影响编制"）、且有中文名。"""
        for k in ("total_formwork", "total_masonry"):
            assert k in B.CORE_KEYS, k
            assert k in B.FALLBACK_KEYS, k
            assert B.param_label(k) != k, k


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
