# -*- coding: utf-8 -*-
"""WS5 契约 §8（E1）回归测试：伴随型工序不得锁主体。

真实缺陷（本文件的由来）——`backend/plans/plan_run_1789895021.json`（304 叶子 / 447 依赖）：
  `3.4.2 周边环境监测`（1 人 60 天）被写成 `3.4.2 --FS--> 4.1.1.1`（1-0.5 层钢筋绑扎），
  于是"主体结构第一根钢筋"必须等 60 天观测窗全部做完：主体开工被整体锁后 60 天，
  而监测自己（无前置、开工当天起算）成了**关键路径起点**。同一形态在 8 份历史计划里
  稳定复现（`plan_run_1789567958` / `plan_sample3_after_org_v2` / `_after_allfix` …），
  所以它是**规则缺失**，不是"模型这一次写错了"。

根因（详见 `deps_gen.py` 顶部与 `parallelize_companion_deps` 的注释）：
  依赖主要来自 LLM（`deps_gen.py` 调 `prompts/deps_gen.txt`），提示词只教"写出工序先后"，
  从没说"伴随型工序不构成顺序门槛"；而节点改动前只做叶子化 + 环检测，
  **从不审边的语义**，于是"监测在时间上与主体重叠"被写成了 FS（必须做完才能开始）。

本文件钉六件事：
  ① 判据可复用：按**任务名关键词 + 工种(work_type)**判定，不针对任何单个 task_id；
  ② 改判正确：伴随型 → 后续主体工序的 FS 边降级为 **SS**，并在伴随工序叶子上写
     `dependency_note`（契约 §8 要求）；
  ③ 不误伤：主体→伴随、伴随→伴随、伴随→验收/资料类，一律保持 FS；
  ④ 安全：只改边型不改拓扑 → 不引入环；入参 deps 不被修改；规则幂等；
  ⑤ 路径覆盖：LLM 路径之外，**顺序链兜底**与**节拍搭接**两条路径同样生效；
  ⑥ 真计划验收：`plan_run_1789895021` 重算后 `3.4.2` 不再是 `4.1.1.1` 的 FS 前置、
     不再出现在关键路径上、关键路径起点不再是监测。

运行：python -m pytest backend/tests/test_final_ws5_companion_parallel.py -q \
        --basetemp=backend/_probe_tmp/pt_ws5 -p no:cacheprovider
"""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes import cpm                                    # noqa: E402
from pipeline.nodes import deps_gen as DG                         # noqa: E402

PLANS = BACKEND / "plans"
#: 病征最干净的一份冻结计划：修复前关键路径起点就是 `3.4.2 周边环境监测`。
REAL_PLAN = PLANS / "plan_run_1789895021.json"


# ==================== 构造工具 ====================
def _leaf(tid, name, duration=3, work_type=None):
    leaf = {"id": tid, "name": name, "duration_days": duration}
    if work_type:
        leaf["work_type"] = work_type
    return leaf


def _wp(wid, name, *leaves):
    return {"id": wid, "name": name, "sub_packages": list(leaves)}


def _wbs(*phases):
    out = []
    for ph in phases:
        nm, rest = ph[0], list(ph[1:])
        wps = list(rest[0]) if (len(rest) == 1 and isinstance(rest[0], (list, tuple))) \
            else [w for w in rest if isinstance(w, dict)]
        out.append({"phase": nm, "work_packages": wps})
    return {"phases": out}


def _dep(pred, succ, dtype="FS", lag=0):
    return {"predecessor": pred, "successor": succ, "type": dtype, "lag_days": lag}


def _types(deps):
    return {(str(d["predecessor"]), str(d["successor"])): str(d.get("type")) for d in deps}


def _leaf_of(wbs, tid):
    for ph in wbs["phases"]:
        for wp in ph.get("work_packages") or []:
            for s in wp.get("sub_packages") or []:
                if s.get("id") == tid:
                    return s
    raise AssertionError("no leaf %s" % tid)


def _plan(path):
    p = json.loads(Path(path).read_text(encoding="utf-8"))
    deps = p.get("dependencies") or []
    if isinstance(deps, dict):
        deps = deps.get("dependencies") or []
    return p.get("wbs") or {}, deps


class _NoLLM:
    """即时抛错的桩 LLM → 强制 deps 走"顺序链兜底"路径，不碰网络。"""

    def chat_json(self, *a, **k):
        raise RuntimeError("no network in tests")


def _silence(node):
    node._emit = lambda event, data: None
    return node


#: 一条典型的最小 WBS：3.4 监测（伴随型）排在 4.1 主体之前，与真计划的形态一致。
def _companion_wbs():
    return _wbs(
        ("施工准备", _wp("1.1", "场地平整", _leaf("1.1.1", "场地平整", 5))),
        ("基坑支护与土方",
         _wp("3.4", "监测", _leaf("3.4.1", "基坑变形监测", 60, "监测工程"),
             _leaf("3.4.2", "周边环境监测", 60, "监测工程")),
         _wp("3.2", "土方", _leaf("3.2.3", "基底钎探", 3))),
        ("地下室结构", _wp("4.1", "结构", _leaf("4.1.1.1", "1-0.5层 钢筋绑扎", 2))),
    )


# ==================== ① 判据可复用 ====================
class TestIsCompanionTask:
    """判据 = 任务名关键词 ∪ 工种(work_type) 关键词（契约 §8 的用户清单）。"""

    def test_按任务名关键词命中五类伴随工序(self):
        for name in ("周边环境监测", "沉降观测", "基坑降水", "混凝土养护",
                     "成品保护（阳角）"):
            item = DG.leaf_items(_wbs(("基坑", _wp("3.4", "x", _leaf("3.4.1", name)))))[0]
            hit, why = DG.is_companion_task(item)
            assert hit, "%s 应判为伴随型工序" % name
            assert "任务名" in why, why

    def test_按工种命中_即使任务名不含关键词(self):
        """真计划里 `9.3.5 绿化养护（初期）` 的 name 与 work_type 都含「养护」；
        这里刻意把 name 写成不含关键词，验证**工种**这条判据单独也成立。"""
        item = DG.leaf_items(_wbs(
            ("室外工程", _wp("9.3", "绿化", _leaf("9.3.9", "苗木浇灌与修剪", 30, "养护工程")))))[0]
        hit, why = DG.is_companion_task(item)
        assert hit and "养护工程" in why, why

    def test_普通主体工序不命中(self):
        for name, wt in (("1-0.5层 钢筋绑扎", "钢筋工程"), ("地下室周边回填", "土方工程"),
                         ("ALC墙板安装", "砌筑工程"), ("外檐涂料", "涂饰工程")):
            item = DG.leaf_items(_wbs(("主体", _wp("4.1", "x", _leaf("4.1.1", name, 3, wt)))))[0]
            assert DG.is_companion_task(item)[0] is False, name

    def test_不针对单个task_id(self):
        """同一个工种的工序换个 id（3.5.9 而不是 3.4.2）照样命中 —— 规则不是打补丁。"""
        item = DG.leaf_items(_wbs(
            ("基坑", _wp("3.5", "监测", _leaf("3.5.9", "周边环境监测", 60, "监测工程")))))[0]
        assert DG.is_companion_task(item)[0] is True


# ==================== ② 改判为 SS 并留痕 ====================
class TestParallelize:
    """`parallelize_companion_deps` 的直接行为。"""

    def test_监测到主体的FS降级为SS并写dependency_note(self):
        wbs = _companion_wbs()
        deps = [_dep("3.4.2", "4.1.1.1"), _dep("3.2.3", "4.1.1.1")]
        new, changes, warns = DG.parallelize_companion_deps(deps, DG.leaf_items(wbs))

        assert _types(new)[("3.4.2", "4.1.1.1")] == "SS", new
        assert _types(new)[("3.2.3", "4.1.1.1")] == "FS", "真实顺序前置不许被改"
        assert changes == [{"predecessor": "3.4.2", "successor": "4.1.1.1",
                            "reason": "任务名含「监测」"}], changes
        assert warns == [], "紧随主体另有 FS 来路 → 不该报"

        note = _leaf_of(wbs, "3.4.2")["dependency_note"]
        assert "伴随型工序" in note and "并行" in note and "3.4.2→4.1.1.1" in note, note

    def test_只有伴随来路时留告警_绝不静默(self):
        """降级后主体工序没有任何 FS 来路 → 可能被 SS 放到开工第 1 天，必须报出来。"""
        wbs = _companion_wbs()
        new, changes, warns = DG.parallelize_companion_deps(
            [_dep("3.4.2", "4.1.1.1")], DG.leaf_items(wbs))

        assert _types(new)[("3.4.2", "4.1.1.1")] == "SS"
        assert len(warns) == 1 and "3.4.2→4.1.1.1" in warns[0]["detail"], warns

    def test_幂等_再跑一遍不再改也不重复写note(self):
        wbs = _companion_wbs()
        once, ch1, _ = DG.parallelize_companion_deps(
            [_dep("3.4.2", "4.1.1.1"), _dep("3.2.3", "4.1.1.1")], DG.leaf_items(wbs))
        twice, ch2, _ = DG.parallelize_companion_deps(once, DG.leaf_items(wbs))

        assert ch1 and not ch2, (ch1, ch2)
        assert once == twice
        assert _leaf_of(wbs, "3.4.2")["dependency_note"].count("伴随型工序") == 1

    def test_不修改入参的dep(self):
        wbs = _companion_wbs()
        deps = [_dep("3.4.2", "4.1.1.1")]
        before = json.dumps(deps, ensure_ascii=False, sort_keys=True)
        DG.parallelize_companion_deps(deps, DG.leaf_items(wbs))
        assert json.dumps(deps, ensure_ascii=False, sort_keys=True) == before

    def test_只改边型不改拓扑_不引入环(self):
        wbs = _companion_wbs()
        deps = [_dep("1.1.1", "3.4.2"), _dep("3.4.2", "4.1.1.1"), _dep("3.2.3", "4.1.1.1")]
        new, _, _ = DG.parallelize_companion_deps(deps, DG.leaf_items(wbs))

        assert set(_types(new)) == set(_types(deps)), "拓扑（端点集合）必须逐条保留"
        assert not DG.has_cycle(new, DG.collect_leaf_ids(wbs))


# ==================== ③ 不误伤 ====================
class TestNoCollateral:
    """改判只针对「伴随型 → **非豁免**后续工序」这一个方向。

    豁免名单 = `COMPANION_FS_EXEMPT_SUCCESSOR_KEYWORDS`（验收 / 检验批 / 资料 /
    移交 / 整改 / 竣工 / 手续），判据**只看任务名**（第 44 轮补：原先还看 `work_type`，
    实测让 `3.3.2 基坑验槽` 因 `work_type="验收"` 误豁免，60 天的基坑变形监测
    继续当它的 FS 门槛）。
    """

    def _run(self, deps, wbs=None):
        wbs = wbs or _companion_wbs()
        return DG.parallelize_companion_deps(deps, DG.leaf_items(wbs))

    def test_主体到伴随的FS保持不变(self):
        """主体先干完、养护/观测随后 —— 这是正常顺序，不许改。"""
        new, changes, _ = self._run([_dep("4.1.1.1", "3.4.2")])
        assert _types(new)[("4.1.1.1", "3.4.2")] == "FS"
        assert changes == []

    def test_伴随到伴随的FS改为SS(self):
        """⚠️ 期望值变更原因（第 44 轮补，用户 2026-09-21 实测）：

        旧口径把"伴随型 → 伴随型"排除在改判之外，理由是"监测→监测是正常顺序"。
        实测 `plan_run_1790001550` 正是踩在这个例外上：`1.2.6 基坑变形监测(30d) →
        1.2.7 周边环境监测(30d) → 1.2.8 沉降观测(30d)` 三条纯 FS 串链，把
        `1.3.1 材料采购` 顶到开工后第 160 天（2026-08-08）。

        这三条在工程上是**同一个监测周期内并行开展的不同监测项**（基坑变形 / 周边
        环境 / 沉降同步观测），不是"干完监测 A 再开始监测 B"。所以伴随型 → 伴随型
        也一并改判为 SS；**验收 / 检验批 / 资料 / 移交 / 整改 / 竣工 / 手续 类仍豁免**
        （见 `test_伴随到验收类的FS保持不变`，那条就是本规则的护栏）。
        """
        new, changes, _ = self._run([_dep("3.4.1", "3.4.2")])
        assert _types(new)[("3.4.1", "3.4.2")] == "SS"
        assert [(c["predecessor"], c["successor"]) for c in changes] == [("3.4.1", "3.4.2")]

    def test_伴随到验收类的FS保持不变(self):
        """实证：`plan_sample3_after_fix` 有 `3.4.1 监测 --FS--> 3.4.2 基坑支护专项验收`。
        专项验收本来就该等监测收尾，改成 SS 会让验收提前到开工当天 → 必须留 FS。"""
        wbs = _wbs(
            ("基坑",
             _wp("3.4", "监测", _leaf("3.4.1", "基坑变形监测", 60, "监测工程")),
             _wp("3.5", "验收", _leaf("3.5.1", "基坑支护专项验收", 1, "验收工程"))),
        )
        new, changes, _ = self._run([_dep("3.4.1", "3.5.1")], wbs)
        assert _types(new)[("3.4.1", "3.5.1")] == "FS"
        assert changes == []

    def test_已有的SS边不受影响(self):
        new, changes, _ = self._run([_dep("3.4.2", "4.1.1.1", "SS", 3)])
        assert _types(new)[("3.4.2", "4.1.1.1")] == "SS"
        assert new[0]["lag_days"] == 3, "lag 不许被抹掉"
        assert changes == []

    def test_普通主体边不受影响(self):
        new, changes, _ = self._run([_dep("3.2.3", "4.1.1.1"), _dep("1.1.1", "3.2.3")])
        assert set(_types(new).values()) == {"FS"}
        assert changes == []


# ==================== ④ 集成到 ensure_dependencies（唯一入口） ====================
class TestEnsureDependenciesIntegration:
    def test_主路径_降级与留痕并出告警(self):
        wbs = _companion_wbs()
        new, warns, applied = DG.ensure_dependencies(
            [_dep("3.4.2", "4.1.1.1"), _dep("3.2.3", "4.1.1.1")], wbs)

        assert _types(new)[("3.4.2", "4.1.1.1")] == "SS"
        assert "3.4.2" in _leaf_of(wbs, "3.4.2")["dependency_note"]
        blob = json.dumps(warns, ensure_ascii=False)
        assert "伴随型工序并行化" in blob and "3.4.2" in blob and "4.1.1.1" in blob, warns
        # `applied` 只记"补入的边"，不记改型 —— 否则"另兜底补入 N 条"的计数会失真
        assert all(a.get("direction") in ("pred", "succ") for a in applied), applied

    def test_没有任何原有边被杀掉(self):
        """本规则的实现选择是"改边型"而不是"删边"：原边端点集合必须逐条保留。"""
        wbs = _companion_wbs()
        deps = [_dep("1.1.1", "3.2.3"), _dep("3.4.2", "4.1.1.1"), _dep("3.2.3", "4.1.1.1")]
        new, _, _ = DG.ensure_dependencies(deps, wbs)
        assert set(_types(deps)) <= set(_types(new)), _types(new)

    def test_孤儿守卫不会把伴随型当主体来路(self):
        """守卫**自己**也不许再造一条「伴随型 → 主体」的顺序来路（否则 E1 会被它复活）。

        这里的 `3.2.3 基底钎探` 在输入里没有前置，守卫要给它补一条；候选阶梯里
        "同阶段前一工作包的收尾"正好是伴随型的 `3.4.2` —— 必须被跳过，退到
        "上一阶段的收尾"（`1.1.1`）。
        """
        wbs = _companion_wbs()
        new, _, applied = DG.ensure_dependencies(
            [_dep("3.4.2", "4.1.1.1"), _dep("3.2.3", "4.1.1.1")], wbs)

        preds_411 = {str(d["predecessor"]) for d in new if str(d["successor"]) == "4.1.1.1"}
        assert preds_411 == {"3.4.2", "3.2.3"}, preds_411
        assert [d for d in new
                if str(d["predecessor"]) == "3.4.2"
                and str(d.get("type") or "FS").upper() == "FS"] == [], \
            "伴随型工序不许再有任何 FS 后继（除非后继是验收/资料类）"
        preds_323 = {str(d["predecessor"]) for d in new if str(d["successor"]) == "3.2.3"}
        assert "3.4.2" not in preds_323, "守卫不许把伴随型当兜底来路：%s" % preds_323
        assert any(a["successor"] == "3.2.3" for a in applied), applied

    def test_守卫新造的边也会被收尾那道改型收拾(self):
        """候选阶梯第 1 条（同工作包前一条）不受 `_usable_as_fallback` 限制，
        所以守卫仍可能新造「伴随型 → 主体」的 FS 边 —— 收尾的第二道改型必须收拾它。"""
        wbs = _wbs(
            ("施工准备", _wp("1.1", "准备", _leaf("1.1.1", "场地平整", 5))),
            # 同一个工作包里，伴随型工序紧排在主体工序之前 → 守卫的候选阶梯第 1 条
            # 会把「3.5.1 基坑变形监测 → 3.5.2 基底钎探」补成 FS。
            # 注意 id 用 3.5.x 而不是 3.4.x：`3.4.` 在 `ORPHAN_EXEMPT_ID_PREFIXES` 里，
            # 3.4.x 会被判据豁免，守卫根本不会给它补前置（那样就测不到这条路径了）。
            ("基坑支护与土方", _wp("3.5", "监测",
                                   _leaf("3.5.1", "基坑变形监测", 60, "监测工程"),
                                   _leaf("3.5.2", "基底钎探", 3))),
        )
        new, _, applied = DG.ensure_dependencies([_dep("1.1.1", "3.5.1")], wbs)
        by_id = {it["id"]: it for it in DG.leaf_items(wbs)}

        assert any(a["successor"] == "3.5.2" and a["predecessor"] == "3.5.1"
                   for a in applied), "守卫确实按候选阶梯第 1 条新造了这条边：%s" % applied
        assert _types(new)[("3.5.1", "3.5.2")] == "SS", new
        for d in new:
            if str(d.get("type") or "FS").upper() != "FS":
                continue
            pred, succ = by_id.get(str(d["predecessor"])), by_id.get(str(d["successor"]))
            if pred is None or succ is None or not DG.is_companion_task(pred)[0]:
                continue
            assert DG.is_companion_task(succ)[0] or DG._companion_successor_exempt(succ), d

    def test_顺序链兜底路径也生效(self):
        """LLM 抛错 → 顺序链 = WBS 相邻序，仍可能把「伴随型 → 主体」排成 FS。"""
        wbs = _wbs(
            ("施工准备", _wp("1.1", "准备", _leaf("1.1.1", "场地平整", 5))),
            ("基坑支护与土方", _wp("3.4", "监测",
                                   _leaf("3.4.2", "周边环境监测", 60, "监测工程"))),
            ("地下室结构", _wp("4.1", "结构", _leaf("4.1.1.1", "1-0.5层 钢筋绑扎", 2))),
        )
        node = _silence(DG.DepsGenNode(llm=_NoLLM()))
        deps = node.run({"wbs": wbs})["dependencies"]["dependencies"]

        # 顺序链 = 1.1.1 → 3.4.2 → 4.1.1.1；中间那条是「伴随 → 主体」，必须改判
        assert _types(deps)[("3.4.2", "4.1.1.1")] == "SS", deps
        assert _types(deps)[("1.1.1", "3.4.2")] == "FS", "主体→伴随仍是正常顺序"
        assert not DG.has_cycle(deps, DG.collect_leaf_ids(wbs))


# ==================== ⑤ 第二条"非 LLM"路径：节拍搭接 ====================
class TestBeatPathCovered:
    def test_节拍搭接路径也生效(self):
        """`ctx["beat_deps"]`（代码产出的结构搭接）合并后同样要过规则。"""
        wbs = _wbs(
            ("主体结构", _wp("5.1", "节拍", _leaf("5.1.1", "1-1层 钢筋绑扎", 7),
                            _leaf("5.1.2", "1-1层 混凝土浇筑", 1))),
            ("基坑", _wp("3.4", "监测", _leaf("3.4.1", "基坑变形监测", 60, "监测工程"))),
        )
        ctx = {
            "wbs": wbs,
            "beat_deps": [_dep("3.4.1", "5.1.1")],          # 代码侧产出的搭接（FS）
            "beat_leaf_ids": ["3.4.1", "5.1.1", "5.1.2"],
        }
        node = _silence(DG.DepsGenNode(llm=_NoLLM()))
        out = node.run(ctx)
        deps = out["dependencies"]["dependencies"]

        assert _types(deps)[("3.4.1", "5.1.1")] == "SS", deps
        assert not DG.has_cycle(deps, DG.collect_leaf_ids(wbs))


# ==================== ⑥ 真计划验收 ====================
class TestRealPlan:
    """冻结计划的端到端验收（只读；本文件绝不写 plans/ 下任何东西）。"""

    def test_病征存在_且修复后关键路径起点不再是监测(self):
        wbs, deps0 = _plan(REAL_PLAN)
        assert _types(deps0)[("3.4.2", "4.1.1.1")] == "FS", "病征必须先在冻结计划里钉住"

        before = cpm.calculate_cpm(wbs, {"dependencies": deps0})
        assert before["critical_path"][0] == "3.4.2", "修复前关键路径起点就是监测"

        new, warns, _ = DG.ensure_dependencies(deps0, wbs)
        after = cpm.calculate_cpm(wbs, {"dependencies": new})

        assert _types(new)[("3.4.2", "4.1.1.1")] == "SS"
        assert "3.4.2" not in after["critical_path"], "监测不得再落在关键路径上"
        assert after["critical_path"][0] != "3.4.2", after["critical_path"][0]
        assert after["total_duration_days"] < before["total_duration_days"], (
            "理由不是「凑工期」，而是「监测不再违反施工常识地锁住主体」：%s → %s"
            % (before["total_duration_days"], after["total_duration_days"]))
        assert not DG.has_cycle(new, DG.collect_leaf_ids(wbs))
        assert "伴随型工序" in _leaf_of(wbs, "3.4.2")["dependency_note"]
        assert any("伴随型工序并行化" in w["message"] for w in warns), warns

    def test_全部冻结计划_处理后不再残留伴随型到主体的FS边(self):
        """可复用性：跑遍仓里所有冻结计划，规则都要成立（不是只修好某一份）。"""
        checked = 0
        for path in sorted(PLANS.glob("*.json")):
            wbs, deps0 = _plan(path)
            if not wbs.get("phases") or not deps0:
                continue
            checked += 1
            items = DG.leaf_items(wbs)
            by_id = {it["id"]: it for it in items}
            new, _, _ = DG.ensure_dependencies(deps0, wbs)
            for d in new:
                p, s = str(d["predecessor"]), str(d["successor"])
                if str(d.get("type") or "FS").upper() != "FS":
                    continue
                if p not in by_id or s not in by_id:
                    continue
                if not DG.is_companion_task(by_id[p])[0]:
                    continue
                assert DG.is_companion_task(by_id[s])[0] or \
                    DG._companion_successor_exempt(by_id[s]), \
                    "%s 残留伴随型→主体的 FS 前置：%s→%s" % (path.name, p, s)
            assert not DG.has_cycle(new, DG.collect_leaf_ids(wbs)), path.name
        assert checked >= 5, "至少要有几份冻结计划被跑到，checked=%s" % checked

    def test_伴随到验收的例外在真计划里确实存在且被保留(self):
        """`plan_sample3_after_fix.json` 有 `3.4.1 监测 --FS--> 3.4.2 基坑支护专项验收`。
        它证明"例外清单"不是空想；这条边必须原样保留 FS。"""
        path = PLANS / "plan_sample3_after_fix.json"
        wbs, deps0 = _plan(path)
        if ("3.4.1", "3.4.2") not in _types(deps0):
            return                       # 计划被替换/清理时优雅跳过，不误报
        new, _, _ = DG.ensure_dependencies(deps0, wbs)
        assert _types(new)[("3.4.1", "3.4.2")] == "FS"
