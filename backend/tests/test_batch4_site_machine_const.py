# -*- coding: utf-8 -*-
"""第 2 批 · 域 7.7 / 7.8 / 7.9 / 7.10：塔吊 / 施工电梯 = **项目级常量**。

口径（用户裁定，见 `docs/域7_资源层_实现设计.md` §3.7–§3.10）：
  · **7.7** 台数在 `boundary` 节点一次性定好，冻结成
    `boundary_conditions["site_machine_const"]`（**不能**寄生在 `equipment` 上 ——
    那个键会被 `strip_model_declared()` pop 掉）；
  · **7.8** 默认够用 / 不判超限 / 日账本每天记这个常量 / 超限清单永不含它们；
  · **7.9** 司机 / 信号工**跟台数走**（逐日配员 = 台数 × 每台人数），不设限额；
  · **7.10** 只用 `total_area` / `building_count` / `floors` 三个**已有建筑参数** +
    明示规则估算，冻结并标注，**不编系数表、不引入新数据源**。

本文件的锁（改坏就红）：
  1. `site_machine_const` **不在** `MODEL_DECLARED_KEYS` 里，且能活过 `strip_model_declared()`；
  2. 用户申报台数**赢**（逐台 `count_source="user"`），没申报的机械走 AI 估算；
  3. 真实样例参数（215000 m² / 12 栋 / 38 层）→ 塔吊 12 台、施工电梯 12 台（**改前恒为 1 台**）；
  4. 缺建筑参数 → 1 台，但**必须带标注**（不许静默编一个数）；
  5. 重跑**逐位一致**（无随机、无时间戳、无字典序依赖 → 冻结）；
  6. `resource` 侧既有键名 / 类型**一个字不变**，只新增标注键；
  7. 无命中时输出逐字段不变（`test_algorithm_parity` 同源纪律）；
  8. `_over_limit_records` 在 **ai_default**（用户没申报台数）时**结构性不含**塔吊；
     用户申报台数时**现状会含**（缺口在 `scheduler.py`，不在本批辖区，见 xfail 标记）。

运行：cd backend && python -m pytest tests\\test_batch4_site_machine_const.py -q ^
      -p no:cacheprovider --basetemp=_test_tmp\\d7site
"""

import copy
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import kb                                             # noqa: E402
from pipeline import org_defaults as OD                             # noqa: E402
from pipeline.nodes import boundary as B                            # noqa: E402
from pipeline.nodes import resource as R                            # noqa: E402
from pipeline.nodes import scheduler as S                           # noqa: E402

KEY = OD.SITE_MACHINE_CONST_KEY

# 真实样例参数（`项目样例\示例3_住宅楼.txt` 的量级：12 栋 / 38 层 / 21.5 万 m²）
PARAMS = {"total_area": 215000.0, "building_count": 12, "floors": 38}


class _StubLLM(object):
    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, system, user, temperature=0.3, retries=1):
        return self.payload


class _BoomLLM(object):
    def chat_json(self, system, user, temperature=0.3, retries=1):
        raise RuntimeError("模型不可用（离线测试）")


def _crew_of(machine):
    """与 `boundary._site_machine_crew_of` 同口径（读真实 KB 两行）。"""
    return OD.resolve_site_machine_crew(machine, kb.crew_for_machine(machine))


def _const(**kw):
    return OD.build_site_machine_const(kw.pop("params", PARAMS), crew_of=_crew_of, **kw)


def _hit_task(tid="t1", name="钢筋绑扎", days=5):
    return {"task_id": tid, "task_name": name, "planned_duration_days": days,
            "钢筋工_per_day": 10, "钢筋工_total_days": 50, "_norm_flagged": True}


# ══════════════════════════════════════════════════════════════════
# 7.10 · 估算：只用三个建筑参数 + 明示规则 + 标注
# ══════════════════════════════════════════════════════════════════
class TestEstimate:
    def test_样例参数给12台而不是1台(self):
        """★ 本批的核心：真实规模 12 栋 / 38 层 → 塔吊 12 台、施工电梯 12 台。"""
        assert OD.estimate_site_machine_count("塔吊", PARAMS)[0] == 12
        assert OD.estimate_site_machine_count("施工电梯", PARAMS)[0] == 12

    def test_规则文本明示阈值与算式(self):
        n, rule, basis = OD.estimate_site_machine_count("塔吊", PARAMS)
        assert n == 12
        assert "每栋 1 台" in rule and "12" in rule, rule
        assert basis["rule_id"] == "tower_crane.per_building.high_rise"
        assert basis["inputs"] == {"building_count": 12.0, "floors": 38.0,
                                  "total_area": 215000.0}, basis

    def test_只用已有的三个建筑参数(self):
        """给一堆与台数无关的键（节拍 / 定额 / 工程量）→ 结果**一个数都不变**。"""
        noisy = dict(PARAMS)
        noisy.update({"cadence_days": 3, "total_concrete": 99999,
                      "labor_peak": 500, "定额": "任意", "total_rebar": 1})
        assert OD.estimate_site_machine_count("塔吊", noisy) == \
            OD.estimate_site_machine_count("塔吊", PARAMS)

    def test_缺栋数退回面积法且与栋数法同解(self):
        n, rule, basis = OD.estimate_site_machine_count("塔吊", {"total_area": 215000.0})
        assert n == 12, rule
        assert basis["rule_id"] == "tower_crane.per_area"
        assert "18000" in rule, rule

    def test_多层按每2栋1台(self):
        n, rule, _b = OD.estimate_site_machine_count("塔吊", {"building_count": 12, "floors": 6})
        assert n == 6, rule
        assert "每 2 栋 1 台" in rule, rule

    def test_缺全部参数给1台但必须带标注(self):
        """不许静默编数：1 台 + `rule_id=no_input` + `inputs_missing`（7.10 的"不猜"）。"""
        n, rule, basis = OD.estimate_site_machine_count("塔吊", {})
        assert n == OD.SITE_MACHINE_MIN_COUNT == 1
        assert basis["rule_id"] == "tower_crane.no_input"
        assert basis["inputs_missing"] is True
        assert "无可用建筑参数" in rule, rule

    def test_确定性重跑逐位一致(self):
        a = json.dumps(_const(), ensure_ascii=False, sort_keys=True)
        b = json.dumps(_const(), ensure_ascii=False, sort_keys=True)
        assert a == b


# ══════════════════════════════════════════════════════════════════
# 7.7 / 7.10 · 常量块的形状、冻结、来源
# ══════════════════════════════════════════════════════════════════
class TestConstBlock:
    def test_形状与键序稳定(self):
        block = _const()
        assert block["schema"] == 1
        assert block["caliber"] == "project_level_constant"
        assert block["frozen"] is True
        assert list(block["machines"]) == list(OD.SITE_MACHINE_MACHINES), \
            "键序必须是固定元组（逐位可复现）"
        for m, entry in block["machines"].items():
            assert isinstance(entry["count"], int), entry
            assert entry["unit"] == "台"
            assert entry["count_source"] == "ai_default"
            assert entry["confidence"] == "LOW"
            assert entry["const_period"] == {"from_day": 0, "to_day": None}
            assert entry["crew_per_unit"], "每台配员不许为空（KB 两行在）"
            assert entry["rule"], "7.10：台数必须带明示规则"
        assert OD.SITE_MACHINE_ESTIMATE_NOTE in json.dumps(block, ensure_ascii=False)

    def test_每台配员来自KB两行(self):
        """7.9 的前提：KB `Equipment_Crew_Mapping` 两行 → 每台 1 司机(+1 信号工)。"""
        m = _const()["machines"]
        assert m["塔吊"]["crew_per_unit"] == {"司机": 1, "信号工": 1}
        assert m["塔吊"]["crew_source"] == "kb:Equipment_Crew_Mapping"
        assert m["施工电梯"]["crew_per_unit"] == {"司机": 1}

    def test_用户申报赢逐台(self):
        block = _const(declared={"塔吊": 3})
        assert block["machines"]["塔吊"]["count"] == 3
        assert block["machines"]["塔吊"]["count_source"] == "user"
        assert block["machines"]["塔吊"]["confidence"] == "HIGH"
        assert "用户值优先" in block["machines"]["塔吊"]["rule"]
        # 没申报的那台仍走 AI 估算，不被"块级 user"带跑
        assert block["machines"]["施工电梯"]["count"] == 12
        assert block["machines"]["施工电梯"]["count_source"] == "ai_default"
        assert block["_source"] == "model", "混合来源 → 块级标 model（逐台真值在 count_source）"

    def test_全用户申报时块级来源是user(self):
        block = _const(declared={"塔吊": 3, "施工电梯": 4})
        assert block["_source"] == "user"
        assert all(m["count_source"] == "user" for m in block["machines"].values())

    def test_用户优先判据走_source_of(self):
        block = _const(declared={"塔吊": 3})
        bc = {"equipment": {"塔吊": 3}, KEY: block}
        assert B._source_of(KEY, bc, "") == "model"       # 施工电梯是 AI 估的
        bc2 = {"equipment": {"塔吊": 3}, KEY: _const(declared={"塔吊": 3, "施工电梯": 4})}
        assert B._source_of(KEY, bc2, "") == "user"
        assert B._source_of(KEY, {}, "") == "model"       # 块不存在 → 不冒充 user


# ══════════════════════════════════════════════════════════════════
# 7.7 · boundary 节点：写进 bc、活过 strip、不算成"N 项"
# ══════════════════════════════════════════════════════════════════
class TestBoundaryNode:
    def test_登记表(self):
        assert KEY in B.SOURCE_KEYS, "要进 SOURCE_KEYS（来源留痕）"
        assert KEY not in B.MODEL_DECLARED_KEYS, "★ 铁律：进 MODEL_DECLARED_KEYS 就会被 pop 掉"
        assert KEY in B._BOUNDARY_META_KEYS, "当元数据：不计入“N 项边界条件”"

    def test_strip_model_declared不许清掉它(self):
        bc = {"equipment": [{"name": "塔吊", "quantity": 1}], KEY: _const()}
        out, ignored = B.strip_model_declared(bc, "")
        assert KEY in out, "★ 回归锁：本键必须活过源头剔除"
        assert "equipment" not in out and ignored, "模型编的 equipment 仍要被剔掉"

    def test_节点写入且来源标注恒有(self):
        node = B.BoundaryNode(llm=_StubLLM({"boundary_conditions": {}}))
        ctx = {"doc_content": "", "extracted_params": dict(PARAMS), "prompt": ""}
        node.run(ctx)
        bc = ctx["boundary_conditions"]
        assert bc[KEY]["machines"]["塔吊"]["count"] == 12, bc.get(KEY)
        assert bc["_source"][KEY] in ("user", "model")
        assert KEY not in B.condition_keys(bc), "元数据不许虚增“N 项”"
        assert set(bc["_source"]) == set(B.SOURCE_KEYS)

    def test_用户申报经节点后赢(self):
        node = B.BoundaryNode(llm=_StubLLM({"boundary_conditions": {"equipment": [
            {"name": "塔吊", "quantity": 3, "unit": "台"}]}}))
        ctx = {"doc_content": "垂直运输：塔吊 3 台。", "extracted_params": dict(PARAMS),
               "prompt": ""}
        node.run(ctx)
        bc = ctx["boundary_conditions"]
        assert bc[KEY]["machines"]["塔吊"]["count"] == 3
        assert bc[KEY]["machines"]["塔吊"]["count_source"] == "user"
        assert bc[KEY]["machines"]["施工电梯"]["count"] == 12

    def test_模型自己编的台数不许冒充常量(self):
        """模型返回"塔吊 1 台"但原文没有任何依据 → equipment 被剔，常量=AI 估的 12 台。"""
        node = B.BoundaryNode(llm=_StubLLM({"boundary_conditions": {"equipment": [
            {"name": "塔吊", "quantity": 1, "unit": "台"}]}}))
        ctx = {"doc_content": "本工程为住宅楼。", "extracted_params": dict(PARAMS),
               "prompt": ""}
        node.run(ctx)
        bc = ctx["boundary_conditions"]
        assert bc[KEY]["machines"]["塔吊"]["count"] == 12, bc[KEY]["machines"]["塔吊"]
        assert bc[KEY]["machines"]["塔吊"]["count_source"] == "ai_default"
        assert bc.get("_ignored_model_values"), "剔除必须留痕"

    def test_兜底路径也写常量(self):
        """LLM 崩了（离线兜底）→ 常量照样有，且来源标 model（不许冒充用户）。"""
        node = B.BoundaryNode(llm=_BoomLLM())
        ctx = {"doc_content": "", "extracted_params": dict(PARAMS), "prompt": ""}
        node.run(ctx)
        bc = ctx["boundary_conditions"]
        assert bc[KEY]["machines"]["施工电梯"]["count"] == 12
        assert bc["_source"][KEY] == "model"


# ══════════════════════════════════════════════════════════════════
# 7.7 / 7.8 / 7.9 · resource 侧：读常量、配员跟台数走、日账本
# ══════════════════════════════════════════════════════════════════
class TestResourceReadsConst:
    def _inject(self, const=None, declared=None, params=PARAMS, tasks=None):
        bc = {KEY: const} if const is not None else {}
        boundaries = R.parse_boundary_conditions(bc)
        tasks = tasks if tasks is not None else [_hit_task()]
        reg, prov = R._inject_site_equipment(tasks, boundaries, params)
        return tasks, reg, prov

    def test_台数来自常量而不是写死的1(self):
        tasks, reg, _p = self._inject(_const())
        item = tasks[0]["_site_equipment"][0]
        assert item["quantity"] == 12, item
        assert item["quantity_source"] == "ai_default"
        assert tasks[0]["塔吊_per_day"] == 12
        assert reg["machines"]["塔吊"]["quantity"] == 12
        # 同时命中两台机械的任务（「砌块」在两个词表里）→ 两台都来自常量
        tasks2, reg2, _p2 = self._inject(_const(), tasks=[_hit_task(name="砌块砌筑")])
        assert reg2["machines"]["塔吊"]["quantity"] == 12
        assert reg2["machines"]["施工电梯"]["quantity"] == 12

    def test_既有键名与类型一个字不变(self):
        tasks, _r, _p = self._inject(_const())
        item = tasks[0]["_site_equipment"][0]
        for k in ("name", "quantity", "unit", "quantity_source", "norm_source",
                  "caliber", "crew", "crew_composition", "crew_source", "crew_ref"):
            assert k in item, k
        assert item["unit"] == "台"
        assert item["caliber"] == "site_level_max"
        assert isinstance(item["quantity"], int)
        assert item["crew"] == {"司机": 1, "信号工": 1}, "`crew` 是**每台**人数"

    def test_新增标注键_规则与冻结(self):
        tasks, _r, _p = self._inject(_const())
        item = tasks[0]["_site_equipment"][0]
        assert item["frozen"] is True
        assert item["const_source"] == "site_machine_const"
        assert item["rule"], "7.10：台数规则必须进产物"
        assert item["basis"]["rule_id"]
        assert "台数规则" in item["note"] and "site_machine_const" in item["note"]

    def test_7点9_配员跟台数走(self):
        """逐日配员 = 台数 × 每台人数（12 台 → 12 司机 + 12 信号工）。"""
        tasks, reg, _p = self._inject(_const())
        assert reg["machines"]["塔吊"]["crew"] == {"司机": 1, "信号工": 1}
        assert reg["machines"]["塔吊"]["crew_daily"] == {"司机": 12, "信号工": 12}
        assert tasks[0]["司机_per_day"] == 12
        assert tasks[0]["信号工_per_day"] == 12
        assert tasks[0]["司机_total_days"] == 60.0        # 12 × 5 天

    def test_7点9_用户申报台数时同样跟台数(self):
        tasks, reg, _p = self._inject(_const(declared={"塔吊": 3}))
        assert reg["machines"]["塔吊"]["quantity"] == 3
        assert reg["machines"]["塔吊"]["crew_daily"] == {"司机": 3, "信号工": 3}
        assert tasks[0]["_site_equipment"][0]["crew"] == {"司机": 1, "信号工": 1}

    def test_7点9_不给司机信号工发限额(self):
        tasks, _r, _p = self._inject(_const())
        item = tasks[0]["_site_equipment"][0]
        assert not [k for k in item if "limit" in str(k).lower()], item
        # 配员是**人**，进人工侧（_resource_source 逐角色留痕），不是设备曲线
        assert tasks[0]["_resource_source"]["司机"]["origin"] == "kb"

    def test_7点8_日账本每天记常量(self):
        _t, reg, _p = self._inject(_const(), tasks=[_hit_task(name="砌块砌筑")])
        ledger = R._site_machine_const_daily(reg)
        assert list(ledger) == ["塔吊", "施工电梯"], "键序 = 固定元组（确定性）"
        for m in OD.SITE_MACHINE_MACHINES:
            assert ledger[m]["per_day"] == 12
            assert ledger[m]["present_all_days"] is True
            assert ledger[m]["const_period"] == {"from_day": 0, "to_day": None}
            assert ledger[m]["frozen"] is True
            assert "不进超限清单" in ledger[m]["note"]

    def test_无常量时按同一套规则补算并如实标注(self):
        """老产物（boundary 没落常量）→ 资源层补算 **12 台**（不是写死的 1），标未冻结。"""
        tasks, reg, _p = self._inject(const=None)
        item = tasks[0]["_site_equipment"][0]
        assert item["quantity"] == 12
        assert item["frozen"] is False
        assert item["const_source"] == "resource_estimate"
        assert "未冻结" in item["note"], item["note"]

    def test_用户限额老路径仍然赢(self):
        boundaries = R.parse_boundary_conditions(
            {"equipment": {"塔吊": 2}, "_source": {"equipment": "user"}})
        tasks = [_hit_task()]
        R._inject_site_equipment(tasks, boundaries, PARAMS)
        item = tasks[0]["_site_equipment"][0]
        assert item["quantity"] == 2 and item["quantity_source"] == "user"
        assert item["const_source"] == "equipment_peak"

    def test_无常量无参数退回1台(self):
        tasks = [_hit_task()]
        R._inject_site_equipment(tasks, {}, None)
        item = tasks[0]["_site_equipment"][0]
        assert item["quantity"] == 1
        assert item["const_source"] == "default_one"
        assert "无可用建筑参数" in item["rule"]

    def test_无命中时输出逐字段不变(self):
        tasks = [{"task_id": "t9", "task_name": "场地平整", "planned_duration_days": 3}]
        before = copy.deepcopy(tasks)
        reg, prov = R._inject_site_equipment(tasks, R.parse_boundary_conditions({KEY: _const()}),
                                             PARAMS)
        assert (reg, prov) == ({}, {})
        assert tasks == before, "没有垂直运输设备可投 → 逐字段不变（parity 纪律）"

    def test_parse_boundary_conditions兼容(self):
        """新增键**只在输入含它时**才出现（既有退化断言 / 老产物形状逐字不变）。"""
        assert R.parse_boundary_conditions(None) == {
            "equipment_peak": {}, "labor_peak": None, "trade_peak": {}}
        assert R.parse_boundary_conditions("{bad json") == {
            "equipment_peak": {}, "labor_peak": None, "trade_peak": {}}
        assert R.parse_boundary_conditions("[]") == {
            "equipment_peak": {}, "labor_peak": None, "trade_peak": {}}
        assert KEY not in R.parse_boundary_conditions({"equipment": {"塔吊": 1}})
        b = R.parse_boundary_conditions({KEY: _const(), "equipment": {"塔吊": 1}})
        assert b[KEY]["machines"]["塔吊"]["count"] == 12
        assert b["equipment_peak"] == {"塔吊": 1}


# ══════════════════════════════════════════════════════════════════
# 7.8 · 超限清单：现状实测 + 缺口交接
# ══════════════════════════════════════════════════════════════════
def _peaks(items):
    return {"labor": 0, "equipment": sum(items.values()), "trades": {}, "items": items}


class TestOverLimit:
    def test_ai_default时结构性不进超限清单(self):
        """用户没申报台数 → `limits["equipment"]` 里根本没这个资源 → 不可能有记录。"""
        limits = S.parse_boundary_limits({"_source": {}})
        assert S._over_limit_records([], _peaks({"塔吊": 12}), limits) == []
        assert S._over_limit_records([], _peaks({"施工电梯": 12}), limits) == []

    def test_现状_用户申报台数时仍会出现记录(self):
        """⚠️ 现状钉住（**两种结果都绿**）：缺口在 `scheduler.py:_over_limit_records`
        的 `peaks["items"]` 循环里没有白名单剔除 —— `scheduler.py` 不在本批辖区。
        修好后本用例仍然绿（`recs` 为空 → 不进 if 分支）。"""
        limits = S.parse_boundary_limits(
            {"equipment": {"塔吊": 12}, "_source": {"equipment": "user"}})
        recs = S._over_limit_records([], _peaks({"塔吊": 12}), limits)
        if recs:
            assert recs[0]["resource"] == "塔吊"
            assert recs[0]["note"].startswith("已达上限"), recs

    def test_用户申报台数时也不该进超限清单(self):
        limits = S.parse_boundary_limits(
            {"equipment": {"塔吊": 12}, "_source": {"equipment": "user"}})
        assert S._over_limit_records([], _peaks({"塔吊": 12}), limits) == []
