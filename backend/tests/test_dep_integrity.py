# -*- coding: utf-8 -*-
"""工序依赖「孤儿守卫」回归测试（第 43 轮）。

真实缺陷（本文件的由来）——`backend/plans/plan_sample3_after_allfix.json`：
  310 条叶子 / 447 条依赖里，**13 条叶子没有前置**，且全部落在开工头三天
  （2026-06-01 起）。其中 5 条（3.3.1 / 3.3.2 / 3.4.1 / 8.2.1.1 / 8.3.1.1）**连后续也
  没有**，是彻底孤立节点。三条工程上绝不可能：
    · `3.3.1 地下室周边回填`  2026-06-01→06-02 —— 必须等**地下室结构收尾**；
    · `3.3.2 回填土夯实`      2026-06-03→06-04 —— 必须等回填；
    · `8.2.1.1 外檐保温（全楼平行）` 2026-06-01→06-06 —— 必须在主体/装饰阶段；
    · `8.3.1.1 外檐涂料（全楼平行）` 2026-06-01→06-05 —— 同病（极易被漏说的一条）。
  根因（详见 deps_gen.py 顶部注释）：`prompts/deps_gen.txt` 第 5 条明确允许
  「独立任务可以没有任何依赖」，而 DepsGenNode 改动前只做「叶子化 + 环检测 + 有环
  回退顺序链」，**从不检查覆盖**；`ctx["deps_warnings"]` 更是在全仓没有任何消费方，
  连已有告警都没落过盘。于是"该有前置却没有"可以一路静默走到交付物。

本文件钉五件事：
  ① 兜底：阶段 > 1 却没有任何前置的叶子，会被补上一条**合理前置**（同工作包前一条
     工序 / 结构阶段收尾 / 前一节拍阶段收尾），并留一条**含任务 ID** 的告警；
  ② 落盘：告警经引擎 `emit("warning")` 收集 → `plan_assembler.build_meta` 的
     `meta["node_warnings"]`（人可核对、交付物可展示）；
  ③ 白名单：施工准备（1.x）与监测类（3.4.x / 名称含"监测"）**不**被误报；
  ④ 成环与补不出：候选会成环时不许补；一条都补不出时必须留告警列出任务 ID；
  ⑤ 真计划复核：修后 `3.3.1`（以及 `3.3.2` / `8.2.1.1` / `8.3.1.1`）有前置，且
     **不存在「阶段 > 1 且无前置且无告警」的任务**。
     另：`3.3.3` 是**整条任务不存在**（不是"有任务没排上日期"），这里用两条不变量
     把"静默丢任务"钉死 —— WBS 里每一条叶子都必须在任务表里有日期（真计划 310/310）。

运行：python -m pytest backend/tests/test_dep_integrity.py -q -p no:cacheprovider
"""

import datetime
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.engine import NODE_WARNINGS_CTX_KEY, Pipeline      # noqa: E402
from pipeline.nodes import deps_gen as DG                       # noqa: E402
from pipeline.nodes import plan_assembler as PA                 # noqa: E402

#: 真计划（只读；本文件**绝不**写它，也绝不改 `输出结果/` 下任何东西）
REAL_PLAN = BACKEND / "plans" / "plan_sample3_after_allfix.json"


# ==================== 构造工具 ====================
def _leaf(tid, name, duration=3):
    return {"id": tid, "name": name, "duration_days": duration}


def _wp(wid, name, *leaves):
    return {"id": wid, "name": name, "sub_packages": list(leaves)}


def _wbs(*phases):
    """`_wbs(("施工准备", wp1, wp2), ("主体结构", [wp3, wp4]))` → 三层 WBS。

    写法宽容：一个阶段后面的工作包可以直接平铺，也可以装进一个 list/tuple。
    """
    out = []
    for ph in phases:
        nm, rest = ph[0], list(ph[1:])
        if len(rest) == 1 and isinstance(rest[0], (list, tuple)):
            wps = list(rest[0])
        else:
            wps = [w for w in rest if isinstance(w, dict)]
        out.append({"phase": nm, "work_packages": wps})
    return {"phases": out}


def _dep(pred, succ, dtype="FS", lag=0):
    return {"predecessor": pred, "successor": succ, "type": dtype, "lag_days": lag}


def _beat(tid, name, parallel=False):
    """节拍叶子（真计划里 `layer_engine._make_leaf` 会给它写 `_beat: True`）。"""
    leaf = {"id": tid, "name": name, "duration_days": 5, "_beat": True}
    if parallel:
        leaf["_parallel"] = True
    return leaf


def _mirror_wbs():
    """**镜像真计划的阶段次序**（1 准备 / 2 桩基 / 3 基坑 / 4 地下室结构 / 5 主体 /
    6 二次结构 / 7 机电 / 8 装饰 / 9 室外），并带上节拍标记。

    为什么必须镜像：判据里的"阶段号 > 1"与规则里的"上一阶段/前一节拍阶段"都按**阶段
    位置**算；真计划里 `3.3 土方回填` 挂在第 3 阶段（前面还有两段），合成用例若不补齐
    前两段，会把"基坑回填"错当成第 1 阶段（从而被当成"本就该从开工起干"）。
    """
    return _wbs(
        ("施工准备", _wp("1.1", "场地平整", _leaf("1.1.1", "场地平整"))),
        ("地基处理与桩基", _wp("2.1", "桩基", _leaf("2.1.1", "预应力管桩施工"))),
        ("基坑支护与土方",
         _wp("3.2", "土方开挖", _leaf("3.2.1", "基坑土方开挖")),
         _wp("3.3", "土方回填", _leaf("3.3.1", "地下室周边回填"),
             _leaf("3.3.2", "回填土夯实"))),
        ("地下室结构", _wp("4.1", "结构", _beat("4.1.1.1", "1-0.5层 钢筋绑扎"),
                           _beat("4.1.4.3", "顶板 混凝土浇筑"))),
        ("地上主体结构", _wp("5.1", "主体", _beat("5.1.1.1", "1层 钢筋绑扎"))),
        ("二次结构与砌体", _wp("6.1", "砌体", _beat("6.1.1.1", "1层 ALC墙板"),
                               _beat("6.1.18.4", "18层 勾缝"))),
        ("机电安装", _wp("7.1", "机电", _leaf("7.1.1", "电气线管敷设"))),
        ("装饰装修",
         _wp("8.1", "内装", _beat("8.1.1.1", "1-3层 内墙抹灰")),
         _wp("8.2", "外檐保温", _beat("8.2.1.1", "外檐保温（全楼平行）", parallel=True)),
         _wp("8.3", "外檐涂料", _beat("8.3.1.1", "外檐涂料（全楼平行）", parallel=True))),
        ("室外工程", _wp("9.1", "室外", _leaf("9.1.1", "室外管网"))),
    )


def _pred_map(deps):
    """`{后继: {前置...}}`（"这条任务的前置有谁"）。"""
    out = {}
    for d in deps:
        out.setdefault(str(d["successor"]), set()).add(str(d["predecessor"]))
    return out


def _succ_map(deps):
    """`{前置: {后继...}}`（"这条任务的后继有谁"）。"""
    out = {}
    for d in deps:
        out.setdefault(str(d["predecessor"]), set()).add(str(d["successor"]))
    return out


def _pairs(deps):
    return [(str(d["predecessor"]), str(d["successor"])) for d in deps]


def _blob(obj):
    return json.dumps(obj, ensure_ascii=False)


class _FakeLLM:
    """假模型：`chat_json` 直接返回给定依赖集（绝不联网，conftest 也强制无 Key）。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_json(self, prompt, user, temperature=None):
        self.calls += 1
        return self.payload


def _run_deps_node(wbs, llm_payload):
    """把 DepsGenNode 放进单节点流水线跑一遍，返回 (ctx, 事件表)。

    注意 `Pipeline.add_node()` 返回的是**节点**（不是流水线），写法照
    test_node_warnings.py：先建流水线、再 add_node、最后 run。
    """
    node = DG.DepsGenNode(llm=_FakeLLM(llm_payload))
    ctx = {"wbs": wbs}
    seen = []
    pipe = Pipeline(run_id="t")
    pipe.add_node(node)
    pipe.run(ctx, emit=lambda ev, d: seen.append((ev, d)))
    return ctx, seen


# ==================== ① 兜底：该有前置的必须补上，且留告警 ====================

class TestOrphanGuardFills:
    def test_阶段大于1缺前置被补上同工作包前一条工序(self):
        """`5.1.2` 没有任何前置 → 按"同工作包内的前一条工序"补 `5.1.1 → 5.1.2`。"""
        wbs = _wbs(
            ("施工准备", _wp("1.1", "场地平整", _leaf("1.1.1", "场地平整"))),
            ("主体结构", _wp("5.1", "结构施工", _leaf("5.1.1", "钢筋绑扎"),
                             _leaf("5.1.2", "混凝土浇筑"))),
        )
        deps = [_dep("1.1.1", "5.1.1")]
        new, warns, applied = DG.ensure_dependencies(deps, wbs)

        assert ("5.1.1", "5.1.2") in _pairs(new), new
        assert applied == [{"successor": "5.1.2", "predecessor": "5.1.1",
                            "reason": "同一工作包内的前一条工序", "direction": "pred"}], applied
        assert len(warns) == 1, warns
        assert "5.1.2" in _blob(warns[0]), "告警必须点出任务 ID（人可核对）：%s" % _blob(warns[0])
        assert "5.1.1" in warns[0]["detail"]

    def test_不修改入参且不编造叶子(self):
        """守卫是纯函数：入参 deps 原样不动；端点不存在的边丢弃并留痕。"""
        wbs = _wbs(("施工准备", _wp("1.1", "场地平整", _leaf("1.1.1", "场地平整"))),
                   ("主体结构", _wp("5.1", "结构施工", _leaf("5.1.1", "钢筋绑扎"))))
        deps = [_dep("1.1.1", "5.1.1"), _dep("9.9.9", "5.1.1")]
        before = json.loads(_blob(deps))
        new, warns, applied = DG.ensure_dependencies(deps, wbs)

        assert deps == before, "入参不许被改"
        assert applied == [], applied
        assert _pairs(new) == [("1.1.1", "5.1.1")], new
        assert len(warns) == 1 and "9.9.9" in warns[0]["detail"], warns

    def test_幂等_再跑一遍不再补(self):
        wbs = _wbs(("施工准备", _wp("1.1", "场地平整", _leaf("1.1.1", "场地平整"))),
                   ("主体结构", _wp("5.1", "结构施工", _leaf("5.1.1", "钢筋绑扎"),
                                    _leaf("5.1.2", "混凝土浇筑"))))
        once, warns1, applied1 = DG.ensure_dependencies([_dep("1.1.1", "5.1.1")], wbs)
        twice, warns2, applied2 = DG.ensure_dependencies(once, wbs)

        assert applied1 and not applied2, (applied1, applied2)
        assert not warns2, warns2
        assert once == twice

    def test_回填类等结构阶段收尾_外檐平行等前一节拍阶段(self):
        """两条**跨阶段**规则：回填类 → 之后最近结构阶段收尾；外檐平行 → 前一节拍阶段收尾。"""
        wbs = _mirror_wbs()
        # 内装首条搭到二次结构收尾（与 beat_configs 的 lead_in.from_node=6 一致）
        deps = [_dep("6.1.18.4", "8.1.1.1")]
        new, warns, applied = DG.ensure_dependencies(deps, wbs)
        preds = _pred_map(new)

        assert preds["3.3.1"] == {"4.1.4.3"}, preds["3.3.1"]
        assert preds["3.3.2"] == {"3.3.1"}, preds["3.3.2"]
        assert preds["8.2.1.1"] == {"6.1.18.4"}, preds["8.2.1.1"]
        assert preds["8.3.1.1"] == {"8.2.1.1"}, preds["8.3.1.1"]
        # 回填收尾接室外工程（回填后工序）
        succs = _succ_map(new)
        assert "9.1.1" in succs["3.3.2"], succs["3.3.2"]
        assert not DG.has_cycle(new, DG.collect_leaf_ids(wbs))
        assert "3.3.1" in _blob(warns) and "8.2.1.1" in _blob(warns), warns

    def test_注释里的三类理由都能被机器识别(self):
        """`should_have_predecessor` 的判据与白名单必须可解释（告警/追溯要用）。"""
        items = DG.leaf_items(_wbs(
            ("施工准备", _wp("1.1", "准备", _leaf("1.1.1", "场地平整"))),
            ("基坑", _wp("3.4", "监测", _leaf("3.4.1", "基坑变形监测")),
             _wp("3.5", "别处监测", _leaf("3.5.1", "沉降观测")),
             _wp("3.6", "回填", _leaf("3.6.1", "回填土夯实"))),
        ))
        got = dict((it["id"], DG.should_have_predecessor(it)) for it in items)

        assert got["1.1.1"][0] is False and "施工准备" in got["1.1.1"][1]
        assert got["3.4.1"][0] is False and "3.4." in got["3.4.1"][1]
        assert got["3.5.1"][0] is False, "叫「沉降观测」的工序按全程观测看待"
        assert ("监测" in got["3.5.1"][1]) or ("观测" in got["3.5.1"][1]), got["3.5.1"][1]
        assert got["3.6.1"][0] is True and "阶段 2" in got["3.6.1"][1]


# ==================== ② 落盘：告警进 meta.node_warnings ====================

class TestWarningLandsInMeta:
    def test_告警经引擎收集并落进meta且事件照旧转发(self):
        wbs = _wbs(
            ("施工准备", _wp("1.1", "场地平整", _leaf("1.1.1", "场地平整"))),
            ("主体结构", _wp("5.1", "结构施工", _leaf("5.1.1", "钢筋绑扎"),
                             _leaf("5.1.2", "混凝土浇筑"))),
        )
        # 模型只给了一条依赖：5.1.2 既无前置也无后续 —— 正是真实缺陷的形态
        ctx, seen = _run_deps_node(wbs, {"dependencies": [_dep("1.1.1", "5.1.1")]})

        items = ctx.get(NODE_WARNINGS_CTX_KEY)
        assert isinstance(items, list) and len(items) == 1, items
        assert items[0]["node"] == "deps"
        assert items[0]["count"] == 1 and items[0]["at"]
        assert "5.1.2" in _blob(items[0]), items[0]
        # 事件照旧转发（留档是旁路，终端/UI 行为不变）
        assert any(ev == "warning" and "5.1.2" in _blob(d) for ev, d in seen), seen
        # 兜底后的依赖真的写回了 ctx
        deps = (ctx["dependencies"] or {}).get("dependencies") or []
        assert ("5.1.1", "5.1.2") in _pairs(deps), deps

        meta = PA.build_meta(ctx)
        assert meta["node_warning_count"] == 1
        assert "5.1.2" in _blob(meta["node_warnings"]), meta["node_warnings"]

    def test_白名单不误报_一条告警都不发(self):
        wbs = _wbs(
            ("施工准备", _wp("1.1", "准备", _leaf("1.1.1", "场地平整"),
                             _leaf("1.2.1", "测量控制网建立"),
                             _leaf("1.5.1", "台风季节防风措施"))),
            ("基坑支护与土方", _wp("3.4", "基坑监测", _leaf("3.4.1", "基坑变形监测"),
                                   _leaf("3.4.2", "周边环境监测"))),
        )
        ctx, seen = _run_deps_node(wbs, {"dependencies": []})

        assert not ctx.get(NODE_WARNINGS_CTX_KEY), ctx.get(NODE_WARNINGS_CTX_KEY)
        assert not [d for ev, d in seen if ev == "warning"], seen
        assert PA.build_meta(ctx)["node_warnings"] == []


# ==================== ③ 成环与"补不出" ====================

class TestCycleAndUnresolved:
    def test_候选会成环就不补_并如实报缺口(self):
        """唯一候选 `1.1.1` 反过来依赖 `5.1.1`（畸形输入）→ 不许补，且必须报缺口。"""
        wbs = _wbs(("施工准备", _wp("1.1", "准备", _leaf("1.1.1", "场地平整"))),
                   ("主体结构", _wp("5.1", "结构", _leaf("5.1.1", "钢筋绑扎"))))
        deps = [_dep("5.1.1", "1.1.1")]          # 反向：5.1.1 是 1.1.1 的前置
        new, warns, applied = DG.ensure_dependencies(deps, wbs)

        assert applied == [], applied
        assert not DG.has_cycle(new, DG.collect_leaf_ids(wbs))
        assert len(warns) == 1 and "缺口" in warns[0]["message"], warns
        assert "5.1.1" in warns[0]["detail"], warns

    def test_一条候选都推不出时列出缺前置的任务ID(self):
        """阶段 2 只有一条工序、阶段 1 没有工序 → 没有任何可用候选 → 报缺口。"""
        wbs = _wbs(("施工准备", _wp("1.0", "空", )),
                   ("主体结构", _wp("5.1", "结构", _leaf("5.1.1", "钢筋绑扎"))))
        new, warns, applied = DG.ensure_dependencies([], wbs)

        assert applied == [] and not DG.has_cycle(new, DG.collect_leaf_ids(wbs))
        assert len(warns) == 1, warns
        assert "5.1.1" in warns[0]["detail"] and "缺口" in warns[0]["message"]

    def test_补入后成环时整棵回退顺序链并留告警(self, monkeypatch):
        """万一兜底补边仍成环（畸形输入），宁可回退顺序链，也不给 CPM 一个无解图。"""
        wbs = _wbs(("施工准备", _wp("1.1", "准备", _leaf("1.1.1", "场地平整"))),
                   ("主体结构", _wp("5.1", "结构", _leaf("5.1.1", "钢筋绑扎"),
                                    _leaf("5.1.2", "混凝土浇筑"))))
        # 人为让"补完后的环检测"为真：模拟补边后仍有环的极端输入。
        # 注意判据要落在**补入的边**上（5.1.2 是被补上来的后继），而不是 predecessor
        # —— 否则补边后条件仍为假，回退分支根本不进（实测踩过这个坑）。
        monkeypatch.setattr(DG, "has_cycle",
                            lambda deps, leaves: any(
                                str(d.get("successor")) == "5.1.2" for d in deps))
        ctx, seen = _run_deps_node(wbs, {"dependencies": []})

        assert any("回退整棵顺序链" in _blob(d) for ev, d in seen if ev == "warning"), seen
        # 顺序链兜底 → 除第一条外每条都有前置（1.1.1 是阶段 1，豁免）
        assert ctx[NODE_WARNINGS_CTX_KEY], ctx.get(NODE_WARNINGS_CTX_KEY)


# ==================== ④ 真计划复核 ====================

@pytest.mark.skipif(not REAL_PLAN.exists(),
                    reason="plans/ 是运行产物；冻结档案不在干净 clone 里")
class TestRealPlan:
    @classmethod
    def setup_class(cls):
        cls.plan = json.loads(REAL_PLAN.read_text(encoding="utf-8"))
        cls.wbs = cls.plan["wbs"]
        cls.deps = cls.plan["dependencies"]
        cls.leaf_ids = DG.collect_leaf_ids(cls.wbs)
        cls.sched = {t["task_id"]: t for t in cls.plan["all_tasks_schedule"]}

    def test_缺陷复现_原始依赖里确有13条无前置且全在开工头三天(self):
        """先把"缺陷真的存在"钉住（防止有人把守卫删了还看不出来）。"""
        preds = _pred_map(self.deps)
        orphans = [tid for tid in self.leaf_ids if not preds.get(tid)]

        for tid in ("3.3.1", "3.3.2", "8.2.1.1", "8.3.1.1"):
            assert tid in orphans, "这条缺陷必须仍可复现（否则本文件失去意义）"
        starts = sorted(t["start_date"] for t in self.sched.values() if t.get("start_date"))
        first_day = datetime.date.fromisoformat(starts[0])
        window_end = (first_day + datetime.timedelta(days=2)).isoformat()
        assert all(self.sched[tid]["start_date"] <= window_end for tid in orphans), \
            ("13 条孤儿任务全部压在开工头三天（%s~%s）—— 这正是缺陷的表现："
             "它们没有来路，只能从第 0 天起排" % (starts[0], window_end))
        on_first_day = [tid for tid in orphans
                        if self.sched[tid]["start_date"] == starts[0]]
        assert len(on_first_day) >= 12, "至少 12 条直接落在开工当天：%s" % on_first_day

    def test_修后_3_3_x与8_2_1_1的前后关系接对(self):
        new, warns, applied = DG.ensure_dependencies(self.deps, self.wbs)
        preds = _pred_map(new)
        succs = _succ_map(new)

        # 回填 ← 地下室结构收尾（阶段 4 = 「地下室结构」）；夯实 ← 回填；回填收尾 → 室外
        assert preds["3.3.1"], "3.3.1 地下室周边回填必须有前置"
        assert "4.1.4.3" in preds["3.3.1"], preds["3.3.1"]
        assert preds["3.3.2"] == {"3.3.1"}, preds["3.3.2"]
        assert succs.get("3.3.1") == {"3.3.2"}, succs.get("3.3.1")
        outdoor = [it["id"] for it in DG.leaf_items(self.wbs)
                   if "室外" in it["phase_name"]]
        assert outdoor and outdoor[0] in succs.get("3.3.2", set()), succs.get("3.3.2")
        # 外檐保温/涂料必须在主体/装饰阶段之后，不是开工当天
        assert preds["8.2.1.1"], "8.2.1.1 外檐保温必须有前置"
        assert preds["8.3.1.1"], "8.3.1.1 外檐涂料必须有前置"
        # 补的每条都在告警里点名
        blob = _blob(warns)
        for tid in ("3.3.1", "3.3.2", "8.2.1.1", "8.3.1.1"):
            assert tid in blob, "%s 补了前置就必须在告警里点名" % tid
        assert not DG.has_cycle(new, self.leaf_ids)

    def test_不存在阶段大于1且无前置且无告警的任务(self):
        """结构性保证的核心断言：守卫跑完，这个组合必须为空集。"""
        new, warns, applied = DG.ensure_dependencies(self.deps, self.wbs)
        preds = _pred_map(new)
        warned = _blob(warns)          # 告警（含明细）里出现过的任务 = 已如实说明

        bad = []
        for it in DG.leaf_items(self.wbs):
            need, _why = DG.should_have_predecessor(it)
            if need and not preds.get(it["id"]) and it["id"] not in warned:
                bad.append(it["id"])
        assert bad == [], "阶段>1、无前置、又没有告警的任务：%s" % bad

    def test_3_3_3_不在计划树里_不存在静默丢日期的任务(self):
        """「3.3.3 没有任何日期」的如实结论：**这条任务不存在**，不是"有任务没排上"。

        复核（自动验收脚本 + 本文件均确认）：真计划（plan_sample3_after_allfix）的 WBS
        里土方回填工作包（3.3）只生成了 `3.3.1` / `3.3.2` 两条叶子，任务表里自然也没有
        3.3.3 —— `sched.get("3.3.3")` 返回 None 是"键不存在"，不是"日期为 None"。
        实测 310/310 条叶子全部进了排程，漏排 0 条。所以不能为它造数据，也不能去
        scheduler 里找"为什么被跳过"（那里没有可查的问题）。

        真正要防的是**另一种**静默：叶子在 WBS 里、却没有排程行 → 下游
        `plan_assembler.build_parts` 会 `if not s: continue` 静默跳过，计划里再没有这条
        任务。这里对真计划把这条底线钉死：WBS 里每一条叶子都必须在任务表里有日期。
        """
        assert "3.3.3" not in self.leaf_ids, "真计划的 3.3 工作包只有 3.3.1/3.3.2"
        assert "3.3.3" not in self.sched, "3.3.3 是整条任务不存在，不是日期缺失"
        assert "3.3.1" in self.leaf_ids and "3.3.2" in self.leaf_ids

        missing = [tid for tid in self.leaf_ids
                   if not (self.sched.get(tid) or {}).get("start_date")
                   or not (self.sched.get(tid) or {}).get("finish_date")]
        assert missing == [], "WBS 里的叶子却在任务表里没有日期（静默消失）：%s" % missing
        assert len(self.sched) == len(self.leaf_ids), \
            "任务表条数必须与叶子条数一致（%d vs %d）" % (len(self.sched), len(self.leaf_ids))

    def test_所有存档计划都满足_WBS叶子全都有日期(self):
        """跨计划不变量（把"漏排"这件事钉死，而不是只信一份计划）。

        注意：3.3.3 **存在与否**是 WBS 生成口径的差异（8 份存档里只有
        plan_sample3_after_fix 有它，且它有正常日期）—— 所以这里不查"谁有 3.3.3"，
        只查"凡是在 WBS 里的叶子，都必须有日期"。这才是排程侧的不变量。
        """
        plans = sorted((BACKEND / "plans").glob("plan_*.json"))
        if len(plans) < 3:
            # 域 9.3：`plans/` 是**运行产物**，干净 clone 上不存在。原先这里
            # `assert plans` + 末尾 `assert checked >= 3` 会在干净机器上假失败 ——
            # 跨档案不变量在"没有档案"时本就无从谈起，跳过才是正确语义。
            pytest.skip("plans/ 是运行产物；本机只有 %d 份存档，跨档案不变量无从谈起"
                        % len(plans))
        assert plans, "计划存档目录不见了？"
        checked = 0
        for path in plans:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue          # 损坏的存档不该让本条断言失败（另有专门的用例管）
            ids = DG.collect_leaf_ids(data.get("wbs") or {})
            if not ids:
                continue
            sched = {str(t.get("task_id")): t
                     for t in (data.get("all_tasks_schedule") or [])}
            missing = [tid for tid in ids
                       if not (sched.get(tid) or {}).get("start_date")]
            assert missing == [], "%s：WBS 叶子没有排上日期：%s" % (path.name, missing[:8])
            checked += 1
        assert checked >= 3, "至少要看几份存档才算数，实际 %d 份" % checked
