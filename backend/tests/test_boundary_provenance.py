# -*- coding: utf-8 -*-
"""第 40 轮回归：边界条件的**来源标注** + 峰值口径（A1 / A2 / B1 / B2）。

四个被修的缺陷（都是用户实测反馈）：

  A1  `项目样例\\示例3_住宅楼.txt` 原文里**一条资源数据都没有**，边界节点按
      "18 层住宅常见做法"补了 `labor.peak_total=120 / equipment / materials`
      （`prompts/boundary_conditions.txt:7-8` 明确要求模型补齐），下游却当"用户限额"用。
      修法：`boundary_conditions["_source"]` 逐项标注 user / model。

  A2  因为 A1 没标来源，`plan_assembler` 里"申报值优先"把**模型编的 120** 顶掉了
      实算曲线峰值 —— 看板印「峰值人数 120 人」，逐日曲线实算只有 38 人。
      修法：只有 `_source == "user"` 才允许顶掉曲线。

  B1  `equip_peak` / `machine_crew_peak` 用「单任务取最大值」→ 每种机械恒为 1 台；
      而交付物逐日铺开算出「履带式单斗液压挖掘机」单日峰值 **2 台**，两个数打架。
      修法：改为按天叠加（与 `delivery._compute_view()` 同口径）。

  B2  `scheduler.equipment_binding_report()` 早就算出"用户申报的塔吊没匹配到计划资源"，
      但只在 ctx 里，`plan.meta` **没有这个键** → 用户看不到"该限额未生效"。
      修法：透传进 `meta["equipment_binding"]`。

运行：python -m pytest backend/tests/test_boundary_provenance.py -q -p no:cacheprovider
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from pipeline.nodes.boundary import (MODEL_DECLARED_KEYS,  # noqa: E402
                                     BoundaryNode, SOURCE_KEYS,
                                     SOURCE_NOTE, annotate_sources,
                                     boundary_haystack, boundary_sources,
                                     condition_keys)
from pipeline.nodes.plan_assembler import build_meta, build_parts  # noqa: E402


# ══════════════════════════════════════════════════════════════════
# 0. 共用夹具
# ══════════════════════════════════════════════════════════════════
# 示例3 原文的**要害片段**（真实文件 项目样例\示例3_住宅楼.txt 28 行）：
#   第 8 行「工期要求：420日历天」、第 10 行「89㎡和120㎡两种户型」、
#   第 12 行「约120根管桩」。
# 注意：全文**没有**"劳动力/峰值"字样，但数字 120 实实在在地出现过两次 ——
# 这正是"纯数字匹配会把模型补的 120 误判成 user"的反例。
SAMPLE_TEXT = """项目名称：某住宅楼工程
建设地点：某市某区
建筑规模：地下1层，地上18层
建筑面积：14200平方米
结构形式：剪力墙结构
工期要求：420日历天
户型组成：89㎡和120㎡两种户型
基础形式：预应力管桩基础，约120根管桩
"""

# 模型对示例3 的实际补全结果（实测）
SAMPLE_BOUNDARY = {
    "labor": {"peak_total": 120, "by_trade": [
        {"trade": "钢筋工", "quantity": 25, "unit": "人"},
        {"trade": "木工", "quantity": 20, "unit": "人"},
        {"trade": "混凝土工", "quantity": 15, "unit": "人"}]},
    "equipment": [{"name": "塔吊", "quantity": 1, "unit": "台"},
                  {"name": "施工电梯", "quantity": 1, "unit": "台"}],
    # 【第 2 批 · 域 2 / 2.6】这里**不再有** `materials`：材料清单已从输入/边界/计划
    # 三处一起删除（"模型仍返回它 → 节点必须 pop 掉"的取证放在
    # `TestStripModelDeclared.MODEL_FILLED`，那里跑的是**节点** `run()`）。
    "project_duration_days": 420,
}


def _leaf(tid, name, dur=2):
    return {"id": tid, "name": name, "quantity": 100.0, "unit": "t",
            "duration_days": dur}


def _row(tid, resources, name="任务"):
    return {"task_id": tid, "task_name": name, "resources": resources,
           "_norm_applied": {"mode": "labor"}}


def _ctx(leaves, rows, boundary=None, sched=None, peak_labor=None):
    ctx = {
        "wbs": {"phases": [{"phase": "主体", "work_packages": [
            {"id": "1.1", "name": "主体", "sub_packages": leaves}]}]},
        "cpm_result": {
            "total_duration_days": 10,
            "critical_path": [leaves[0]["id"]] if leaves else [],
            "schedule": [{"task_id": lf["id"], "es": 0, "ef": lf["duration_days"]}
                         for lf in leaves],
        },
        "resource_demand": {"tasks": rows},
        "extracted_params": {"planned_start_date": "2026-03-01"},
        "boundary_conditions": boundary if boundary is not None else {},
    }
    if sched is not None:
        ver = {"total_duration_days": 10, "schedule": sched}
        if peak_labor is not None:
            ver["peak_labor"] = peak_labor
        ctx["schedule"] = ver
    return ctx


# ══════════════════════════════════════════════════════════════════
# 1. A1 —— 来源标注：user / model
# ══════════════════════════════════════════════════════════════════
class TestSourceJudgement:
    def test_示例3_模型补的数全判model(self):
        """示例3 原文一条资源数据都没有 → 各项来源全部 model（工期那项除外）。

        这是本轮的核心：原文里的 120（户型面积/管桩根数）**不许**让模型补的
        `peak_total=120` 冒充成"用户限额"。

        第 41 轮：契约新增施工节拍两键（原文没写节拍 → 归 model），
        这里的"全判 model"因此从 4 项扩到 6 项。
        【第 2 批 · 域 2 / 2.6】`materials` 已从 `SOURCE_KEYS` 删除 → 不再是这里的契约键。
        【第 2 批 · 域 7.7】**口径变更（预期内）**：`site_machine_const`（塔吊 / 施工电梯的
        项目级常量块）进了 `SOURCE_KEYS` —— 它有自己的来源（用户申报 / AI 估算），按
        "恒有、取值封闭"的既有纪律必须登记。这里是"**没有常量块**"的输入，判据落在
        `_source_of()`：块不存在 → `"model"`（宁可标 model，不冒充用户值）。
        """
        hay = boundary_haystack({"doc_content": SAMPLE_TEXT}, {})
        src = boundary_sources(SAMPLE_BOUNDARY, hay)
        assert src == {
            "labor.peak_total": "model",
            "labor.by_trade": "model",
            "equipment": "model",
            # 工期是"工期要求：420日历天"白纸黑字写的 → 这一项必须认出来
            "project_duration_days": "user",
            # 原文没写节拍（SAMPLE_TEXT 里没有"标准层N天一层"）→ 不许冒认
            "cadence_days": "model",
            "cadence_scope": "model",
            # 【域 7.7】本输入里没有常量块 → model（真实流水线由边界节点现场写入）
            "site_machine_const": "model",
        }, src
        assert "materials" not in src, "材料清单已删除，不许再进 `_source`：%s" % src

    def test_同数字不同上下文不许误判user(self):
        """「120」出现在户型面积/管桩根数里，离"劳动力峰值"很远 → model。"""
        hay = boundary_haystack({"doc_content": "本项目89㎡和120㎡两种户型，约120根管桩"}, {})
        assert boundary_sources(SAMPLE_BOUNDARY, hay)["labor.peak_total"] == "model"

    def test_用户明确写了峰值人数才判user(self):
        hay = boundary_haystack(
            {"doc_content": "劳动力峰值要求不超过200人；钢筋工 25 人，木工 20 人；"
                            "塔吊 1 台；钢筋 639 吨；工期要求 420 日历天"}, {})
        boundary = dict(SAMPLE_BOUNDARY)
        boundary["labor"] = dict(SAMPLE_BOUNDARY["labor"])
        boundary["labor"]["peak_total"] = 200
        boundary["equipment"] = [{"name": "塔吊", "quantity": 1, "unit": "台"}]
        src = boundary_sources(boundary, hay)
        assert src["labor.peak_total"] == "user"
        assert src["equipment"] == "user"
        assert src["project_duration_days"] == "user"
        # 用户只写了钢筋工/木工，模型补的混凝土工没有出处 → 整个列表按 model 处理
        assert src["labor.by_trade"] == "model", \
            "列表里混着模型补的项时必须整体记 model（宁可少认，不许冒充）"

    def test_名称切片能对上挖掘机(self):
        """模型写「PC200挖掘机」、用户只写「挖掘机 18 台」—— 2 字切片要能对上。"""
        hay = boundary_haystack({"doc_content": "拟投入挖掘机 18 台，工期 300 天"}, {})
        boundary = {"equipment": [{"name": "PC200挖掘机", "quantity": 18, "unit": "台"}]}
        assert boundary_sources(boundary, hay)["equipment"] == "user"

    def test_拿不准一律model(self):
        """空值 / 非数值 / 结构看不懂 → 全部 model（宁可标 model 原则）。"""
        hay = boundary_haystack({"doc_content": "劳动力峰值 xxx 人"}, {})
        src = boundary_sources({"labor": {"peak_total": None}, "equipment": "???"}, hay)
        assert set(src.values()) == {"model"}, src
        assert boundary_sources({}, "")["labor.peak_total"] == "model"

    def test_不误伤原文本里没有的数字(self):
        """「工期 420 天」但模型给 365（换算了）→ 数字对不上 → model。"""
        hay = boundary_haystack({"doc_content": "工期要求 420 日历天"}, {})
        src = boundary_sources({"project_duration_days": 365}, hay)
        assert src["project_duration_days"] == "model"

    def test_写入契约键名与原文说明(self):
        bc = annotate_sources(SAMPLE_BOUNDARY, {"doc_content": SAMPLE_TEXT}, {})
        # 第 41 轮：契约新增 cadence_days / cadence_scope 两键（施工节拍），
        # 键集由 `boundary.SOURCE_KEYS` 声明 —— 测试跟着生产实际走的路径走。
        assert set(bc["_source"]) == set(SOURCE_KEYS)
        assert bc["_source_note"] == SOURCE_NOTE
        assert bc["_source_note"] == ("「user」= 用户在自己提供的文件/参数里明确给出；"
                                      "「model」= 模型按常见做法补齐（非用户输入）")
        # 元数据不许被当成"第 N 项边界条件"
        assert "_source" not in condition_keys(bc)
        assert condition_keys(bc) == ["labor", "equipment",
                                      "project_duration_days"]

    def test_手动补充参数进了haystack(self):
        """用户在参数门手动补的「劳动力峰值 260 人」必须能认出来（否则白填）。"""
        ctx = {"_manual_param_input": "劳动力峰值按 260 人考虑"}
        hay = boundary_haystack(ctx, {})
        assert boundary_sources({"labor": {"peak_total": 260}}, hay)[
            "labor.peak_total"] == "user"

    def test_契约_source_永远存在且取值封闭(self):
        """**跨节点契约**（scheduler._user_limits / resource.parse_boundary_conditions 在读）：
        `_source` 永远存在、永远是 dict、永远含 `SOURCE_KEYS` 声明的这 6 个键、
        取值只能是 user/model；`_source_note` 永远存在。

        为什么不能"没找到用户依据就省略这个键"：下游按
        「`_source` 存在且为 model → 不许当限额用；不存在 → 保持旧行为」判 ——
        省略就等于退回"模型补的当用户限额用"，用户第②条白改。
        """
        keys = set(SOURCE_KEYS)
        weird = [{}, None, [], "???", {"labor": None}, {"labor": {"peak_total": -3}},
                 {"labor": {"by_trade": "不是列表"}},
                 {"equipment": [{"name": "", "quantity": None}]},
                 {"materials": [1, 2, 3]}, {"labor_peak": 7}]
        for raw in weird:
            bc = annotate_sources(raw, {}, {})
            assert isinstance(bc["_source"], dict), raw
            assert set(bc["_source"]) == keys, (raw, bc["_source"])
            assert set(bc["_source"].values()) <= {"user", "model"}, raw
            assert bc["_source_note"] == SOURCE_NOTE, raw


class _StubLLM(object):
    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, system, user, temperature=0.3, retries=1):
        return self.payload


class TestBoundaryNodeWiring:
    def test_节点把标注写进ctx(self):
        llm = _StubLLM({"boundary_conditions": SAMPLE_BOUNDARY})
        ctx = {"extracted_params": {"total_area": 14200},
               "prompt": "某住宅楼", "doc_content": SAMPLE_TEXT}
        node = BoundaryNode(llm=llm)
        node.run(ctx)
        bc = ctx["boundary_conditions"]
        assert bc["_source"]["labor.peak_total"] == "model"
        assert bc["_source_note"] == SOURCE_NOTE
        # 组装信息里要能看出"用户给了几项"，否则这层标注在终端上不可见
        assert "来源标注" in node.done_summary, node.done_summary

    def test_正则兜底不再从文本里抠出申报峰值(self):
        """【W3-C / 用户裁定 2026-09-21】LLM 不可用 → 正则兜底**也不许**造申报峰值。

        旧口径（本用例原名 `test_正则兜底路径同样标注`）：原文白纸黑字写了
        「总劳动力峰值 929 人」→ 正则抠出 929 并标 user。
        用户重新裁定：`boundary.py` 里那条
            `bc.setdefault("labor", {})["peak_total"] = int(m.group(1))`
        是"拿正则从自由文本里抠一个数字冒充用户申报的人工峰值"→ **已删除**。
        现在兜底路径不再产生 `labor.peak_total`；用户真要申报峰值，应当在**参数门**
        写一句明确的数量，由模型路径（`_source` 判 user）认出来。
        """
        class _Boom(object):
            def chat_json(self, *a, **k):
                raise RuntimeError("模型不可用")

        ctx = {"extracted_params": {},
               "prompt": "本工程总劳动力峰值 929 人，塔吊 2 台"}
        BoundaryNode(llm=_Boom()).run(ctx)
        bc = ctx["boundary_conditions"]
        assert bc.get("labor", {}).get("peak_total") is None, \
            "正则抠数冒充用户申报的路径必须已经删掉"
        # 没有值 → 来源判 model（`_source` 契约键集不变）
        assert bc["_source"]["labor.peak_total"] == "model", bc["_source"]


# ══════════════════════════════════════════════════════════════════
# 1b. 【W3-C】模型替用户补的申报值 → **源头不再产生**（用户裁定 2026-09-21）
# ══════════════════════════════════════════════════════════════════
class TestModelDeclaredNotProduced:
    """病根 3：`boundary` 让 LLM **替用户补**了 `labor.peak_total=120` / 分工种人数 /
    设备清单 / 450 天目标工期，下游当成"用户申报"用（看板/报告"资源看起来很假"）。

    用户裁定：这四类**模型补的一律不再产生**（源头删除），**只认用户明确给出的值**。

    实现：`boundary.strip_model_declared()` + `MODEL_DECLARED_KEYS`，在
    `apply_cadence` / `annotate_sources` **之前**跑；留痕键 `_ignored_model_values`。
    """

    # 模型在"用户一个字都没提"时按 18 层住宅常见做法编出来的那一套（实测原样）
    MODEL_FILLED = {
        "labor": {"peak_total": 120,
                  "by_trade": [{"trade": "钢筋工", "quantity": 25, "unit": "人"},
                               {"trade": "木工", "quantity": 20, "unit": "人"}]},
        "equipment": [{"name": "塔吊", "quantity": 1, "unit": "台"}],
        # 【第 2 批 · 域 2 / 2.6】`materials` **刻意留在这里**：模型（旧提示词 / 模型习惯）
        # 仍可能返回材料清单，本节点必须**在 `run()` 里把它 pop 掉** —— 这条输入就是
        # `test_材料清单不进计划` 的取证材料。
        "materials": [{"name": "钢筋", "total_quantity": 639, "unit": "吨"}],
        "project_duration_days": 450,
    }
    # 用户原文里**一条资源数据都没有**（只有面积/层数）—— 示例3 的要害
    USER_SILENT_PROMPT = "某住宅楼工程 地下1层 地上18层 建筑面积14200平方米"
    USER_SILENT_DOC = "建筑面积：14200平方米"

    def _run(self, boundary, prompt=None, doc=None):
        node = BoundaryNode(llm=_StubLLM({"boundary_conditions": boundary}))
        ctx = {"extracted_params": {},
               "prompt": prompt if prompt is not None else self.USER_SILENT_PROMPT,
               "doc_content": doc if doc is not None else self.USER_SILENT_DOC}
        node.run(ctx)
        return ctx["boundary_conditions"], node

    def test_裁定范围就是这四个键(self):
        """`MODEL_DECLARED_KEYS` 就是用户 2026-09-21 裁定的四类。

        有人想扩大/缩小删除范围 → 必须同步改这张表与下面 `MODEL_FILLED` 的断言。
        【第 2 批 · 域 2 / 2.6】`materials` 不在**这张表**里 —— 它不是"按来源剔除"，
        而是**整个键被删除**（见 `test_材料清单不进计划`）。
        """
        assert set(MODEL_DECLARED_KEYS) == {
            "labor.peak_total", "labor.by_trade", "equipment",
            "project_duration_days"}, MODEL_DECLARED_KEYS
        assert "materials" not in MODEL_DECLARED_KEYS, MODEL_DECLARED_KEYS

    def test_用户没提就一条都不产生(self):
        """模型补的峰值/分工种/设备/目标工期 → 边界条件里**没有值**。"""
        bc, _ = self._run(self.MODEL_FILLED)
        assert bc.get("labor", {}).get("peak_total") is None, bc
        assert not bc.get("labor", {}).get("by_trade"), bc
        assert not bc.get("equipment"), bc
        assert bc.get("project_duration_days") is None, bc

    def test_剔除要留痕不许静默(self):
        """剔掉了什么必须看得见（含键与值）—— 静默删除等于"数据凭空消失"。"""
        bc, _ = self._run(self.MODEL_FILLED)
        note = bc["_ignored_model_values"]
        joined = "；".join(note)
        for k in ("labor.peak_total", "labor.by_trade", "equipment",
                  "project_duration_days"):
            assert k in joined, (k, note)
        assert "120" in joined and "450" in joined, note
        assert "来源=model" in joined, note

    def test_材料清单不进计划(self):
        """【第 2 批 · 域 2 / 2.6】`materials`（材料清单）**整个键被删除**。

        判据：模型即使仍返回 `materials`（旧提示词 / 模型习惯），本节点也必须在 `run()`
        里把它 pop 掉 —— 计划 JSON、`_source`、交付物三处都不许再出现它。
        与"按来源剔除"（`MODEL_DECLARED_KEYS`）是两回事：那一档是删**值**、留键；
        这一档是连**键**一起删。
        """
        bc, _ = self._run(self.MODEL_FILLED)
        assert "materials" not in bc, bc
        assert "materials" not in (bc.get("_source") or {}), bc.get("_source")

    def test_剔除后来源标注同步(self):
        """`_source` 反映**最终**内容：值没了 → 判 model（契约键集不变）。"""
        bc, _ = self._run(self.MODEL_FILLED)
        for k in ("labor.peak_total", "labor.by_trade", "equipment",
                  "project_duration_days"):
            assert bc["_source"][k] == "model", (k, bc["_source"])
        assert set(bc["_source"]) == set(SOURCE_KEYS), bc["_source"]
        # 元数据键不许被当成"第 N 项边界条件"
        assert "_ignored_model_values" not in condition_keys(bc), condition_keys(bc)

    def test_用户明确给了就保留并标user(self):
        """只认用户明确给出的值：用户写明的 200 人 / 钢筋工 25 人 / 塔吊 1 台 / 420 天
        → 一项都不许丢，来源全部 user。"""
        user_text = ("劳动力峰值 200 人；钢筋工 25 人；塔吊 1 台；"
                     "工期要求：420 日历天")
        boundary = {
            "labor": {"peak_total": 200,
                      "by_trade": [{"trade": "钢筋工", "quantity": 25, "unit": "人"}]},
            "equipment": [{"name": "塔吊", "quantity": 1, "unit": "台"}],
            "materials": [{"name": "钢筋", "total_quantity": 639, "unit": "吨"}],
            "project_duration_days": 420,
        }
        bc, _ = self._run(boundary, prompt=user_text, doc=user_text)
        assert bc["labor"]["peak_total"] == 200, bc
        assert bc["labor"]["by_trade"] == boundary["labor"]["by_trade"], bc
        assert bc["equipment"] == boundary["equipment"], bc
        assert bc["project_duration_days"] == 420, bc
        # 【第 2 批 · 域 2 / 2.6】模型返回的材料清单被源头剔除（连键一起）
        assert "materials" not in bc, bc
        for k in ("labor.peak_total", "labor.by_trade", "equipment",
                  "project_duration_days"):
            assert bc["_source"][k] == "user", (k, bc["_source"])
        assert "_ignored_model_values" not in bc, bc["_ignored_model_values"]

    def test_模型把用户写的数改了就丢掉(self):
        """用户写"工期要求：420 日历天"，模型却返回 450 → 450 没有任何依据 → 丢掉。"""
        user_text = "建筑面积：14200平方米；工期要求：420 日历天"
        boundary = {"project_duration_days": 450}
        bc, _ = self._run(boundary, prompt=user_text, doc=user_text)
        assert bc.get("project_duration_days") is None, bc
        assert "450" in "；".join(bc["_ignored_model_values"]), bc


# ══════════════════════════════════════════════════════════════════
# 2. A2 —— 模型补的峰值不许顶掉实算曲线
# ══════════════════════════════════════════════════════════════════
def _peak_ctx(bc, peak_labor=38):
    leaf = _leaf("1.1.1", "钢筋绑扎")
    row = _row("1.1.1", {"钢筋工": {"per_day": 38, "total_days": 76.0}})
    sched = [{"task_id": "1.1.1", "es": 0, "ef": 2}]
    return _ctx([leaf], [row], boundary=bc, sched=sched, peak_labor=peak_labor)


class TestPeakManpowerSemantics:
    def test_模型补的120不许顶掉曲线(self):
        """核心缺陷回归：申报 120（model）+ 曲线 38 → 必须报 38。

        【W3-C / 用户二次裁定 2026-09-21】旧口径在这里断言的
        `declared_peak_manpower == 120, "申报值不许丢，要留档"` **已作废**：
        用户裁定"模型替用户补的申报峰值连源头一起删"，交付物不再有「申报峰值」
        展示项，`declared_peak_manpower` / `declared_peak_manpower_source` 两键
        已从 `plan_assembler.build_parts` 整体删除。
        本用例保留**旧计划遗留数据**（hand-built 的 model 来源 120）的回归价值：
        下游读到它时仍然只认曲线，不许顶掉。
        """
        bc = annotate_sources(SAMPLE_BOUNDARY, {"doc_content": SAMPLE_TEXT}, {})
        rp = build_parts(_peak_ctx(bc))["resource_plan"]
        assert rp["peak_manpower"] == 38, rp
        assert rp["peak_manpower_source"] == "resource_curve"
        # 新语义：没有"模型来源的申报值"这回事了（键不存在 ＝ 不再产生、不再展示）
        assert "declared_peak_manpower" not in rp, rp
        assert "declared_peak_manpower_source" not in rp, rp
        assert rp["curve_peak_manpower"] == 38

    def test_用户申报才当限额(self):
        bc = {"labor": {"peak_total": 120}, "_source": {"labor.peak_total": "user"}}
        rp = build_parts(_peak_ctx(bc))["resource_plan"]
        assert rp["peak_manpower"] == 120
        assert rp["peak_manpower_source"] == "user"
        # 用户明确给的值得保留 —— 但它走的是 peak_manpower 本身，不是旧的"申报留档"键
        assert "declared_peak_manpower" not in rp, rp

    def test_缺source时也用曲线(self):
        """老计划 / 上游没标注（没有 `_source`）→ 不许再默认"申报值优先"。"""
        rp = build_parts(_peak_ctx({"labor": {"peak_total": 120}}))["resource_plan"]
        assert rp["peak_manpower"] == 38
        assert rp["peak_manpower_source"] == "resource_curve"

    def test_没有曲线可依据时用逐任务口径(self):
        """重导旧计划（ctx 里没有排程版本）→ 退回逐任务口径（曲线实算的保守下界）。

        【W3-C】旧口径是"保留 120 但标 model_estimate"—— 那一档（④没有曲线时显示
        申报值）已被用户裁定删除：**AI 补的数不许印给用户看**。现在没有曲线就退回
        逐任务口径，来源仍是 `resource_curve`，绝不回落到申报值。
        """
        leaf = _leaf("1.1.1", "钢筋绑扎")
        row = _row("1.1.1", {"钢筋工": {"per_day": 38, "total_days": 76.0}})
        ctx = _ctx([leaf], [row], boundary={"labor": {"peak_total": 120}})
        rp = build_parts(ctx)["resource_plan"]
        assert rp["peak_manpower_source"] == "resource_curve", rp
        assert rp["peak_manpower"] != 120, "模型/无来源的申报值不许当峰值用"
        assert "declared_peak_manpower" not in rp, rp

    def test_另两个键始终存在(self):
        """契约：A2 新增的键（另一子代理要读）必须永远在。

        【W3-C】`declared_peak_manpower` 已按用户 2026-09-21 裁定整体删除，
        不在契约里了；剩下的三档口径键必须始终存在。
        """
        bc = annotate_sources(SAMPLE_BOUNDARY, {"doc_content": SAMPLE_TEXT}, {})
        rp = build_parts(_peak_ctx(bc))["resource_plan"]
        for k in ("curve_peak_manpower", "peak_manpower_source", "peak_manpower"):
            assert k in rp, k
        assert "declared_peak_manpower" not in rp, rp

    def test_模型补的申报值不再往下传(self):
        """【W3-C / 用户裁定 2026-09-21】模型补的申报值**源头删除**，不再往下传。

        旧口径（本用例原名 `test_申报值自己的来源也要传下去`）：交付物要能写
        "120 是模型估的，不是用户输入的" → 现在处置更强硬：**不再产生、不再展示**，
        所以两个 `declared_*` 键在 `resource_plan` 里一律不存在。
        用户真写了峰值 → 走 `peak_manpower` 本身，`peak_manpower_source == "user"`。
        """
        bc = annotate_sources(SAMPLE_BOUNDARY, {"doc_content": SAMPLE_TEXT}, {})
        rp = build_parts(_peak_ctx(bc))["resource_plan"]
        assert "declared_peak_manpower" not in rp, rp
        assert "declared_peak_manpower_source" not in rp, rp
        # 看板峰值来自曲线，与"申报值"是两件事，不许混
        assert rp["peak_manpower_source"] == "resource_curve"

        user_bc = {"labor": {"peak_total": 120},
                   "_source": {"labor.peak_total": "user"}}
        rp_user = build_parts(_peak_ctx(user_bc))["resource_plan"]
        assert rp_user["peak_manpower"] == 120
        assert rp_user["peak_manpower_source"] == "user"
        assert "declared_peak_manpower" not in rp_user, rp_user


# ══════════════════════════════════════════════════════════════════
# 3. B1 —— 机械/配员峰值按天叠加
# ══════════════════════════════════════════════════════════════════
class TestDailyStacking:
    def _two_task_ctx(self, sched, per_day=1):
        leaves = [_leaf("T1", "土方开挖A"), _leaf("T2", "土方开挖B")]
        rows = [_row("T1", {"履带式单斗液压挖掘机": {"per_day": per_day,
                                                  "total_days": 2.0}},
                     name="土方开挖A"),
                _row("T2", {"履带式单斗液压挖掘机": {"per_day": per_day,
                                                  "total_days": 2.0}},
                     name="土方开挖B")]
        return _ctx(leaves, rows, sched=sched, peak_labor=0)

    def test_并行两任务要叠加成2台(self):
        """旧口径恒为 1 台；交付物逐日铺开算的是 2 台 —— 必须一致。"""
        sched = [{"task_id": "T1", "es": 0, "ef": 2},
                 {"task_id": "T2", "es": 1, "ef": 3}]     # 第 1~2 天两台同时在
        rp = build_parts(self._two_task_ctx(sched))["resource_plan"]
        assert rp["equipment_peak"]["履带式单斗液压挖掘机"] == 2, rp["equipment_peak"]
        assert rp["equipment_peak"]["履带式单斗液压挖掘机"] >= \
            max(1, 1), "按天叠加的结果不许低于单任务最大值"

    def test_首尾相接不叠加(self):
        """闭区间占用：T1 占 0~1、T2 占 2~3 → 没有同一天两台 → 峰值 1。"""
        sched = [{"task_id": "T1", "es": 0, "ef": 1},
                 {"task_id": "T2", "es": 2, "ef": 3}]
        rp = build_parts(self._two_task_ctx(sched))["resource_plan"]
        assert rp["equipment_peak"]["履带式单斗液压挖掘机"] == 1

    def test_重叠一天就算重叠(self):
        """半开区间 `es..ef-1` 下**首尾相接**的两条任务不重叠 → 峰值 1。

        冻结断言原来写 2：那是旧口径把 `ef` 当闭区间端点的产物（T1 多占第 1 天）。
        真正"重叠一天"的是 T1 `0..1`(天0,1) / T2 `1..2`(天1,2) 这种 `ef` 大于下一条
        `es` 的组合 —— 见 `test_并行两任务要叠加成2台`（es/ef = 0..2 / 1..3 → 2 台）。
        """
        sched = [{"task_id": "T1", "es": 0, "ef": 1},
                 {"task_id": "T2", "es": 1, "ef": 2}]
        rp = build_parts(self._two_task_ctx(sched))["resource_plan"]
        assert rp["equipment_peak"]["履带式单斗液压挖掘机"] == 1, \
            "T1 占第 0 天、T2 占第 1 天：同一天只有一台，不许按闭区间 ef 多算一天"

    def test_台数不同按天求和(self):
        """3 台 + 2 台并行 → 5 台（不是 max(3,2)=3）。"""
        leaves = [_leaf("T1", "A"), _leaf("T2", "B")]
        rows = [_row("T1", {"塔吊": {"per_day": 3, "total_days": 6.0}}),
                _row("T2", {"塔吊": {"per_day": 2, "total_days": 4.0}})]
        sched = [{"task_id": "T1", "es": 0, "ef": 5},
                 {"task_id": "T2", "es": 3, "ef": 5}]
        rp = build_parts(_ctx(leaves, rows, sched=sched, peak_labor=0))["resource_plan"]
        assert rp["equipment_peak"]["塔吊"] == 5, rp["equipment_peak"]

    def test_机械配员同样按天叠加(self):
        """泵工是**人**，也要按天叠加（老口径同样恒为 1）。"""
        leaves = [_leaf("T1", "浇筑A"), _leaf("T2", "浇筑B")]
        rows = [_row("T1", {"混凝土输送泵车": {"per_day": 1, "total_days": 2.0},
                            "泵工": {"per_day": 1, "total_days": 2.0}}),
                _row("T2", {"混凝土输送泵车": {"per_day": 1, "total_days": 2.0},
                            "泵工": {"per_day": 1, "total_days": 2.0}})]
        sched = [{"task_id": "T1", "es": 0, "ef": 2},
                 {"task_id": "T2", "es": 1, "ef": 3}]
        rp = build_parts(_ctx(leaves, rows, sched=sched, peak_labor=0))["resource_plan"]
        assert rp["machine_crew_peak"]["泵工"] == 2, rp["machine_crew_peak"]
        assert "泵工" not in rp["equipment_peak"], "配员是人不许进设备表"

    def test_没有排程行的任务不静默丢(self):
        """任务没落进排程行 → 仍计入（各自单日），并留 `peak_caliber_note`。"""
        leaves = [_leaf("T1", "A"), _leaf("T9", "未排上")]
        rows = [_row("T1", {"塔吊": {"per_day": 1, "total_days": 2.0}}),
                _row("T9", {"塔吊": {"per_day": 2, "total_days": 2.0}})]
        sched = [{"task_id": "T1", "es": 0, "ef": 2}]      # 故意缺 T9
        rp = build_parts(_ctx(leaves, rows, sched=sched, peak_labor=0))["resource_plan"]
        assert rp["equipment_peak"]["塔吊"] == 2, rp["equipment_peak"]
        assert "peak_caliber_note" in rp, "丢了一条排程行却不说，就是静默失败"

    def test_正常计划不留note(self):
        leaves = [_leaf("T1", "A")]
        rows = [_row("T1", {"塔吊": {"per_day": 1, "total_days": 2.0}})]
        rp = build_parts(_ctx(leaves, rows, sched=[{"task_id": "T1", "es": 0, "ef": 2}],
                              peak_labor=0))["resource_plan"]
        assert "peak_caliber_note" not in rp


# ══════════════════════════════════════════════════════════════════
# 4. B2 —— 设备对账透传进 meta
# ══════════════════════════════════════════════════════════════════
class TestEquipmentBindingMeta:
    RAW = {
        "塔吊": {"declared": 1, "bound_to": None, "effective": False,
                 "note": "用户申报的「塔吊」未匹配到计划中的任何机械资源，该限额未生效"},
        "静压桩机": {"declared": 1, "bound_to": "静力压桩机", "effective": True,
                     "note": "已绑定到计划资源「静力压桩机」，限额 1 生效"},
    }

    def test_透传成列表且不丢未匹配项(self):
        meta = build_meta({"equipment_binding": self.RAW})
        items = meta["equipment_binding"]
        assert isinstance(items, list) and len(items) == 2
        by_name = {it["name"]: it for it in items}
        assert by_name["塔吊"]["quantity"] == 1
        assert by_name["塔吊"]["bound_to"] is None
        assert by_name["塔吊"]["effective"] is False
        assert "未生效" in by_name["塔吊"]["note"], by_name["塔吊"]
        assert by_name["静压桩机"]["bound_to"] == "静力压桩机"
        assert by_name["静压桩机"]["effective"] is True

    def test_没有对账结果就是空列表(self):
        """不许编：ctx 里没有 → 空列表（而不是 None / 伪造一条"已绑定"）。"""
        assert build_meta({})["equipment_binding"] == []
        assert build_meta({"equipment_binding": None})["equipment_binding"] == []
        assert build_meta({"equipment_binding": "???"})["equipment_binding"] == []

    def test_键名是契约(self):
        for it in build_meta({"equipment_binding": self.RAW})["equipment_binding"]:
            for k in ("name", "quantity", "bound_to", "note"):
                assert k in it, k
