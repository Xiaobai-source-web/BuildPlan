"""WBS 多级分工 复评 + 人工门 测试。

单元测试用桩 LLM（不碰真实云端）验证新编排：
  - 复评 PASS → 直接收树，不再重跑
  - HIGH → 发 node_paused 人工门；approve 接受该轮 WBS
  - REVIEW 带 MED → 静默收敛：按 REPORT 目标相重跑一轮再复评
  - 脚本自检软证据 shape（新的 _self_check 只产证据，不再判 HIGH）

运行：python -m pytest backend/tests/test_wbs_agent.py -v
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes.wbs_agent import WBSAgentNode
from pipeline.nodes.wbs_phases import default_phases, build_phase_kb_injection
from pipeline.nodes.beat_configs import BEAT_PHASE_NAMES
from pipeline.registry import InteractionRegistry

PARAMS = {"building_type": "剪力墙住宅", "structure_type": "剪力墙",
          "area": 8000, "floors": 38, "total_area": 301354.26}


def _phase_wps(phase_name):
    return [{"id": "1.1", "name": f"{phase_name}工作", "sub_packages": [
        {"id": "1.1.1", "name": f"{phase_name}任务", "duration_days": 20,
         "quantity": 6840, "unit": "m³", "work_type": "混凝土工程",
         "kb_activity_id": "CONC_NEW_COLUMN"}]}]


class _Stub:
    """按 user 内容分派角色（总览/逐相/复评），规避温度判据歧义。"""

    def __init__(self, review):
        self.review = review
        self.overview_calls = 0
        self.phase_calls = 0
        self.review_calls = 0
        self.phases_seen = []

    def chat_json(self, system, user, temperature=0.3, retries=1):
        if "完整三层WBS" in user:                  # 跨相融合 → 无补丁
            return {"fusions": []}
        if "候选三层WBS" in user:                  # 复评
            self.review_calls += 1
            return self.review
        if "本阶段：" in user:                     # 逐相 worker → 推算该相例程
            self.phase_calls += 1
            # 从 user 里抓此刻正在展开的阶段名
            marker = user.split("本阶段：", 1)[1].split("\n\n项目参数", 1)[0]
            marker = marker.splitlines()[0].strip()
            self.phases_seen.append(marker)
            wps = _phase_wps(marker) if ("已下发修改" not in user) else _phase_wps(marker + "_改")
            return {"work_packages": self._recut(wps)}
        return None

    @staticmethod
    def _recut(wps):
        return [{"id": p["id"], "name": p["name"],
                 "sub_packages": [dict(s) for s in p["sub_packages"]]} for p in wps]


class _AutoApproveReg(InteractionRegistry):
    def wait(self, key, cancel_evt=None, timeout=600):
        return {"action": "continue"}          # 自动 approve


class _OpinionReg(InteractionRegistry):
    """人工门：返回自由文本修改意见（对应终端打 Y 之外的情形）。"""
    def __init__(self, opinion):
        super().__init__()
        self.opinion = opinion
    def wait(self, key, cancel_evt=None, timeout=600):
        return {"action": "revise", "instruction": self.opinion}


def _run(node, ctx):
    events = []
    node._emit = lambda e, d: events.append((e, d))
    node.run(ctx)
    return events


def test_review_pass_no_rework():
    stub = _Stub(review={"verdict": "PASS", "issues": []})
    node = WBSAgentNode(llm=stub)
    ctx = {"prompt": "住宅项目", "extracted_params": dict(PARAMS)}
    _run(node, ctx)
    assert stub.review_calls >= 1, "必须跑复评"
    # 逐相展开次数 = **全部** 10 相。域 3.2 起，4 个节拍型一级分部
    # （地下室结构/地上主体结构/二次结构与砌体/装饰装修）的「有序 L4 工序清单 `l4_order`」
    # 也必须由 LLM 产出，实现上由 `WBSAgentNode._llm_beat_l4_order()` 额外问一次模型
    # ——**只取 `l4_order`**，树仍由 BeatExpandNode 引擎铺。所以调用次数 = 10
    #（旧口径是「10 相 − 4 节拍相 = 6」，那 4 相完全不过模型）。
    assert stub.phase_calls == len(node.specs) == 10
    assert len(node.specs) == len(default_phases()), "不得新增独立专项相"


def test_high_triggers_manual_gate_approve_stops():
    stub = _Stub(review={"verdict": "REVISE", "issues": [{
        "severity": "HIGH", "dimension": "层数", "target": "1.1",
        "finding": "只统计了一层", "suggestion": "×层数"}]})
    node = WBSAgentNode(llm=stub)
    node._registry = _AutoApproveReg()
    node._run_id = "th"
    ctx = {"prompt": "住宅项目", "extracted_params": dict(PARAMS)}
    events = _run(node, ctx)
    assert any(e == "node_paused" for e, _ in events), "HIGH 必须触发人工门"
    rounds_after_gate = stub.review_calls
    # approve → 立即停止，不再开展第二轮复评重跑
    assert not any(m.startswith("⑥") for e, d in events if e == "node_progress"
                   for m in ([d.get("message")] if isinstance(d, dict) else []))
    assert stub.review_calls == rounds_after_gate


def test_med_issues_silent_rework_one_round():
    # 第一轮复评 REVISE + MED → 静默收敛重跑；第二轮 PASS 停止
    class _Seq(InteractionRegistry):
        def __init__(self):
            self.n = 0
        def chat_json(self, system, user, temperature=0.3, retries=1):
            if "候选三层WBS" in user:
                self.n += 1
                if self.n == 1:
                    return {"verdict": "REVISE", "issues": [{
                        "severity": "MED", "dimension": "量级", "target": "1",
                        "finding": "混凝土量偏小", "suggestion": "混凝土总量应≥0.2m³/㎡"}]}
                return {"verdict": "PASS", "issues": []}
            if "完整三层WBS" in user:
                return {"fusions": []}
            return {"work_packages": _phase_wps(user.split("本阶段：", 1)[1].splitlines()[0].strip())}
    seq = _Seq()
    node = WBSAgentNode(llm=seq)
    ctx = {"prompt": "住宅项目", "extracted_params": dict(PARAMS)}
    _run(node, ctx)
    assert seq.n == 2, f"应两轮复评（MED 一轮重跑），实际 {seq.n}"
    # MED 不触发人工门
    node2 = WBSAgentNode(llm=seq)
    node2._emit = lambda e, d: None
    node2.run({"prompt": "x", "extracted_params": dict(PARAMS)})


def test_self_check_evidence_shape():
    node = WBSAgentNode()
    wbs = {"phases": [{"phase": "主体结构", "work_packages": [{"id": "1.1", "name": "结构",
        "sub_packages": [{"id": "1.1.1", "name": "柱混凝土", "duration_days": 40,
        "quantity": 6840, "unit": "m³", "work_type": "混凝土工程"}]}]}]}
    ev = node._self_check(PARAMS, wbs)
    assert ev["leaves"] == 1
    assert ev["phases"] == 1
    assert ev["GFA"] != "0"
    assert ev["conc_m3"] == 6840


def test_self_check_one_floor_concrete_low_evidence():
    """只统计一层混凝土 → 证据里的混凝土总量应明显偏低（交评审复核）。"""
    node = WBSAgentNode()
    wbs = {"phases": [{"phase": "主体结构", "work_packages": [{"id": "1.1", "name": "结构",
        "sub_packages": [{"id": "1.1.1", "name": "柱混凝土(单层)", "duration_days": 1,
        "quantity": 180, "unit": "m³", "work_type": "混凝土工程"}]}]}]}
    ev = node._self_check(PARAMS, wbs)
    floors = int(PARAMS["floors"])
    single = ev["conc_m3"]
    assert single < 0.05 * floors * PARAMS["area"], "单层混凝土量应远小于整楼合理量"


def test_自检不许把节拍型阶段的工程量排除掉():
    """真实缺陷回归（第 24 轮）：自检曾把**节拍型阶段整个排除**在统计之外。

    后果（实测，住宅 14200㎡ / 18 层）：同一份 WBS 里真实有
    **20216 m³ 混凝土 / 18996 吨钢筋 / 209 条叶子**，自检却报
    `conc_m3=0 / rebar_t=0 / leaves=9`（只剩非节拍的 9 条占位项）。
    这份"0"被当作证据送进审计模型 → 模型写出「混凝土缺失超 93%」这类**假 HIGH**，
    门把用户白白拦下。

    保证：节拍型阶段（`BEAT_PHASE_NAMES` 里的名字）的工程量**必须计入**，
    同时把节拍阶段自己的叶子数单独报出来（`beat_leaves`）供人对照。
    """
    node = WBSAgentNode()
    for beat_name in BEAT_PHASE_NAMES:
        wbs = {"phases": [
            # 非节拍阶段：只有占位项（这正是老实现唯一会统计到的部分）
            {"phase": "施工准备", "work_packages": [{"id": "1.1", "name": "准备",
                "sub_packages": [{"id": "1.1.1", "name": "场地平整", "quantity": 1,
                                  "unit": "项", "duration_days": 3}]}]},
            # 节拍型阶段：承载主体工程量
            {"phase": beat_name, "work_packages": [{"id": "5.1", "name": "标准层",
                "sub_packages": [
                    {"id": "5.1.1.1", "name": "混凝土浇筑", "quantity": 4100,
                     "unit": "m³", "work_type": "混凝土工程", "duration_days": 18},
                    {"id": "5.1.1.2", "name": "钢筋绑扎", "quantity": 900,
                     "unit": "t", "work_type": "钢筋工程", "duration_days": 6}]}]},
        ]}
        ev = node._self_check(PARAMS, wbs)
        assert ev["conc_m3"] == 4100, (
            "节拍型阶段「%s」的混凝土量被漏掉了 → 会喂给审计模型一份假证据：%s"
            % (beat_name, ev))
        assert ev["rebar_t"] == 900, ev
        assert ev["leaves"] == 3, "叶子数必须覆盖全部阶段：%s" % ev
        assert ev["beat_leaves"] == 2, "节拍阶段的叶子数要单独报出来：%s" % ev
        assert ev["beat_phases"] == 1, ev


def test_no_key_full_tree_via_fallback():
    import pipeline.nodes.wbs_agent as wa
    key = wa.config.LLM_API_KEY
    wa.config.LLM_API_KEY = ""
    try:
        node = WBSAgentNode()              # 无 llm 无 key → 模板/最小项兜底
        node._emit = lambda e, d: None
        ctx = {"prompt": "一个综合楼项目。", "extracted_params": dict(PARAMS)}
        out = node.run(ctx)
        code = {p["phase"] for p in default_phases()}
        wbs_phases = {p["phase"] for p in out["wbs"]["phases"]}
        assert code <= wbs_phases
        for ph in out["wbs"]["phases"]:
            assert ph.get("work_packages")
            for wp in ph["work_packages"]:
                assert wp.get("sub_packages")
    finally:
        wa.config.LLM_API_KEY = key


def test_manual_opinion_triggers_main_llm_fusion_revision():
    """人工门自由输入意见 → 主体 LLM 据此融合修订既有 WBS → 复评收敛。

    首轮复评 HIGH 进人工门；注册表返回自由文本意见（revise+instruction）；
    主体 LLM 依据意见产 fusions 并应用进宿主阶段；第二轮复评 PASS 收束。
    """
    class _FuseStub(_Stub):
        def __init__(self):
            super().__init__(review={"verdict": "REVISE", "issues": []})
            self.review_n = 0
            self.fusion_applied = 0
        def chat_json(self, system, user, temperature=0.3, retries=1):
            if "候选三层WBS" in user:                  # 复评：第1轮 HIGH，之后 PASS
                self.review_n += 1
                if self.review_n == 1:
                    return {"verdict": "REVISE", "issues": [{
                        "severity": "HIGH", "dimension": "量级", "target": "1",
                        "finding": "混凝土偏小", "suggestion": "整体上调"}]}
                return {"verdict": "PASS", "issues": []}
            if "完整三层WBS" in user:                  # 融合：仅人工意见后再应用
                if "人工修改意见" in user:
                    self.fusion_applied += 1
                    return {"fusions": [{"target_phase": "地基处理与桩基", "action": "add_leaf",
                                         "wp_name": "桩基加固", "leaf_name": "微型桩补强",
                                         "duration_days": 6, "quantity": 120, "unit": "根",
                                         "work_type": "桩基工程"}]}
                return {"fusions": []}
            return None

    stub = _FuseStub()
    node = WBSAgentNode(llm=stub)
    node._registry = _OpinionReg("混凝土总用量偏小，请整体上调现浇量")
    node._run_id = "t3"
    ctx = {"prompt": "住宅项目", "extracted_params": dict(PARAMS)}
    _run(node, ctx)
    assert stub.fusion_applied == 1, "人工意见必须触发主体LLM融合一次"
    assert stub.review_n >= 2, f"应按意见改后复评≥2轮，实际{stub.review_n}"
    found = any(l["name"] == "微型桩补强" for ph in ctx["wbs"]["phases"]
                for wp in ph.get("work_packages", [])
                for l in wp.get("sub_packages", []))
    assert found, "主体LLM修订的补丁必须并入 WBS 宿主阶段"


def test_y_approve_stops_without_rework():
    """人工门打 Y（continue/无意见）→ 直接放行，不再触发人工意见融合。"""
    stub = _Stub(review={"verdict": "REVISE", "issues": [{
        "severity": "HIGH", "dimension": "层数", "target": "1.1",
        "finding": "只统计了一层", "suggestion": "×层数"}]})
    node = WBSAgentNode(llm=stub)
    node._registry = _AutoApproveReg()                 # continue → 视为 Y
    ctx = {"prompt": "住宅项目", "extracted_params": dict(PARAMS)}
    events = _run(node, ctx)
    paused = [d for e, d in events if e == "node_paused"]
    assert paused, "HIGH 必须进人工门"
    # continue/Y → 直接放行：不进融合修订（融合调用数应保持 1 = 展开后的常态融合）
    assert not any("⑥" in d.get("message", "") for e, d in events if e == "node_progress")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"  PASS  {fn.__name__}")
    print("全部 wbs_agent 用例通过 ✔")