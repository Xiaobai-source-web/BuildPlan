# -*- coding: utf-8 -*-
r"""域 5 · 补量节点（`quantity_fill`）专项测试 —— 全部**离线**（不联网、不读 kb.db）。

钉住七件事（每一条都对应设计里一条硬边界）：

  1. **同源**：复制出去的常量 / 遍历与源头逐字一致（`QTY_ZERO_TOL` ↔ `ratio_scope`、
     `RATIO_CTX_KEY` ↔ `ratio_scope`、`_MAGNITUDE_BANDS` ↔ `norm_bind`、
     `iter_leaves` ↔ `norm_bind.iter_leaves`）。
  2. **接线**：主链 27 个节点，`quantity_fill` 夹在 `audit_wbs` 与 `norm_bind` 之间，
     且 `builder.QUANTITY_AGENT_IMPORT == "hard"`（真实现已接管占位类）。
  3. **优先级**：`user > ratio > tree > llm` —— 既有系数路径（`STEP_SPECS` 那 11 道）
     **不许**被 LLM 覆写；用户值两种形状都认且压过占比表。
  4. **不怕漏项**：漏项只重问漏的那几条（最多 2 轮）；两轮都不表态**不报错、不 `_stop`**，
     叶子量**不置 0、不编数**；`LLMError` 记 `model_unavailable`。
  5. **P0 护栏**：LLM 补的量**真的到得了叶子**（否则工期完全不变，是最危险的一类静默失败）；
     同时"同单位不重写"「工程量一个字都不许改」。
  6. **冻结可复现**：同一 ctx 连跑两次 → 第二次 0 次模型调用、叶子 JSON 逐字节相同；
     跑前跑后叶子 id 列表逐位相同（不新造叶子 —— 对域 4 编号冻结的唯一承诺）。
  7. **交付物接线**：`plan_assembler.build_meta` 必须把 `ctx["quantity_coverage"]`
     透传成 `meta.quantity_coverage`（接线代理的活；未落地时下面的用例带理由跳过）。

运行（**必须**带 `--basetemp`，仓库纪律）：
  cd backend; python -m pytest tests/test_quantity_agent.py -q -p no:cacheprovider \
      --basetemp=_test_tmp\qty5
"""

import copy
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest                                                # noqa: E402

from pipeline import quantity_scope as qs                    # noqa: E402
from pipeline.nodes.quantity_agent import QuantityAgentNode   # noqa: E402

# ══════════════════ 桩：最小 wbs / kb_scope / 假模型 ══════════════════


def leaf(lid, aid, qty, unit, source=qs.LEAF_SOURCE_RATIO, seg=100.0, formula="算式"):
    """一个节拍叶子（字段与 `layer_engine._make_leaf` 的产物同形）。"""
    return {"id": lid, "name": "工序" + lid, "quantity": qty, "unit": unit,
            "kb_activity_id": aid, "segment_area": seg,
            "_qty_source": source, "_qty_formula": formula}


def wbs(*leaves):
    return {"phases": [{"phase": "主体结构", "work_packages": [
        {"id": "wp1", "name": "主体", "sub_packages": list(leaves)}]}]}


def scope(items):
    """`items` = [(aid, name, unit, wt_id, wt_name)] → 与 KBScopeNode 产物同形的桩。"""
    cands, names = {}, {}
    for aid, name, unit, wt, wtn in items:
        cands.setdefault(wt, []).append({
            "activity_id": aid, "activity_name": name, "unit": unit,
            "applicability_level": "REQUIRED", "production_mode": "labor_driven",
            "labor_type": "混凝土工", "structure_mapping_absent": False})
        names[wt] = wtn
    return {"l3_list": [{"work_type_id": k, "work_type_name": v} for k, v in names.items()],
            "l4_candidates": cands}


def units_of(items):
    return {aid: unit for aid, _n, unit, _wt, _wtn in items}


class FakeLLM:
    """假模型：`fn(batch_ids, call_no, payload) -> {"items": [...]}` 或抛异常。"""

    def __init__(self, fn):
        self.fn = fn
        self.calls = 0
        self.kwargs = None
        self.payloads = []

    def chat_json(self, system, user, temperature=0.3, retries=1):
        self.calls += 1
        self.kwargs = {"temperature": temperature, "retries": retries}
        payload = json.loads(user)
        self.payloads.append(payload)
        batch = [r["activity_id"] for r in payload["本轮工序（闭集内、待表态）"]]
        return self.fn(batch, self.calls, payload)


def answer(aid, qty, unit="m³", in_project=True, reason="按项目参数估算", **extra):
    it = {"activity_id": aid, "in_project": in_project, "quantity": qty, "unit": unit,
          "basis": "桩基数据", "confidence": "low", "reason": reason}
    it.update(extra)
    return it


def ans_all(aids, qty=100.0, unit="m³"):
    return {"items": [answer(a, qty, unit) for a in aids]}


def run_node(items, *, src_leaves=(), params=None, fake=None, llm=None, coverage=None,
             beat_subtrees=None, **kw):
    """跑一次节点，返回 `(node, ret, ctx, leaves)`。"""
    p = dict(params or {})
    p.setdefault("building_type", "residential")
    p.setdefault("structure_type", "frame_shear")
    ctx = {"wbs": wbs(*src_leaves), "kb_scope": scope(items), "extracted_params": p}
    if coverage is not None:
        ctx["quantity_coverage"] = coverage
    if beat_subtrees is not None:
        ctx["beat_subtrees"] = beat_subtrees
    node = QuantityAgentNode(llm=(fake if fake is not None else llm),
                             unit_map=units_of(items),
                             norm_units_map={i[0]: {i[2]} for i in items}, **kw)
    ret = node.run(ctx)
    return node, ret, ctx, qs.audit_leaves(ctx["wbs"])


# ══════════════════ 1. 同源（复制 + 注释要真的一样） ══════════════════

def test_同源常量与ratio_scope逐字一致():
    from pipeline import ratio_scope as RS
    assert qs.QTY_ZERO_TOL == RS.QTY_ZERO_TOL
    assert qs.RATIO_CTX_KEY == RS.RATIO_CTX_KEY


def test_同源量级带与norm_bind逐字一致():
    from pipeline.nodes import norm_bind as NB
    assert qs._MAGNITUDE_BANDS == NB._MAGNITUDE_BANDS


def test_同源遍历与norm_bind结果相同():
    from pipeline.nodes import norm_bind as NB
    w = wbs(leaf("1.1.1.1", "A", 1.0, "m³"), leaf("1.1.1.2", "B", 2.0, "m³"))
    assert [tuple(x) for x in qs.iter_leaves(w)] == [tuple(x) for x in NB.iter_leaves(w)]


def test_叶子source三态与beat_configs一致():
    from pipeline.nodes.beat_configs import SOURCE_BASE, SOURCE_PARAM, SOURCE_RATIO
    assert (qs.LEAF_SOURCE_RATIO, qs.LEAF_SOURCE_PARAM, qs.LEAF_SOURCE_BASE) == \
        (SOURCE_RATIO, SOURCE_PARAM, SOURCE_BASE)


# ══════════════════ 2. 接线（27 节点 / 顺序 / 硬 import） ══════════════════

def test_主链27节点且补量夹在审计门与定额锚定之间():
    import pipeline.builder as B
    from pipeline.nodes.quantity_agent import QuantityAgentNode
    names = [n for n, _t in B.pipeline_steps()]
    assert len(names) == 27, names
    i = names.index("quantity_fill")
    assert names[i - 1] == "audit_wbs" and names[i + 1] == "norm_bind", names
    # 接线两态（容错 import 期 / 收口后的硬 import）都允许，但**真实现必须已被用上**：
    # 若 builder 用的还是那个"不改任何数据"的占位类，本节点等于没接线。
    assert B.QuantityAgentNode is QuantityAgentNode, \
        "builder 必须用真节点（nodes/quantity_agent.py），不许还是占位类"
    assert B.PIPELINE_TITLES.get("quantity_fill") == "补全各工序工程量"


def test_闭集覆盖全部候选且带工种名():
    items = [("A1", "甲", "m³", "concrete", "混凝土"), ("A2", "乙", "m²", "formwork", "模板")]
    c = qs.closed_l4_set(scope(items))
    assert sorted(c) == ["A1", "A2"]
    assert c["A2"]["work_type_name"] == "模板" and c["A2"]["unit"] == "m²"


# ══════════════════ 3. 优先级：tree 压住 llm；user 压住 ratio ══════════════════

def test_既有系数路径的量绝不被模型覆写():
    items = [("WALL_AI_001", "内墙抹灰", "m²", "plaster", "抹灰")]
    ls = [leaf("1.1.1.1", "WALL_AI_001", 500.0, "m²", qs.LEAF_SOURCE_PARAM, seg=100.0)]
    fake = FakeLLM(lambda b, n, p: ans_all(b, 99999.0, "m²"))
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake)
    cov = ret["quantity_coverage"]
    assert fake.calls == 0, "已有量的 L4 根本不该发给模型（省 token 也是契约）"
    assert leaves[0]["quantity"] == 500.0, "既有系数路径的量一个字都不许改"
    assert cov["l4"]["WALL_AI_001"]["source"] == qs.SOURCE_TREE
    assert cov["l4"]["WALL_AI_001"]["status"] == qs.STATUS_QUANTIFIED
    assert leaves[0]["_qty_provenance"] == qs.SOURCE_TREE
    assert leaves[0]["_qty_frozen"] is True and leaves[0]["_qty_frozen_by"] == "quantity_fill"


def test_占比表排在树与模型之前():
    items = [("CONC_X", "现浇板", "m³", "concrete", "混凝土")]
    ls = [leaf("1.1.1.1", "CONC_X", 800.0, "m³", qs.LEAF_SOURCE_RATIO, seg=100.0)]
    p = {"_component_ratio": {"l4_index": {"CONC_X": {"activity_id": "CONC_X",
                                                       "work_type_id": "concrete",
                                                       "structure_type_id": "frame_shear",
                                                       "ratio_percent": 10, "quantity": 261.6,
                                                       "unit": "m³"}}},
         "l4_quantities": {"CONC_X": 261.6}}
    fake = FakeLLM(lambda b, n, p_: ans_all(b, 9999.0))
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, params=p, fake=fake)
    cov = ret["quantity_coverage"]
    assert cov["l4"]["CONC_X"]["source"] == qs.SOURCE_RATIO
    assert cov["l4"]["CONC_X"]["formula"].startswith("占比表拆分")
    assert leaves[0]["quantity"] == 800.0, "单位没变 → 叶子量一个字节都不重写（P0 护栏）"
    assert fake.calls == 0


def test_用户值覆盖占比表且两种形状都认_分组形状():
    items = [("CONC_X", "现浇板", "m³", "concrete", "混凝土")]
    p = {"l4_quantities": {"concrete": {"CONC_X": 900}}}
    node, ret, ctx, leaves = run_node(items, src_leaves=[
        leaf("1.1.1.1", "CONC_X", 800.0, "m³", qs.LEAF_SOURCE_RATIO)], params=p)
    cov = ret["quantity_coverage"]
    row = cov["l4"]["CONC_X"]
    assert row["source"] == qs.SOURCE_USER
    assert row["user_raw"] == {"value": 900.0, "unit_assumed": "m³"}
    assert row["overridden"] == {"source": qs.SOURCE_RATIO, "value": 800.0}
    assert leaves[0]["quantity"] == 900.0, "用户值必须真的落到叶子上"
    assert leaves[0]["_qty_provenance"] == "user"
    assert leaves[0]["provenance"]["quantity"]["origin"] == "user"
    assert leaves[0]["_qty_override"] == {"source": qs.SOURCE_RATIO, "value": 800.0}


def test_用户值不在闭集_记unknown_user_ids不静默丢():
    items = [("CONC_X", "现浇板", "m³", "concrete", "混凝土")]
    p = {"l4_quantities": {"NOT_IN_CLOSED": 5}}
    node, ret, ctx, leaves = run_node(items, params=p)
    assert ret["quantity_coverage"]["unknown_user_ids"] == ["NOT_IN_CLOSED"]


def test_用户值让总量翻倍_无异常且不被守恒回算改回去():
    items = [("CONC_X", "现浇板", "m³", "concrete", "混凝土")]
    p = {"total_concrete": 8000, "l4_quantities": {"CONC_X": 16000}}
    node, ret, ctx, leaves = run_node(items, src_leaves=[
        leaf("1.1.1.1", "CONC_X", 8000.0, "m³", qs.LEAF_SOURCE_RATIO)], params=p)
    cov = ret["quantity_coverage"]
    assert leaves[0]["quantity"] == 16000.0, "覆盖后的版本就是最终工程量"
    assert cov["summary"]["conservation_note"], "守恒口径必须留一句人话（只展示、不回算）"
    assert all("mismatch" not in w for w in ret["quantity_warnings"])


def test_用户值小于等于容差时不覆盖_记ignored():
    items = [("CONC_X", "现浇板", "m³", "concrete", "混凝土")]
    p = {"l4_quantities": {"CONC_X": 0}}
    node, ret, ctx, leaves = run_node(items, src_leaves=[
        leaf("1.1.1.1", "CONC_X", 800.0, "m³", qs.LEAF_SOURCE_RATIO)], params=p)
    assert ret["quantity_coverage"]["summary"]["ignored_user_ids"] == ["CONC_X"]
    assert leaves[0]["quantity"] == 800.0
    assert ret["quantity_coverage"]["l4"]["CONC_X"]["source"] == qs.SOURCE_RATIO


# ══════════════════ 4. 模型：漏项重试 / 不报错 / 异常降级 ══════════════════

def _five():
    return [("A%d" % i, "工序%d" % i, "m³", "wt%d" % i, "工种%d" % i) for i in range(5)]


def test_漏项只重问漏的那几条():
    items = _five()

    def fn(batch, call_no, payload):
        if call_no == 1:
            return ans_all([a for a in batch if a not in ("A2", "A4")])
        assert payload["上一轮你漏掉的条目"] == ["A2", "A4"]
        return ans_all(batch)

    fake = FakeLLM(fn)
    node, ret, ctx, leaves = run_node(items, fake=fake)
    cov = ret["quantity_coverage"]
    assert fake.calls == 2 and cov["summary"]["model_calls"] == 2
    assert cov["summary"]["retry_batches"] == 1
    assert qs.coverage_gaps(cov) == []
    assert cov["l4"]["A2"]["llm_raw"]["attempt"] == 2


def test_永远漏_不抛异常_量不置零不编数():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE, seg=100.0)]
    fake = FakeLLM(lambda b, n, p: {"items": []})
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake)
    cov = ret["quantity_coverage"]
    assert fake.calls == 2, "两轮之后就不再问了"
    assert cov["l4"]["A1"]["status"] == qs.STATUS_UNSTATED_TREE
    assert leaves[0]["quantity"] == 0.0, "未表态**不许**把量编成 0 以外的数，也不许改原值"
    assert qs.coverage_gaps(cov), "缺口要能被数出来（进 meta + 交付物）"
    assert any("没有拿到工程量" in w for w in ret["quantity_warnings"])


def test_模型明确说本项目没有_记not_applicable且不算缺口():
    items = [("A1", "水下爆破", "m³", "wt", "工种")]
    fake = FakeLLM(lambda b, n, p: {"items": [answer("A1", None, None,
                                                     in_project=False, reason="本项目没有")]})
    node, ret, ctx, leaves = run_node(items, fake=fake)
    cov = ret["quantity_coverage"]
    assert cov["l4"]["A1"]["status"] == qs.STATUS_NOT_APPLICABLE
    assert cov["l4"]["A1"]["quantity"] is None
    assert qs.coverage_gaps(cov) == []
    assert [r["activity_id"] for r in cov["not_in_tree"]] == ["A1"]


def test_模型抛LLMError_记model_unavailable_量沿用():
    from pipeline.llm import LLMError
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE, seg=100.0)]

    def boom(b, n, p):
        raise LLMError("LLM 调用失败（共尝试 3 次）：timeout")

    fake = FakeLLM(boom)
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake,
                                      params={"total_concrete": 8000})
    cov = ret["quantity_coverage"]
    assert fake.calls == 2, "调用失败也走完 attempt 上限"
    assert cov["l4"]["A1"]["status"] == qs.STATUS_MODEL_UNAVAILABLE
    assert cov["l4"]["A1"]["source"] == qs.SOURCE_TREE
    # 树里有叶子（量为 0）→ 量沿用既有值（**不置 0 以外的数、不编数**）
    assert cov["l4"]["A1"]["quantity"] == 0.0
    assert leaves[0]["quantity"] == 0.0


def test_调用参数_temperature0_retries0():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    fake = FakeLLM(lambda b, n, p: ans_all(b))
    run_node(items, fake=fake)
    assert fake.kwargs == {"temperature": 0.0, "retries": 0}, \
        "量是数不是文案（0.0）；语义层重试由本节点管，不许与 chat_json 的 retries 叠乘"


def test_提示词文件缺失_走model_unavailable并留告警_不复刻beat_config的坑(monkeypatch):
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE)]

    def boom(name):
        raise FileNotFoundError(2, "No such file or directory", str(name))

    monkeypatch.setattr("pipeline.prompts_loader.load", boom)
    fake = FakeLLM(lambda b, n, p: ans_all(b))
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake,
                                      params={"total_concrete": 8000})
    cov = ret["quantity_coverage"]
    assert fake.calls == 0, "文件都读不到，不许去调模型"
    assert cov["l4"]["A1"]["status"] == qs.STATUS_MODEL_UNAVAILABLE
    assert any("quantity_fill.txt" in w for w in ret["quantity_warnings"])
    assert [d["code"] for d in cov["degradations"]] == ["prompt_missing"]


def test_llm为None_全程不联网不崩():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE)]
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, llm=None,
                                      params={"total_concrete": 8000})
    cov = ret["quantity_coverage"]
    assert cov["summary"]["model_calls"] == 0
    assert cov["summary"]["llm_available"] is False
    assert cov["l4"]["A1"]["status"] == qs.STATUS_MODEL_UNAVAILABLE
    assert "_stop" not in ret, "参数里给了分项总量 → 就算模型不可用也不许中断（继续如实记账）"


# ══════════════════ 5. P0：LLM 量必须真的到叶子；同单位不重写 ══════════════════

def test_LLM补的量真的写进叶子():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE, seg=100.0)]
    fake = FakeLLM(lambda b, n, p: ans_all(b, 900.0))
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake)
    cov = ret["quantity_coverage"]
    assert leaves[0]["quantity"] == 900.0, "补的量到不了叶子 = 数字全是旧的（最危险的一类）"
    assert leaves[0]["_qty_provenance"] == qs.SOURCE_LLM
    assert leaves[0]["_qty_source"] == qs.LEAF_SOURCE_PARAM, "取值域不变（第五态会撞既有断言）"
    assert leaves[0]["_llm_qty"]["quantity"] == 900.0
    assert leaves[0]["provenance"]["quantity"]["origin"] == "ai"
    assert cov["l4"]["A1"]["source"] == qs.SOURCE_LLM
    assert cov["l4"]["A1"]["quantity"] == 900.0
    assert cov["l4"]["A1"]["leaf_sum"] == 900.0


def test_多叶子按段面积权重落量():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE, seg=300.0),
          leaf("1.1.1.2", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE, seg=100.0)]
    fake = FakeLLM(lambda b, n, p: ans_all(b, 400.0))
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake)
    assert [l["quantity"] for l in leaves] == [300.0, 100.0]
    assert ret["quantity_coverage"]["l4"]["A1"]["leaf_sum"] == 400.0
    assert not [d for d in ret["quantity_coverage"]["degradations"]
                if d["code"] == "llm_qty_uniform_split"]


def test_几何取不到时均分并留痕():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE, seg=0.0),
          leaf("1.1.1.2", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE, seg=0.0)]
    fake = FakeLLM(lambda b, n, p: ans_all(b, 100.0))
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake)
    assert [l["quantity"] for l in leaves] == [50.0, 50.0]
    assert [d["code"] for d in ret["quantity_coverage"]["degradations"]] == \
        ["llm_qty_uniform_split"]


def test_同单位不重写不四舍五入():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 1420.005, "m³", qs.LEAF_SOURCE_BASE)]
    node, ret, ctx, leaves = run_node(items, src_leaves=ls)
    assert leaves[0]["quantity"] == 1420.005, \
        "「工程量一个字都不许改」：同单位时原值返回，不 round、不重写"
    assert ret["quantity_coverage"]["l4"]["A1"]["convert_method"] == ""


def test_跨族换算成功时按目标单位重写并记method():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 100.0, "m²", qs.LEAF_SOURCE_BASE)]
    p = {"thickness_m": 0.2}
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, params=p)
    row = ret["quantity_coverage"]["l4"]["A1"]
    assert row["status"] == qs.STATUS_QUANTIFIED
    assert row["from_unit"] == "m²" and row["unit"] == "m³"
    assert "跨族换算" in row["convert_method"]
    assert leaves[0]["quantity"] == 20.0 and leaves[0]["unit"] == "m³"


def test_换算不出参数_记unit_unresolved_量原样():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 100.0, "m²", qs.LEAF_SOURCE_BASE)]
    node, ret, ctx, leaves = run_node(items, src_leaves=ls)
    row = ret["quantity_coverage"]["l4"]["A1"]
    assert row["status"] == qs.STATUS_UNIT_UNRESOLVED
    assert "缺换算参数" in row["unit_note"], row["unit_note"]
    assert leaves[0]["quantity"] == 100.0 and leaves[0]["unit"] == "m²"


def test_字典单位缺失_不阻断只留痕():
    items = [("A1", "截凿桩头", "见表", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 30.0, "根", qs.LEAF_SOURCE_BASE)]
    node = QuantityAgentNode(llm=None, unit_map={"A1": "见表"}, norm_units_map={"A1": {"m³"}})
    ctx = {"wbs": wbs(*ls), "kb_scope": scope(items), "extracted_params": {}}
    ret = node.run(ctx)
    row = ret["quantity_coverage"]["l4"]["A1"]
    assert row["unit_evidence"] == "unresolved"
    assert "无法确定目标单位" in row["unit_note"]
    assert ls[0]["quantity"] == 30.0


def test_字典单位与定额单位不一致的条数进summary():
    items = [("A1", "截凿桩头", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 30.0, "m³", qs.LEAF_SOURCE_BASE)]
    node = QuantityAgentNode(llm=None, unit_map={"A1": "m³"}, norm_units_map={"A1": {"根"}})
    ctx = {"wbs": wbs(*ls), "kb_scope": scope(items), "extracted_params": {}}
    ret = node.run(ctx)
    assert ret["quantity_coverage"]["l4"]["A1"]["unit_evidence"] == "dict+norm_differs"
    assert ret["quantity_coverage"]["summary"]["dict_norm_differs"] == 1


# ══════════════════ 6. 冻结 / 幂等 / 不新造叶子 ══════════════════

def test_不新造叶子且id列表跑前跑后逐位相同():
    items = [("A1", "工序1", "m³", "wt", "工种"), ("A2", "工序2", "m²", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE),
          leaf("1.1.1.2", "A2", 7.0, "m²", qs.LEAF_SOURCE_PARAM)]
    before = [l["id"] for l in ls]
    fake = FakeLLM(lambda b, n, p: ans_all(b, 12.0))
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake)
    assert [l["id"] for l in leaves] == before, "对域 4 的唯一承诺：编号与集合逐位不变"


def test_幂等_第二次0次调用且叶子逐字节相同():
    items = [("A1", "工序1", "m³", "wt", "工种"), ("A2", "工序2", "m²", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE),
          leaf("1.1.1.2", "A2", 5.0, "m²", qs.LEAF_SOURCE_PARAM)]
    fake = FakeLLM(lambda b, n, p: ans_all(b, 300.0))
    node, ret1, ctx, leaves1 = run_node(items, src_leaves=ls, fake=fake)
    first = json.dumps(qs.audit_leaves(ctx["wbs"]), sort_keys=True, ensure_ascii=False)

    # 第二次：同一份产物搬回 ctx（照 tools/replay_plan.py 的手法）→ 整段跳过 LLM
    fake2 = FakeLLM(lambda b, n, p: (_ for _ in ()).throw(AssertionError("不许再调模型")))
    node2, ret2, ctx2, leaves2 = run_node(items, src_leaves=copy.deepcopy(leaves1),
                                          fake=fake2, coverage=ret1["quantity_coverage"])
    second = json.dumps(qs.audit_leaves(ctx2["wbs"]), sort_keys=True, ensure_ascii=False)
    assert fake2.calls == 0 and ret2["quantity_coverage"]["summary"]["model_calls"] == 0
    assert ret2["quantity_coverage"]["summary"]["reused_previous"] is True
    assert first == second, "量 + 溯源逐字节相同（冻结的可复现语义）"


def test_空闭集优雅降级不中断():
    node = QuantityAgentNode(llm=None, unit_map={}, norm_units_map={})
    ret = node.run({"wbs": wbs(), "kb_scope": {}, "extracted_params": {}})
    assert "_stop" not in ret
    assert ret["quantity_coverage"]["summary"]["closed_total"] == 0
    assert "跳过" in node.done_summary


def test_整份计划没有任何量且模型不可用_才走stop():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE)]
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, llm=None)
    assert "_stop" in ret, "全零 + 模型不可用 = 唯一被允许的中断"
    assert "补充混凝土/钢筋/模板" in ret["_stop"]


def test_有模型时绝不stop():
    items = [("A1", "工序1", "m³", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE)]
    fake = FakeLLM(lambda b, n, p: ans_all(b, 88.0))
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake)
    assert "_stop" not in ret
    assert leaves[0]["quantity"] == 88.0


# ══════════════════ 7. 未入树清单 / 缺口 / 交付物接线 ══════════════════

def test_未入树清单分母是闭集且带原因():
    items = [("A1", "工序1", "m³", "wt", "工种"), ("A2", "工序2", "m²", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 5.0, "m³", qs.LEAF_SOURCE_PARAM)]
    fake = FakeLLM(lambda b, n, p: {"items": [answer("A2", 20.0, "m²")]})
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake,
                                      beat_subtrees={"主体结构": {}})
    cov = ret["quantity_coverage"]
    assert [r["activity_id"] for r in cov["not_in_tree"]] == ["A2"]
    assert cov["not_in_tree"][0]["reason"].startswith("节拍引擎未展开")
    assert cov["not_in_tree"][0]["quantity"] == 20.0
    assert cov["not_in_tree"][0]["source"] == qs.SOURCE_LLM
    assert cov["summary"]["text"] == "闭集 2 个 L4，进树 1 个，未入树 1 个"


def test_缺口判据跳过not_applicable():
    cov = {"l4": {"A": {"activity_id": "A", "status": qs.STATUS_NOT_APPLICABLE,
                        "quantity": None},
                  "B": {"activity_id": "B", "status": qs.STATUS_QUANTIFIED, "quantity": 1.0},
                  "C": {"activity_id": "C", "status": qs.STATUS_UNSTATED_ABSENT,
                        "quantity": None}}}
    assert [r["activity_id"] for r in qs.coverage_gaps(cov)] == ["C"]


def test_用户逐L4量的解析与kb_scope契约一致():
    from pipeline.nodes.kb_scope import _zero_quantity_activities
    p = {"l4_quantities": {"A": 1, "wt": {"B": 2}}}
    got = qs.user_l4_quantities(p)
    assert got == {"A": 1.0, "B": 2.0}
    assert set(got) == set((_zero_quantity_activities(p) or {}).keys())


def test_占比表l4_index解析与ratio_scope一致():
    from pipeline import ratio_scope as RS
    p = {qs.RATIO_CTX_KEY: {qs.RATIO_L4_INDEX_KEY: {"A": {"quantity": 3.0}}}}
    assert qs.ratio_l4_index(p) == RS.l4_index_of(p)


def test_plan_assembler把覆盖表透传进meta():
    """`build_meta` 必须把 `ctx["quantity_coverage"]` 搬进 `meta`。

    这一环断掉时**没有任何报错**：节点照跑、量照冻结，但交付物第 8 节与 meta 账本
    永远缺席（"算了但没送到用户眼前"那一类）。接线代理已落地，故这里是**硬断言**。
    """
    import pipeline.nodes.plan_assembler as PA
    meta = PA.build_meta({"quantity_coverage": {"summary": {"closed_total": 7}}})
    assert meta["quantity_coverage"]["summary"]["closed_total"] == 7


def test_端到端_节点到meta到交付物第8节到叶子量():
    """整条链一次跑通：节点 → `ctx["quantity_coverage"]` → `build_meta` →
    `meta.quantity_coverage` → 交付物章节门 → 第 8 节块 → **叶子量真的变了**。

    这是域 5 的"到得了用户眼前"证明：任何一环断掉（尤其 build_meta 不搬键），
    量要么到不了叶子（工期不变）、要么到不了交付物（用户看不见）。
    """
    PA = pytest.importorskip("pipeline.nodes.plan_assembler")
    D = pytest.importorskip("pipeline.nodes.delivery")
    items = [("A1", "工序1", "m³", "wt", "工种"), ("A2", "工序2", "m²", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE)]
    fake = FakeLLM(lambda b, n, p: {"items": [answer(a, 12.0, "m³") for a in b]})
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake,
                                      params={"total_concrete": 8000})
    ctx.update(ret)

    meta = PA.build_meta(ctx)
    assert meta["quantity_coverage"]["summary"]["closed_total"] == 2
    assert len(meta["quantity_coverage"]["not_in_tree"]) == 1

    plan = {"meta": meta}
    assert D.has_confidence_meta(plan) is True, "覆盖表非空 → 置信度章必须开"
    blocks = D.quantity_coverage_blocks(plan)
    assert [b[1] for b in blocks if b[0] == "h3"] == [D.QUANTITY_COVERAGE_TITLE]
    assert D.QUANTITY_COVERAGE_TITLE.startswith("8.")
    assert "闭集 2 个 L4，进树 1 个，未入树 1 个" in "".join(str(b[1]) for b in blocks)
    assert leaves[0]["quantity"] == 12.0 and leaves[0]["_qty_provenance"] == qs.SOURCE_LLM


def test_覆盖表结构能被交付物读走():
    """与 `delivery.quantity_coverage_blocks` 的数据契约对齐（接线代理已落地那一侧）。"""
    items = [("A1", "工序1", "m³", "wt", "工种"), ("A2", "工序2", "m²", "wt", "工种")]
    ls = [leaf("1.1.1.1", "A1", 0.0, "m³", qs.LEAF_SOURCE_BASE)]
    fake = FakeLLM(lambda b, n, p: {"items": [answer("A2", 20.0, "m²")]})
    node, ret, ctx, leaves = run_node(items, src_leaves=ls, fake=fake)
    cov = ret["quantity_coverage"]
    for k in ("l4", "l4_rows", "not_in_tree", "summary"):
        assert k in cov, k
    for k in ("closed_total", "in_tree", "by_source", "unit_unresolved"):
        assert k in cov["summary"], k
    row = cov["l4_rows"][0]
    for k in ("activity_id", "activity_name", "work_type_id", "work_type_name",
              "in_tree", "quantity", "unit", "unit_evidence", "status", "source"):
        assert k in row, k
    delivery = pytest.importorskip("pipeline.nodes.delivery")
    blocks = delivery.quantity_coverage_blocks({"meta": {"quantity_coverage": cov}})
    assert blocks, "交付物必须能从这份覆盖表算出第 8 节"
    heads = [b[1] for b in blocks if b[0] == "h3"]
    assert delivery.QUANTITY_COVERAGE_TITLE in heads
