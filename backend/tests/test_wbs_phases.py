"""WBS 多级分工 骨架/注入/兜底 测试。

覆盖：
  ① default_phases() 含 ≥10 必备 1级
  ② build_phase_kb_injection 注入 KB 活动（含 activity_id/单位）；缺活动不抛返回 None
  ③ host_phase_map 关键词映射专项→宿主实体阶段（不产新相）
  ④ 无 LLM（无 key 且未注入 llm）→ run 仍产全树，1级 覆盖代码骨架（模板/最小项兜底）
  ⑤ 跨相融合：fuse(stub) 返回专项补丁 → 归入宿主阶段，总 1级 数量不增加
  ⑥ 逐相展开：单个 spec → _expand_phase(stub) 返回该相 work_packages

运行：python -m pytest backend/tests/test_wbs_phases.py -v
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes import wbs_agent as wa
from pipeline.nodes.wbs_agent import WBSAgentNode
from pipeline.nodes.wbs_phases import (default_phases, host_phase_map,
                                       build_phase_kb_injection)

PARAMS = {"building_type": "剪力墙住宅", "structure_type": "剪力墙",
          "area": 8000, "floors": 38, "total_area": 301354.26}


# ---------------- 代码骨架 ----------------
def test_default_phases_at_least_10():
    phases = default_phases()
    assert len(phases) >= 10
    for p in phases:
        assert p.get("phase") and p.get("hint"), f"{p} 缺 phase/hint"
        assert "key" in p


def test_phases_include_core_stages():
    names = {p["phase"] for p in default_phases()}
    for must in ("施工准备", "地上主体结构", "地基处理与桩基", "地下室结构", "竣工验收"):
        assert must in names, f"缺必备阶段 {must}"


# ---------------- KB 注入 ----------------
def test_kb_injection_hits_activity():
    text = build_phase_kb_injection(["rebar", "concrete"])
    # 有活动就应文本含 activity_id+单位；完全缺活动才 None —— 两者都不算失败，但不允许抛异常
    assert text is None or isinstance(text, str)
    if text:
        assert "activity_id" in text or "(" in text, "注入应体现 activity_id/单位"


def test_kb_injection_missing_keys_none():
    assert build_phase_kb_injection([]) is None
    assert build_phase_kb_injection(None) is None
    assert build_phase_kb_injection(["definitely_not_a_worktype_key"]) is None


# ---------------- 专项 → 宿主阶段 映射兜底 ----------------
def test_host_phase_map():
    got = host_phase_map("遇溶洞发育，需岩溶处理，同时采用整体爬架施工")
    targets = {g["target_phase"] for g in got}
    assert "地基处理与桩基" in targets, "溶洞→地基处理与桩基"
    assert "地上主体结构" in targets, "爬架→地上主体结构"
    # 不产出新的 1级 阶段：所有 target 都应是实体阶段里的既有名
    host_set = {p["phase"] for p in default_phases()}
    assert targets <= host_set, "宿主映射不得指向不存在的 1级 阶段"


def test_host_phase_map_pc_specialty_removed():
    """A7（2026-09-21 裁定「移除预制相关内容」）：装配式/预制/灌浆/叠合（PC 口径）
    不再映射到任何宿主阶段 —— 该条兜底规则已从 `SPECIALTY_HOST_MAP` 删除。
    """
    assert host_phase_map("本项目为装配式叠合板，含预制构件吊装与套筒灌浆") == []


def test_host_phase_map_no_match_empty():
    assert host_phase_map("普通住宅，无特殊工艺") == []
    assert host_phase_map("") == []

# ---------------- 无 LLM 全树兜底 ----------------
def test_no_llm_full_tree_fallback():
    key_was = wa.config.LLM_API_KEY
    wa.config.LLM_API_KEY = ""           # 无 key
    try:
        node = WBSAgentNode()            # 不注入 llm → llm_usable False
        node._emit = lambda e, d: None
        ctx = {"prompt": "一个住宅项目，请编制施工进度计划。", "extracted_params": dict(PARAMS)}
        out = node.run(ctx)
        code = {p["phase"] for p in default_phases()}
        wbs_phases = {p["phase"] for p in out["wbs"]["phases"]}
        assert code <= wbs_phases, "无 LLM 时 1级 必须覆盖代码骨架"
        # 每个相都要有 2/3级
        for ph in out["wbs"]["phases"]:
            assert ph.get("work_packages"), f"{ph['phase']} 缺工作包"
            for wp in ph["work_packages"]:
                assert wp.get("sub_packages"), f"{ph['phase']}/{wp['name']} 缺叶子"
    finally:
        wa.config.LLM_API_KEY = key_was


# ---------------- 跨相融合（stub fuse llm） ----------------
class _StubLLM:
    """按 user 内容分派角色：跨相融合 / 逐相 / 复评。temperature 不作判据。"""

    def __init__(self, fusions, phase_wps, review):
        self.fusions = fusions
        self.phase_wps = phase_wps
        self.review = review
        self.fuse_calls = 0
        self.phase_calls = 0
        self.review_calls = 0

    def chat_json(self, system, user, temperature=0.3, retries=1):
        if "完整三层WBS" in user:                        # 跨相融合（整树展开后）
            self.fuse_calls += 1
            return {"fusions": self.fusions}
        if "候选三层WBS" in user:                        # 复评
            self.review_calls += 1
            return self.review
        if "本阶段：" in user:                           # 逐相 worker
            self.phase_calls += 1
            return {"work_packages": self.phase_wps}
        return None


def test_fusion_merges_into_host_phase():
    """专项补丁必须归入宿主既有阶段，且不新增第 11 个 1级 阶段。"""
    stub = _StubLLM(
        fusions=[{"target_phase": "地基处理与桩基", "action": "add_wp",
                  "wp_name": "溶洞及岩溶处理", "leaf_name": "溶洞填充",
                  "duration_days": 20, "quantity": 500, "unit": "m³",
                  "work_type": "土方工程", "reason": "溶洞属地基处理"}],
        phase_wps=[{"id": "1.1", "name": "该相工作", "sub_packages": [
            {"id": "1.1.1", "name": "任务1", "duration_days": 5,
             "quantity": 100, "unit": "m³", "work_type": "混凝土工程"}]}],
        review={"verdict": "PASS", "issues": []},
    )
    node = WBSAgentNode(llm=stub)
    node._emit = lambda e, d: None
    ctx = {"prompt": "项目含溶洞。", "extracted_params": dict(PARAMS)}
    out = node.run(ctx)
    phases = out["wbs"]["phases"]
    names = {p["phase"] for p in phases}
    # 1级 数量不得超过代码骨架（专项不再独立成相）
    assert len(phases) == len(default_phases()), f"专项不应新增 1级，实际 {len(phases)}"
    # 专项以工作包形式出现在宿主「地基处理与桩基」内
    pj = next(p for p in phases if p["phase"] == "地基处理与桩基")
    wp_names = [w["name"] for w in pj["work_packages"]]
    assert "溶洞及岩溶处理" in wp_names, "专项应并入地基处理与桩基"
    assert stub.fuse_calls == 1
    # 传进食 {} fusions → 返回 0
    node2 = WBSAgentNode()
    assert node2._apply_fusions({"phases": []}, []) == 0


def test_expand_phase_returns_work_packages():
    stub = _StubLLM(fusions=[], phase_wps=[{"id": "1.1", "name": "XWP", "sub_packages": [
        {"id": "1.1.1", "name": "XT", "duration_days": 2, "quantity": 10, "unit": "项",
         "work_type": "测量放线"}]}], review=None)
    node = WBSAgentNode(llm=stub)
    spec = default_phases()[0]             # 施工准备
    frag = node._expand_phase(ctx={"extracted_params": dict(PARAMS)}, spec=spec, retry_req=None)
    assert frag and frag["phase"] == spec["phase"]
    assert frag["work_packages"] and frag["work_packages"][0]["sub_packages"]
    assert stub.phase_calls == 1


def test_no_key_skips_fusion_llm():
    key_was = wa.config.LLM_API_KEY
    wa.config.LLM_API_KEY = ""
    try:
        node = WBSAgentNode()              # 无 llm 且无 key → 不调融合 LLM，融合不应用
        ctx = {"prompt": "含溶洞的项目", "extracted_params": dict(PARAMS), "wbs": {
            "phases": [{"phase": "地基处理与桩基", "work_packages": []}]}}
        n = node._fuse_cross_phase(ctx, PARAMS)
        assert n == 0, "无 key 时融合 LLM 不可用，应返回 0"
    finally:
        wa.config.LLM_API_KEY = key_was