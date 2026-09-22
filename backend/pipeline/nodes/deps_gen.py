"""节点2b：依赖关系生成 — T-10

- LLM 按 prompts/deps_gen.txt 生成依赖；失败 → 顺序链兜底
- §5.5 强制：predecessor/successor 只允许三级叶子 ID；
  若出现父级 ID（如 1.1），展开为该父级下全部叶子并输出 _warning
- 环检测：Kahn 拓扑排序失败则丢弃 LLM 结果改用顺序链（保证 CPM 有解）
- **孤儿守卫（第 43 轮）**：收尾处逐条判断"这条叶子工程上该不该有前置"，
  该有没有的**按规则补一条合理前置**并留告警；补不出也留告警（绝不静默通过）。
- **伴随型工序并行化（契约 §8，E1）**：监测/观测/降水/养护/成品保护这类**与主体
  并行推进**的工序，不得当作后续主体工序的 FS 门槛；命中即把该条 FS 边降级为 SS
  并在伴随工序叶子上写 `dependency_note`（见 `parallelize_companion_deps`）。
"""

import json

from ..base import BaseNode
from ..llm import LLMClient, LLMError
from ..prompts_loader import load
from .docctx import combine
from .beat_node import merge_beat_deps


def collect_leaf_ids(wbs) -> list:
    leaves = []
    for phase in wbs.get("phases", []):
        for wp in phase.get("work_packages", []):
            for sub in wp.get("sub_packages", []):
                leaves.append(sub["id"])
    return leaves


def parent_map(wbs) -> dict:
    pm = {}
    for phase in wbs.get("phases", []):
        for wp in phase.get("work_packages", []):
            leaves = [sub["id"] for sub in wp.get("sub_packages", [])]
            pm[wp["id"]] = leaves
    return pm


def normalize_deps(deps, wbs) -> (list, list):
    """叶子化 + 父级展开 + 未知 id 过滤。返回 (deps, warnings)。"""
    leaves = set(collect_leaf_ids(wbs))
    pm = parent_map(wbs)
    out, warnings = [], []
    for dep in deps:
        pred = dep.get("predecessor")
        succ = dep.get("successor")
        if not pred or not succ or pred == succ:
            continue
        preds = pm.get(pred, [pred])
        succs = pm.get(succ, [succ])
        if len(preds) > 1 or len(succs) > 1:
            warnings.append(f"父级ID {pred or succ} 已展开为叶子依赖")
        for p in preds:
            for s in succs:
                if p not in leaves or s not in leaves:
                    warnings.append(f"未知任务ID已跳过：{p}→{s}")
                    continue
                out.append({
                    "predecessor": p, "successor": s,
                    "type": dep.get("type", "FS") if dep.get("type") in ("FS", "SS") else "FS",
                    "lag_days": int(dep.get("lag_days") or 0),
                })
    return out, warnings


def has_cycle(deps, leaves) -> bool:
    indeg = {l: 0 for l in leaves}
    adj = {l: [] for l in leaves}
    for d in deps:
        if d["predecessor"] in adj and d["successor"] in indeg:
            adj[d["predecessor"]].append(d["successor"])
            indeg[d["successor"]] += 1
    q = [l for l, d in indeg.items() if d == 0]
    cnt = 0
    while q:
        u = q.pop()
        cnt += 1
        for v in adj[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    return cnt != len(leaves)


def default_chain_deps(leaves) -> list:
    """顺序链兜底：leaves[i] → leaves[i+1]（FS）。保证无环、CPM 有解。"""
    return [{"predecessor": leaves[i], "successor": leaves[i + 1],
             "type": "FS", "lag_days": 0} for i in range(len(leaves) - 1)]


# ============================================================================
# 「该有前置」判据 + 白名单 + 兜底规则（第 43 轮）
# ============================================================================
# 缺陷实证（`backend/plans/plan_sample3_after_allfix.json`，310 叶子 / 447 依赖）：
#   **13 条叶子没有前置**，且全部落在开工头三天（2026-06-01 起）：
#     · 3.3.1 地下室周边回填  2026-06-01→06-02（工期字段 10 天）
#     · 3.3.2 回填土夯实      2026-06-03→06-04
#     · 8.2.1.1 外檐保温（全楼平行） 2026-06-01→06-06
#     · 8.3.1.1 外檐涂料（全楼平行） 2026-06-01→06-05   ← 与前两条同病，容易被漏掉
#     · 3.4.1/3.4.2 基坑变形监测 / 周边环境监测、1.x 施工准备 7 条
#   其中 5 条（3.3.1 / 3.3.2 / 3.4.1 / 8.2.1.1 / 8.3.1.1）**连后续也没有**，是彻底的
#   孤立节点；1.x 那 7 条虽有后续，但没有前置（阶段 1，合理）。
#   工程上不可能：地下室周边回填必须等**地下室结构收尾**，外檐保温/涂料必须在
#   主体/装饰阶段，绝不是开工当天。
#
# 为什么能悄悄通过（根因，非猜测）：
#   · prompts/deps_gen.txt 第 5 条明确允许「独立任务可以没有任何依赖」——模型可以
#     整条工序不写依赖；
#   · 本节点改动前只做「叶子化 + 环检测 + 有环回退顺序链」，**从不检查覆盖**；
#   · `ctx["deps_warnings"]` 在**全仓没有任何消费方**（grep 只有写入这一处），
#     所以连"未知任务ID已跳过/父级ID已展开"这类已有告警也从来没有落过盘。
#   实测旁证：9 份历史计划（plan_run_*、plan_sample3_*）里 `8.2.1.1` 的前置**全是空**
#   —— 跨 9 次不同 WBS/LLM 输出的稳定复现，说明它不只是"模型这次没写"。
# ============================================================================

#: ① 阶段名白名单：整段豁免（这些工序"从开工起就干活"是工程常态，报出来只会淹没真问题）。
ORPHAN_EXEMPT_PHASE_NAMES = ("施工准备",)
#: 阶段 1「施工准备」为什么整段豁免：场地平整 1.1.1 / 围挡搭设 1.1.2 / 测量控制网
#: 1.2.1 / 图纸会审 1.2.3 / 材料采购 1.3.1 / 机械进场 1.3.4 / 手续办理 1.4.1 /
#: 台风季措施 1.5.1 —— 它们本就是开工头几天的工作，**没有前置是对的**。

#: ② ID 前缀白名单（按叶子 id 判定，防"阶段被重命名/重排"后判据失守）。
ORPHAN_EXEMPT_ID_PREFIXES = ("1.", "3.4.")
#: `3.4.x`（基坑变形监测 3.4.1 / 周边环境监测 3.4.2）为什么豁免：这两条是与开挖
#: **同步开始、全程持续**的观测活动（60 天观测窗自开工当天起算），前置为空合理。
#: 注意：豁免的只是"没有前置算不算错"，**不是**"不许有前置"——模型/代码给了前置照样采纳。

#: ③ 名称关键词白名单：任何叫"…监测/观测"的工序都按"全程观测"看待。加这一条是为了
#: 不依赖编号这种易变的东西（WBS 换了生成口径，3.4.x 可能变成 3.5.x）。
ORPHAN_EXEMPT_NAME_KEYWORDS = ("监测", "观测")

#: ④ 回填类关键词：`回填 / 夯实 / 级配砂石 / 素土`。这类工序**必须等被回填的结构完成**，
#: 但 WBS 常把它编在「基坑支护与土方」（阶段 3）、排在阶段 4「地下室结构」**之前** ——
#: 于是只按"同阶段前一条/上一阶段收尾"推前置会推出**错的顺序**（回填排在结构之前）。
#: 所以单独成一条规则：前置取"它之后最近的结构类阶段"的**收尾叶子**。
BACKFILL_NAME_KEYWORDS = ("回填", "夯实", "级配砂石", "素土")
#: 结构类阶段关键词（回填前置的唯一来源；只取排在回填所在阶段**之后**的）。
STRUCTURE_PHASE_KEYWORDS = ("结构",)
#: 室外类阶段关键词：回填收尾的后续应接「室外工程」（回填后工序）。
OUTDOOR_PHASE_KEYWORDS = ("室外",)

#: 节拍叶子标记（`layer_engine._make_leaf` 写死 `_beat: True`）与外檐平行专项标记
#: （`layer_engine.expand_node` 给 `parallel_work` 的叶子写 `_parallel: True`）。
#: 为什么平行专项要单独成规则：`layer_engine.structural_deps` 的搭接循环只遍历真实
#: 分区 `range(1, len(zones)+1)`，而外檐平行叶子挂在 `z = len(zones)+pi`（本计划
#: 单分区 → z=2/3，即 8.2.1.1 / 8.3.1.1）—— 它们**结构上就拿不到任何搭接**；
#: 同时 `merge_beat_deps` 会把"两端都在节拍叶子上"的 LLM 边**删除**（节拍搭接由代码
#: 独占），于是模型即使写了也被删掉。两条叠加 = 外檐专项永远无前置。
BEAT_FLAG = "_beat"
PARALLEL_FLAG = "_parallel"


def leaf_items(wbs) -> list:
    """按 WBS 顺序摊平叶子，附阶段号/阶段名/所属工作包/包内序号/原始叶子。

    返回的是**新**的包装 dict（不改叶子本体，也不改 WBS）：
      {id, name, phase_no, phase_name, wp_id, wp_index, leaf_index, leaf}
    `phase_no` 是 1-based 的阶段位置（与叶子 id 前缀在本项目里一致，但**不假设**一致）。
    """
    items = []
    for pi, phase in enumerate((wbs or {}).get("phases") or [], 1):
        if not isinstance(phase, dict):
            continue
        for wi, wp in enumerate(phase.get("work_packages") or []):
            if not isinstance(wp, dict):
                continue
            for li, sub in enumerate(wp.get("sub_packages") or []):
                if not isinstance(sub, dict):
                    continue
                tid = sub.get("id") or sub.get("task_id")
                if not tid:
                    continue
                items.append({
                    "id": str(tid),
                    "name": str(sub.get("name") or sub.get("task_name") or tid),
                    "phase_no": pi,
                    "phase_name": str(phase.get("phase") or ""),
                    "wp_id": str(wp.get("id") or ""),
                    "wp_index": wi,
                    "leaf_index": li,
                    "leaf": sub,
                })
    return items


def should_have_predecessor(item) -> (bool, str):
    """这条叶子"工程上应当有前置"吗？返回 (应当, 理由)。

    判据（顺序即优先级）：
      1) 阶段名在白名单里 → 否（施工准备整段从开工起干）；
      2) id 前缀在白名单里 → 否（1.x 准备 / 3.4.x 监测）；
      3) 名称含白名单关键词 → 否（任何"…监测/观测"都是全程观测活动）；
      4) 阶段号 > 1 → **是**（第 1 阶段之外，任何工序都该有来路）；
      5) 阶段号 <= 1（且不在白名单）→ 否（开工第一条，没前置是正常的）。
    """
    if item.get("phase_name") in ORPHAN_EXEMPT_PHASE_NAMES:
        return False, "施工准备阶段（本就占开工头几天）"
    tid = item.get("id") or ""
    for prefix in ORPHAN_EXEMPT_ID_PREFIXES:
        if tid.startswith(prefix):
            return False, "白名单前缀 %s（准备/监测类，从开工起就干活）" % prefix
    name = item.get("name") or ""
    for kw in ORPHAN_EXEMPT_NAME_KEYWORDS:
        if kw in name:
            return False, "名称含「%s」（全程观测活动）" % kw
    if int(item.get("phase_no") or 1) > 1:
        return True, "阶段 %s（>1）的工序必须有来路" % item.get("phase_no")
    return False, "第 1 阶段第一条工序"


def _is_parallel(item) -> bool:
    return bool((item.get("leaf") or {}).get(PARALLEL_FLAG))


def _is_beat(item) -> bool:
    """节拍叶子（`layer_engine._make_leaf` / `expand_node` 写的 `_beat: True`）。

    节拍叶子之间的搭接由 `layer_engine.structural_deps` **代码独占**生成（与
    `beat_node.merge_beat_deps` 同一口径）——孤儿守卫不许再按"工作包先后"给它插一手，
    否则会把"Ⅱ 区独立起点"改成"Ⅱ 区排在 Ⅰ 区之后"（见 `_candidate_predecessors` 规则 4）。
    """
    return bool((item.get("leaf") or {}).get(BEAT_FLAG))


def _is_backfill(item) -> bool:
    name = item.get("name") or ""
    return any(kw in name for kw in BACKFILL_NAME_KEYWORDS)


def _reaches(succs, start, target) -> bool:
    """从 start 沿"后继"能否走到 target（用于判新边会不会成环）。"""
    if start == target:
        return True
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for nxt in succs.get(cur, ()):  # noqa: B905 - 与仓内风格一致，显式取默认值
            if nxt == target:
                return True
            if nxt not in seen:
                stack.append(nxt)
    return False


def _structure_tail(items, phase_no):
    """回填前置：它**之后**最近的结构类阶段的收尾叶子（如阶段 4 地下室结构 → 4.1.4.3）。"""
    for it in sorted(items, key=lambda x: x["phase_no"]):
        if it["phase_no"] <= phase_no:
            continue
        if any(kw in it["phase_name"] for kw in STRUCTURE_PHASE_KEYWORDS):
            return _tail_of_phase(items, it["phase_no"])
    return None


def _tail_of_phase(items, phase_no):
    same = [it for it in items if it["phase_no"] == phase_no]
    return same[-1] if same else None


def _prev_beat_phase_tail(items, phase_no):
    """外檐平行专项的前置：**前一个节拍阶段**的收尾叶子。

    为什么用"前一个节拍阶段"而不是"上一个阶段"：外檐保温/涂料挂在「装饰装修」节点
    （节点 8）的 `parallel_work` 里，而节点 8 自己的跨相搭接写的是 `lead_in.from_node=6`
    （二次结构与砌体，见 beat_configs.BASE_BEAT_CONFIGS）—— 外檐专项与内装同属这个
    节点，前置口径必须与节点自身的搭接一致，否则会出现"内装等二次结构、外檐却在开工
    当天"的自相矛盾。机电安装（阶段 7）不是节拍阶段，外檐**不该**等它（会白等半年）。
    """
    candidates = [it for it in items
                  if it["phase_no"] < phase_no
                  and (it.get("leaf") or {}).get(BEAT_FLAG)]
    if not candidates:
        return None
    prev_phase = max(it["phase_no"] for it in candidates)
    return _tail_of_phase(items, prev_phase)


def _usable_as_fallback(cand, item) -> bool:
    """跨工作包/跨阶段的兜底候选能不能用（回填类 + 伴随型叶子要特判）。

    为什么必须特判：回填类工序**本身**要等"它之后的结构类阶段"收尾（见 BACKFILL_* 的
    说明），所以它在 WBS 里虽然编在前面的阶段，**实际发生得很晚**。若把它当成"上一阶段
    的收尾"挂到结构类任务的来路上，顺序会彻底倒置 —— 实测（本节点的合成用例）：
    `4.1.1.1 地下室结构首条钢筋绑扎` 被挂到 `3.3.2 回填土夯实` 之后，等于"先回填、
    再施工地下室"，比没有依赖更糟。
    规则：回填类叶子只能给"回填类自己"或"室外类阶段"的任务当兜底前置。

    伴随型（契约 §8 / E1）同理，而且后果一样严重：兜底候选如果选中一条「监测/养护/
    降水/成品保护」工序，就等于**守卫自己把 E1 又造了一遍**（监测锁主体）。
    规则：伴随型叶子只能给"伴随型自己"或"验收/资料类收尾任务"当兜底前置。
    注意这只挡 2)~5) 的跨包/跨阶段候选；1) "同工作包内的前一条工序"是同包真实先后，
    不受此限 —— 那种情况由 `ensure_dependencies` 收尾处的第二道改型兜底。
    """
    if not _is_backfill(cand):
        return not is_companion_task(cand)[0] or is_companion_task(item)[0] \
            or _companion_successor_exempt(item)
    if _is_backfill(item):
        return True
    return any(kw in item["phase_name"] for kw in OUTDOOR_PHASE_KEYWORDS)


def _candidate_predecessors(item, items, by_wp, by_phase):
    """按优先级给出候选前置（第一个可用的就是它）。返回 [(前置item, 理由)]。

    规则阶梯（前者优先，越靠前越"局部"、越可靠）：
      1) 同工作包内的**前一条叶子** —— 同一工作包的工序先后是最硬的局部信号
         （3.3.2 回填土夯实 ← 3.3.1 地下室周边回填）；
      2) 外檐平行专项（`_parallel`）：
         a) 同阶段内**前一个平行工作包**的收尾叶子（外檐涂料 ← 外檐保温）；
         b) 前一节拍阶段的收尾叶子（外檐保温 ← 二次结构与砌体收尾）；
      3) 回填类：之后最近的结构类阶段的收尾叶子（地下室周边回填 ← 地下室结构收尾）；
      4) 同阶段内前一工作包的收尾叶子 —— ⚠️ **节拍叶子（`_beat`）不适用**（E5-b / G2）：
         节拍阶段的"工作包 = 一个平面分区"，把 Ⅱ 区首段首工序挂到 Ⅰ 区收尾上，就等于把
         "Ⅱ 区独立起点"（`layer_engine.structural_deps` 的设计意图）悄悄改成"Ⅱ 区排在
         Ⅰ 区之后"。实测真计划 `plan_run_1789911477.json` 的 3 条跳区边
         （`5.1.18.5→5.2.1.1` / `6.1.18.4→6.2.1.1` / `8.1.6.4→8.2.1.1`）就是本规则在
         `merge_beat_deps` **之后**补出来的孤儿边（不在节拍搭接的入参里，那里删不到）。
         节拍叶子之间的搭接由 `layer_engine.structural_deps` 独占生成，本规则不许插一手。
      5) 上一阶段的收尾叶子（跨阶段衔接）。
    2)~5) 都是"跨工作包/跨阶段"的推断，过 `_usable_as_fallback`（回填类叶子不许被
    当成通用来路）；1) 是同包内的真实工序先后，不受该限制。
    """
    out = []
    wp = by_wp.get(item["wp_id"]) or []
    if item["leaf_index"] > 0:
        out.append((wp[item["leaf_index"] - 1], "同一工作包内的前一条工序"))

    def _add(cand, why):
        if cand is not None and _usable_as_fallback(cand, item):
            out.append((cand, why))

    if _is_parallel(item):
        # 2a：同阶段里排在本工作包之前的平行工作包（把外檐涂料挂到外檐保温之后）
        phase_items = by_phase.get(item["phase_no"]) or []
        for other in reversed(phase_items):
            if other["wp_index"] >= item["wp_index"] or other["id"] == item["id"]:
                continue
            if _is_parallel(other):
                _add(_tail_of_phase(
                    [x for x in phase_items if x["wp_index"] == other["wp_index"]],
                    item["phase_no"]), "前一个外檐平行工序的收尾（保温→涂料）")
                break
        # 2b：前一节拍阶段的收尾
        _add(_prev_beat_phase_tail(items, item["phase_no"]),
             "外檐平行专项：等前一节拍阶段收尾")

    if _is_backfill(item) or _is_parallel(item):
        _add(_structure_tail(items, item["phase_no"]), "回填/外墙类：等结构类阶段收尾")

    # 4：同阶段内前一工作包的收尾叶子 —— **节拍叶子跳过**（E5-b / G2，见函数 docstring）
    if not _is_beat(item):
        phase_items = by_phase.get(item["phase_no"]) or []
        for other in reversed(phase_items):
            if other["wp_index"] < item["wp_index"]:
                _add(_tail_of_phase(
                    [x for x in phase_items if x["wp_index"] == other["wp_index"]],
                    item["phase_no"]), "同阶段前一工作包的收尾")
                break

    # 5：上一阶段的收尾叶子
    _add(_tail_of_phase(items, item["phase_no"] - 1), "上一阶段的收尾")

    return [(cand, why) for cand, why in out if cand is not None and cand["id"] != item["id"]]


def _wire_backfill_successors(deps, items, succs, has_succ):
    """把"回填类收尾叶子"接到**室外工程**阶段的首条叶子上（回填后工序）。

    只有完全没有后续的回填类叶子才接（WP 中间的叶子本来就有后继）。返回补入的边。
    """
    added = []
    outdoor = [it for it in items
               if any(kw in it["phase_name"] for kw in OUTDOOR_PHASE_KEYWORDS)]
    if not outdoor:
        return added
    for item in items:
        if not _is_backfill(item) or item["id"] in has_succ:
            continue
        target = outdoor[0]
        if target["id"] == item["id"] or _reaches(succs, target["id"], item["id"]):
            continue        # 会成环 / 自己指自己 → 不接
        deps.append({"predecessor": item["id"], "successor": target["id"],
                     "type": "FS", "lag_days": 0})
        added.append((item, target, "回填收尾 → 室外工程首条工序"))
        succs.setdefault(item["id"], set()).add(target["id"])
        has_succ.add(item["id"])
    return added


# ============================================================================
# 伴随型工序并行化（契约 §8 / E1）：监测、观测、降水、养护、成品保护
# ============================================================================
# 缺陷实证（`backend/plans/plan_run_1789895021.json`，304 叶子 / 447 依赖）：
#   `3.4.2 周边环境监测`（1 人 60 天）被写成 `3.4.2 --FS--> 4.1.1.1`（1-0.5 层
#   钢筋绑扎）—— 即"主体结构第一根钢筋，必须等 60 天环境监测全部做完"。
#   后果有两层：
#     · 把主体结构开工整体推后 60 天（该计划 4.1.1.1 起算被锁到 2027-01-01 之后）；
#     · 监测自己（无前置、开工当天起算）成了**关键路径起点**（关键路径 69 条、663 天）。
#   同一个洞在 8 份历史计划里稳定复现（`plan_run_1789567958` 3.4.2→4.1.1.1、
#   `plan_sample3_after_org_v2` / `_after_allfix` 同边）——不是"模型这次写错了"。
#
# 根因：`prompts/deps_gen.txt` 只教模型"写出工序先后"，没有任何一条告诉它
#   "伴随型工序不构成顺序门槛"。而本节点改动前对 FS/SS 只做叶子化与环检测，
#   **从不审边的语义**，于是 LLM 把"监测要在结构之前"（时间上重叠）理解成了
#   FS（必须做完才能开始）。
#
# 为什么选 SS 而不是删边（契约 §8 要求二选一并说明理由）：
#   ① **图拓扑不变**：SS 与原 FS 是同一条边、同一方向，不新增任何可达性，
#      因此**不可能引来新的环**（删边则会把"伴随与主体相关"这条信息整个丢掉）；
#   ② **语义更准**：监测/降水/养护/成品保护与主体是"同时开始、各自推进"的搭接，
#      SS（始-始）正是这个意思；FS 才是错的；
#   ③ **关键路径自动脱钩**：`scheduler.longest_chain` 明确 `dep["type"] == "SS"` 跳过
#      （scheduler.py:1313），伴随型工序不可能再被当成关键路径的起点；
#      `cpm.py` 的 SS 反向推算同样会给它算出大时差。
#
# 与孤儿守卫（第 43 轮）的分工：本规则**只改边型、不删边、不补边**，
#   所以不会与孤儿守卫抢活 —— 降级后主体工序的来路仍在 `preds` 里，
#   `should_have_predecessor` 不会再去补一条（它自己要的那条是"顺序来路"，
#   由别的真实前置承担；实测 8 份计划里没有一条主体工序只靠伴随型工序当前置）。
# ============================================================================

#: 伴随型工序关键词（契约 §8 的用户清单）。判据是"任务名 或 工种(work_type)"，
#: 写成可复用规则，**不针对任何单个 task_id**（WBS 换编号口径也照样命中）。
#: 与 `ORPHAN_EXEMPT_NAME_KEYWORDS`（监测/观测）刻意重叠但**含义不同**：
#: 那份白名单说的是"没有前置不算错"（开工即起算），这里说的是"它不能当别人的门槛"。
COMPANION_KEYWORDS = ("监测", "观测", "降水", "养护", "成品保护")

#: 例外：后续若是**收尾/资料类**而非主体工序，保留 FS 是对的。
#: 实证：`plan_sample3_after_fix.json` 有 `3.4.1 基坑变形及水位监测 --FS--> 3.4.2
#: 基坑支护专项验收` —— 专项验收本来就该等监测收尾，改成 SS 会让验收提前到开工当天。
COMPANION_FS_EXEMPT_SUCCESSOR_KEYWORDS = ("验收", "检验批", "资料", "移交", "整改", "竣工", "手续")

#: 手续/报批类任务（第 44 轮）：本来就该在开工前后跑，与现场机械安装毫无关系。
#: 实证：`plan_run_1790001550` 的 `1.4.1 施工许可证办理` 排在 `1.3.7 机械调试` 之后。
PERMIT_KEYWORDS = ("许可证", "施工许可", "报建", "报审", "报批", "备案",
                   "登记", "审批")

#: 出现在**前置**里 = "先装机械、再办手续"的逻辑倒置。
MACHINERY_KEYWORDS = ("塔吊", "施工电梯", "提升机", "机械", "设备安装", "安装", "调试")

#: 手续类任务重挂锚点：同相位内**最后一条**技术准备类任务。判据是任务名。
TECH_PREP_KEYWORDS = ("图纸会审", "方案", "交底", "图纸", "审查", "策划")


def is_companion_task(item) -> (bool, str):
    """这条叶子是"伴随型工序"吗？返回 (是否, 理由)。

    判据（任一命中即算）：
      1) 任务名含 `COMPANION_KEYWORDS`（如「周边环境监测」「绿化养护（初期）」）；
      2) 工种 `work_type` 含 `COMPANION_KEYWORDS`（如「监测工程」「养护工程」）。
    `item` 是 `leaf_items()` 的包装 dict（含 `name` 与 `leaf`）。
    """
    name = str(item.get("name") or "")
    for kw in COMPANION_KEYWORDS:
        if kw in name:
            return True, "任务名含「%s」" % kw
    work_type = str((item.get("leaf") or {}).get("work_type") or "")
    for kw in COMPANION_KEYWORDS:
        if kw in work_type:
            return True, "工种「%s」含「%s」" % (work_type, kw)
    return False, ""


def _companion_successor_exempt(succ_item) -> bool:
    """后续是收尾/资料类工序 → 该 FS 边保留（见例外清单的注释）。

    第 44 轮修正：判据**只看任务名，不再看 work_type**。
    缺陷实证（`plan_run_1790001550`）：`3.3.1 基坑变形监测`（60 天）--FS-->
    `3.3.2 基坑验槽`，本应降级为 SS；但 `3.3.2` 的 `work_type == "验收"` 命中了豁免
    名单，60 天观测窗于是卡住验槽、进而卡住整条 `4.x 主体` 脊线。
    `work_type` 是**粗粒度工种**（"验收"覆盖验槽/隐蔽验收/分项验收全部），
    而注释里那条正当例外（`3.4.2 基坑支护专项验收`）**本来就在任务名里带「验收」**，
    所以收窄到 name：既保住正当例外，又放掉 work_type 造成的误豁免。
    """
    name = str(succ_item.get("name") or "")
    return any(kw in name for kw in COMPANION_FS_EXEMPT_SUCCESSOR_KEYWORDS)


def _companion_note(why, targets):
    """伴随型工序叶子上的 `dependency_note`（给人看的可读说明）。"""
    return ("伴随型工序（%s）：与主体工序**并行推进**，不作为后续主体工序的 FS 前置 —— "
            "主体开工不受本工序完工约束（不然 60 天观测窗会把主体锁住）。"
            "已将 %d 条 FS 边降级为 SS（始-始并行）：%s。"
            % (why, len(targets), "、".join(targets)))


def parallelize_companion_deps(deps, items):
    """伴随型工序 → 后续主体工序的 FS 边降级为 SS，并写叶子留痕。

    返回 `(deps, changes, warnings)`：
      - `deps`：改型后的依赖列表（**原地改边型，不删边**）；
      - `changes`：`[{"predecessor","successor","reason"}]`，供节点留痕/测试断言；
      - `warnings`：告警载荷列表（`{"message","detail"}`）。只在"降级后该主体工序
        再没有任何 FS 来路"时报一条 —— 那是 SS 会把它放到开工第 1 天的情形，
        按本文件的规矩**绝不静默**（实测 8 份历史计划均为 0 条，属防御性留痕）。

    副作用（刻意）：在伴随工序的**叶子本体**上写 `leaf["dependency_note"]`
    （契约 §8 明确要求；`leaf_items()` 的包装 dict 持有的就是 WBS 里的叶子对象，
    所以这里改的是 `ctx["wbs"]` 里那份，会随 WBS 一起进产物，供人核对）。
    """
    if not deps or not items:
        return list(deps or []), [], []

    by_id = {it["id"]: it for it in items}
    companion_cache = {}

    def _companion(tid, item):
        if tid not in companion_cache:
            companion_cache[tid] = is_companion_task(item)
        return companion_cache[tid]

    out, changes = [], []
    for d in deps:
        dep = d
        pred_id, succ_id = str(d.get("predecessor")), str(d.get("successor"))
        if str(d.get("type") or "FS").upper() == "FS" and pred_id != succ_id:
            pred_item, succ_item = by_id.get(pred_id), by_id.get(succ_id)
            if pred_item is not None and succ_item is not None:
                pred_is_comp, why = _companion(pred_id, pred_item)
                succ_is_comp, _ = _companion(succ_id, succ_item)
                # 第 44 轮：去掉 `not succ_is_comp` —— 伴随型→伴随型（监测→监测→
                # 沉降观测）同样是"并行推进"，原来不降级，于是把 `1.2.6→1.2.7→1.2.8`
                # 三条 30 天串成 90 天，再把 `1.3.1 材料采购` 顶到开工后第 160 天。
                if (pred_is_comp
                        and not _companion_successor_exempt(succ_item)):
                    dep = dict(d)
                    dep["type"] = "SS"          # FS → SS：同一条边、同一方向（不可能新增环）
                    changes.append({"predecessor": pred_id, "successor": succ_id,
                                    "reason": why})
        out.append(dep)

    if not changes:
        return out, [], []

    # 叶子留痕：按伴随工序聚合（它可能同时被升级为多条 SS）。
    by_pred = {}
    for ch in changes:
        by_pred.setdefault(ch["predecessor"], []).append(ch)
    for pred_id, chs in by_pred.items():
        item = by_id[pred_id]
        leaf = item.get("leaf")
        if isinstance(leaf, dict):
            note = _companion_note(chs[0]["reason"],
                                   ["%s→%s" % (c["predecessor"], c["successor"]) for c in chs])
            if leaf.get("dependency_note"):
                note = "%s；%s" % (leaf["dependency_note"], note)
            leaf["dependency_note"] = note

    # 防御性留痕：降级后没有任何 FS 来路的主体工序 → 可能被 SS 放到开工第 1 天。
    fs_preds = set()
    for d in out:
        if str(d.get("type") or "FS").upper() == "FS":
            fs_preds.add(str(d.get("successor")))
    stranded = [c for c in changes if c["successor"] not in fs_preds]
    warnings = []
    if stranded:
        warnings.append({
            "message": ("伴随型工序并行化：%d 条主体工序降级后已无 FS 来路，"
                        "可能被排到开工第 1 天 —— 请人工核对是否需补一条真实前置"
                        % len(stranded)),
            "detail": "；".join("%s→%s（%s）" % (c["predecessor"], c["successor"], c["reason"])
                                for c in stranded),
        })
    return out, changes, warnings


def frontload_permit_deps(deps, items):
    """手续/报批类任务前置纠偏（第 44 轮）。

    缺陷实证（`plan_run_1790001550`）：`1.4.1 施工许可证办理`（15 天）的 FS 前置是
    `1.3.7 机械调试` —— 于是"许可证"排在「塔吊安装 / 施工电梯安装 / 机械调试」**之后**，
    2026-10-17 才受理，而这份计划 2026-03-01 就"开工"了。这一条串行把后续
    `2.1.1 桩基检测 → 3.x 基坑 → 4.x 主体` 整条脊线一起推迟 149 天。

    处理规则（**只改边型/端点，不删任务，绝不静默**）：
      · 命中时把该 FS 边的 predecessor 改挂到**同相位内最后一条技术准备类任务**
        （图纸会审 / 方案编制 / 技术交底）；
      · 同相位内没有可用锚点、或改挂会成环 → **保留原边并照常留痕**
        （宁可留一条可疑的机械前置，也不摘边造出新孤儿 —— 本函数跑在孤儿守卫
        **之后**，摘掉的边不会被守卫补回来）；
      · 每条改动都进 `changes` 留痕：

    返回 `(deps, changes)`；`changes` 元素 = `{"predecessor","successor",
    "new_predecessor"}`，`new_predecessor is None` 表示"没找到锚点、原边保留未改"。
    """
    if not deps or not items:
        return list(deps or []), []

    by_id = {it["id"]: it for it in items}

    #: 每个相位内"最后一条"技术准备类任务（靠后的覆盖靠前的）。
    anchor = {}
    for it in items:
        if any(kw in str(it.get("name") or "") for kw in TECH_PREP_KEYWORDS):
            anchor[it["phase_no"]] = it["id"]

    def _is_permit(it):
        return any(kw in str(it.get("name") or "") for kw in PERMIT_KEYWORDS)

    def _is_machinery(it):
        name = str(it.get("name") or "")
        work_type = str((it.get("leaf") or {}).get("work_type") or "")
        return any(kw in name or kw in work_type for kw in MACHINERY_KEYWORDS)

    # 可达表按"原始边"建：改挂只会引入环、不会消除环，用它挡掉会成环的改挂。
    succs = {}
    for d in deps:
        succs.setdefault(str(d.get("predecessor")), set()).add(str(d.get("successor")))

    out, changes = [], []
    for d in deps:
        dep = d
        if str(d.get("type") or "FS").upper() == "FS":
            pid, sid = str(d.get("predecessor")), str(d.get("successor"))
            pred_item, succ_item = by_id.get(pid), by_id.get(sid)
            if (pred_item is not None and succ_item is not None
                    and _is_permit(succ_item) and _is_machinery(pred_item)):
                new_pred = anchor.get(succ_item["phase_no"])
                if (new_pred and new_pred != sid and new_pred != pid
                        and not _reaches(succs, sid, new_pred)):
                    dep = dict(d)
                    dep["predecessor"] = new_pred
                    changes.append({"predecessor": pid, "successor": sid,
                                    "new_predecessor": new_pred})
                else:
                    # 没有可用锚点 / 改挂会成环 → **保留原边**。宁可留着这条可疑的
                    # 机械前置（它至少不会把任务甩到开工第 1 天），也不摘边造出新孤儿：
                    # 本函数跑在孤儿守卫**之后**，摘掉的边不会被守卫补回来。
                    changes.append({"predecessor": pid, "successor": sid,
                                    "new_predecessor": None})
        out.append(dep)
    return out, changes


#: 机电相位识别：相位名命中即视为"多专业并行"相位（各专业有独立系统与作业面）。
MEP_PHASE_NAME_KEYWORDS = ("机电", "安装", "管道", "管线", "设备", "暖通", "消防", "电气")


def parallelize_specialty_streams(deps, items):
    """机电相位「专业流并行化」（第 44 轮）——本轮收益最大的一条。

    缺陷实证（`plan_run_1790001550`）：相位「机电安装」的 25 条叶子被串成**一条单链**
    `7.1.1 → 7.1.2 → 7.1.3 → 7.1.4 → 7.2.1 → … → 7.5.5 → 7.6.1`
    （全图 283 条依赖里只有 1 条 SS，其余 282 条全是 FS、lag 全 0）。
    可电气 / 给排水 / 暖通 / 消防是**四个独立系统、独立作业面**，现场并行施工。
    串成单链后机电段 = 1035 天 = 全项目 1607 天的 **64%**；按专业拆成 4 条并行支链后
    机电段只剩 330 天，总工期 1607 → 949 天。

    规则（数据驱动：判据是相位名 + 叶子的 `work_type`，不针对任何 task_id）：
      1) 只处理**相位名命中** `MEP_PHASE_NAME_KEYWORDS` 的相位；
      2) 按叶子 `work_type` 分组成若干**专业流**；形状不符就**原样返回、绝不动手**
         （护栏见下），宁可漏修也不误拆；
      3) 流内**保持 WBS 原有先后顺序**逐条串好（专业内的工序先后完全不变）；
      4) 每条流的**入口**统一挂到该相位入口任务的外部前置（= 相位整体开工点），
         流与流之间不再互相等待；
      5) 每条流的**出口**若在相位外没有后继，接到该相位原出口的外部后继上。

    护栏（任一不满足即整相位跳过，保证"宁可不动手"）：
      · 相位名不命中；或相位内有叶子没有 `work_type`；
      · 分组后少于 2 条流；或**存在只有 1 条叶子的流**（那是"落单工序"，
        重新挂入口会把它甩到相位开工当天，语义错）；
      · 相位第一条叶子不在任何一条流的第一位（说明这份 WBS 的相位结构不是
        "专业流"形状，不能套用本规则）；
      · 相位入口任务没有任何外部前置（没有可挂的开工点）。

    返回 `(deps, changes)`；`changes` = `[{"phase","streams","removed","added"}]`。
    """
    if not deps or not items:
        return list(deps or []), []

    by_phase = {}
    for it in items:
        by_phase.setdefault(it["phase_no"], []).append(it)

    out, changes = list(deps), []

    for pno, leaves in by_phase.items():
        phase_name = str(leaves[0].get("phase_name") or "")
        if not any(kw in phase_name for kw in MEP_PHASE_NAME_KEYWORDS):
            continue

        types = [str((it.get("leaf") or {}).get("work_type") or "").strip() for it in leaves]
        if any(not t for t in types):
            continue

        streams = {}
        for it, t in zip(leaves, types):
            streams.setdefault(t, []).append(it)
        if len(streams) < 2 or any(len(g) < 2 for g in streams.values()):
            continue

        phase_ids = {it["id"] for it in leaves}
        entry_id = leaves[0]["id"]
        entry_stream = next((g for g in streams.values() if g[0]["id"] == entry_id), None)
        if entry_stream is None:
            continue
        ext_in = sorted({str(d["predecessor"]) for d in out
                         if str(d["successor"]) == entry_id
                         and str(d["predecessor"]) not in phase_ids})
        if not ext_in:
            continue
        ext_out = sorted({str(d["successor"]) for d in out
                          if str(d["predecessor"]) in phase_ids
                          and str(d["successor"]) not in phase_ids})

        kept, removed = [], []
        for d in out:
            p, s = str(d.get("predecessor")), str(d.get("successor"))
            if p in phase_ids and s in phase_ids:
                removed.append((p, s))
                continue
            kept.append(d)

        added = []
        for _t, group in streams.items():
            group = sorted(group, key=lambda it: (it["wp_index"], it["leaf_index"]))
            for i in range(len(group) - 1):
                kept.append({"predecessor": group[i]["id"], "successor": group[i + 1]["id"],
                             "type": "FS", "lag_days": 0})
                added.append((group[i]["id"], group[i + 1]["id"]))
            first, last = group[0]["id"], group[-1]["id"]
            if not any(str(d["predecessor"]) not in phase_ids
                       and str(d["successor"]) == first for d in kept):
                for p in ext_in:
                    kept.append({"predecessor": p, "successor": first,
                                 "type": "FS", "lag_days": 0})
                    added.append((p, first))
            if ext_out and not any(str(d["predecessor"]) == last
                                   and str(d["successor"]) not in phase_ids for d in kept):
                for s in ext_out:
                    kept.append({"predecessor": last, "successor": s,
                                 "type": "FS", "lag_days": 0})
                    added.append((last, s))

        out = kept
        changes.append({"phase": phase_name, "streams": len(streams),
                        "removed": removed, "added": added})
    return out, changes


#: 验收相位识别：相位名命中即视为"按验收阶段收口"的相位（隐蔽 → 分项 → 分部 → 调试 → 竣工）。
ACCEPTANCE_PHASE_NAME_KEYWORDS = ("验收", "竣工", "移交")

#: 同一验收阶段内允许并行的最小条数。**≤2 条一律不拆**：`竣工预验收 → 竣工验收`、
#: `单机试运转 → 系统联合调试` 这类成对工序天生有先后，拆开就是错；3 条及以上才是
#: "按工种/系统分头验收"的同级工序（钢筋 / 模板 / 混凝土 / 砌筑 / 抹灰 …）。
ACCEPTANCE_PARALLEL_MIN = 3


def parallelize_acceptance_stages(deps, items):
    """验收相位「阶段并行化」（第 44 轮补，用户 2026-09-21 点名的第二个大问题）。

    缺陷实证（`plan_run_1790001550`）：相位「竣工验收」的 18 条叶子被串成**一条单链**
    `10.1.1 → 10.1.2 → … → 10.5.1 → 10.5.2`（18 条 FS、合计 116 天）。而其中
    「隐蔽验收 3 条 / 分项验收 5 条 / 分部验收 6 条」都是**同级的、按工种分头**的验收，
    现场分头验收、并行推进，不该一条等一条；真正有先后的只有
    `单机试运转 → 系统联合调试` 与 `竣工预验收 → 竣工验收` 这两对。

    规则（数据驱动：判据是相位名 + 叶子 `work_type`，不针对任何 task_id）：
      1) 只处理**相位名命中** `ACCEPTANCE_PHASE_NAME_KEYWORDS` 的相位；
      2) 按叶子 `work_type` 分组 = 若干**验收阶段**，阶段先后沿用 WBS 首次出现顺序；
      3) 阶段内条数 ≥ `ACCEPTANCE_PARALLEL_MIN` → 阶段内**并行**（同阶段互不等待）；
         条数 ≤ 2 → **保持 WBS 原有先后顺序串行**（成对工序天生有先后）；
      4) **阶段之间**仍严格先后：第 k+1 阶段**每条**叶子都 FS 挂在第 k 阶段**全部**
         叶子上（= 上一阶段整体验完才进下一阶段）；
      5) 相位入口的外部前置挂到第一阶段**每条**叶子；相位出口的外部后继接在最后
         阶段**每条**叶子上。

    护栏（任一不满足即整相位跳过，保证"宁可不动手"）：
      · 相位名不命中；或相位内有叶子没有 `work_type`；
      · 分组后少于 2 个阶段；或**没有任何阶段**达到 `ACCEPTANCE_PARALLEL_MIN`
        （拆了也一天不省，白改图）；
      · 相位入口任务没有任何外部前置（没有可挂的开工点）。

    返回 `(deps, changes)`；`changes` = `[{"phase","stages","removed","added"}]`。
    """
    if not deps or not items:
        return list(deps or []), []

    by_phase = {}
    for it in items:
        by_phase.setdefault(it["phase_no"], []).append(it)

    out, changes = list(deps), []

    for pno, leaves in by_phase.items():
        phase_name = str(leaves[0].get("phase_name") or "")
        if not any(kw in phase_name for kw in ACCEPTANCE_PHASE_NAME_KEYWORDS):
            continue

        types = [str((it.get("leaf") or {}).get("work_type") or "").strip() for it in leaves]
        if any(not t for t in types):
            continue

        # 阶段 = work_type；组内按 WBS 顺序，阶段先后 = WBS 首次出现顺序（dict 保序）。
        stages = {}
        for it in sorted(leaves, key=lambda x: (x["wp_index"], x["leaf_index"])):
            stages.setdefault(str((it.get("leaf") or {}).get("work_type")).strip(),
                              []).append(it)
        if len(stages) < 2 or not any(len(g) >= ACCEPTANCE_PARALLEL_MIN
                                      for g in stages.values()):
            continue

        phase_ids = {it["id"] for it in leaves}
        entry_id = leaves[0]["id"]
        ext_in = sorted({str(d["predecessor"]) for d in out
                         if str(d["successor"]) == entry_id
                         and str(d["predecessor"]) not in phase_ids})
        if not ext_in:
            continue
        ext_out = sorted({str(d["successor"]) for d in out
                          if str(d["predecessor"]) in phase_ids
                          and str(d["successor"]) not in phase_ids})

        kept, removed = [], []
        for d in out:
            p, s = str(d.get("predecessor")), str(d.get("successor"))
            if p in phase_ids and s in phase_ids:
                removed.append((p, s))
                continue
            kept.append(d)

        added = []
        order = list(stages.values())
        # ---- 阶段内：够条的并行（一条边不留），不够条的保持原顺序串行 ----
        for group in order:
            if len(group) >= ACCEPTANCE_PARALLEL_MIN:
                continue
            for i in range(len(group) - 1):
                kept.append({"predecessor": group[i]["id"], "successor": group[i + 1]["id"],
                             "type": "FS", "lag_days": 0})
                added.append((group[i]["id"], group[i + 1]["id"]))
        # ---- 阶段之间：上一阶段**全部**叶子 → 下一阶段**每条**叶子 ----
        for prev_stage, next_stage in zip(order, order[1:]):
            for prev in prev_stage:
                for nxt in next_stage:
                    kept.append({"predecessor": prev["id"], "successor": nxt["id"],
                                 "type": "FS", "lag_days": 0})
                    added.append((prev["id"], nxt["id"]))
        # ---- 相位入口 / 出口 ----
        for it in order[0]:
            for p in ext_in:
                kept.append({"predecessor": p, "successor": it["id"],
                             "type": "FS", "lag_days": 0})
                added.append((p, it["id"]))
        for it in order[-1]:
            for s in ext_out:
                kept.append({"predecessor": it["id"], "successor": s,
                             "type": "FS", "lag_days": 0})
                added.append((it["id"], s))

        out = kept
        changes.append({"phase": phase_name, "stages": len(stages),
                        "removed": removed, "added": added})
    return out, changes


def ensure_dependencies(deps, wbs):
    """孤儿守卫 + 伴随型工序并行化：把依赖图修成"能交给 CPM 的图"，并返回告警。

    返回 `(deps, warnings, applied)`：
      - `deps`：处理后的依赖列表（原列表的副本，不改入参的 dep dict）；
      - `warnings`：告警载荷列表（`{"node","message","detail"}`，供节点 `emit("warning")`）；
      - `applied`：补入的边 `[{"successor","predecessor","reason"}, ...]`（供测试/追溯）。
        **只记"补入的边"**；伴随型工序的 FS→SS 改型走 `warnings` 与该叶子的
        `dependency_note` 留痕（它没有新增/删除任何边，塞进这里会让
        "另兜底补入 N 条缺失依赖"的计数失真）。

    两条职责（都在这里做，不另起一套生成器）：
      A. **伴随型工序并行化**（契约 §8）：见 `parallelize_companion_deps`；
      B. **孤儿守卫**（第 43 轮）：给"该有前置却没有"的叶子按规则补前置。

    四条硬约束：
      ① **只补该补的**：判据见 `should_have_predecessor`（阶段 >1 且不在白名单）；
      ② **绝不成环**：每条候选边补入前先做可达性检查（`_reaches`）；
         A 的 FS→SS 改型**不改变图拓扑**（同边同向），故不可能引入环；
      ③ **绝不静默**：补了要报（让人核对），补不出更要报（列出缺失任务 ID），
         端点不存在的边被丢弃也要报（正常路径上 `normalize_deps` 已经报过一次，
         这里只是"守卫自己也可能被单独调用"的第二道留痕）；
      ④ **A 在 B 之前**：先修对了边型，守卫才是在"最终图"上判断谁缺前置。
    """
    items = leaf_items(wbs)
    if not items:
        return list(deps or []), [], []

    raw = [dict(d) for d in (deps or [])]
    ids = set(it["id"] for it in items)
    out = [d for d in raw
           if str(d.get("predecessor")) in ids and str(d.get("successor")) in ids]
    dropped = [d for d in raw if d not in out]

    by_wp, by_phase = {}, {}
    for it in items:
        by_wp.setdefault(it["wp_id"], []).append(it)
        by_phase.setdefault(it["phase_no"], []).append(it)

    # ---- 伴随型工序并行化（契约 §8 / E1）----
    # 放在孤儿守卫**之前**：先把错误的 FS 门槛降级为 SS，再让守卫按"最终图"判断
    # 谁还缺前置 —— 顺序反过来会让守卫把"伴随型工序"当成合法来路而漏补。
    out, companion_changes, companion_warnings = parallelize_companion_deps(out, items)

    preds = {}
    succs = {}
    for d in out:
        p, s = str(d["predecessor"]), str(d["successor"])
        preds.setdefault(s, set()).add(p)
        succs.setdefault(p, set()).add(s)

    applied, unresolved, filled = [], [], []
    for item in items:
        need, why = should_have_predecessor(item)
        if not need or preds.get(item["id"]):
            continue
        chosen = None
        for cand, reason in _candidate_predecessors(item, items, by_wp, by_phase):
            cid = cand["id"]
            if cid in preds.get(item["id"], ()):
                continue
            if _reaches(succs, item["id"], cid):
                continue                       # 会成为环 → 换下一个候选
            chosen = (cand, reason)
            break
        if chosen is None:
            unresolved.append(item)
            continue
        cand, reason = chosen
        out.append({"predecessor": cand["id"], "successor": item["id"],
                    "type": "FS", "lag_days": 0})
        preds.setdefault(item["id"], set()).add(cand["id"])
        succs.setdefault(cand["id"], set()).add(item["id"])
        applied.append({"successor": item["id"], "predecessor": cand["id"],
                        "reason": reason, "direction": "pred"})
        filled.append((item, cand, reason))

    # 回填收尾 → 室外工程（回填后工序）。单独报：它是"补后续"，不是"补前置"。
    has_succ = set(succs)
    wired = _wire_backfill_successors(out, items, succs, has_succ)
    for item, target, reason in wired:
        applied.append({"successor": target["id"], "predecessor": item["id"],
                        "reason": reason, "direction": "succ"})

    # ---- 伴随型工序并行化 · 第二道（收尾）----
    # 为什么还要跑一次：孤儿守卫可能**新造**出「伴随型 → 主体」的 FS 边
    #（候选阶梯第 1 条"同工作包内的前一条工序"不受 `_usable_as_fallback` 限制）。
    # 本函数是幂等的（已改过的边是 SS，不会再命中），所以第二道只收拾守卫新造的边。
    out, late_changes, late_warnings = parallelize_companion_deps(out, items)
    companion_changes = list(companion_changes) + list(late_changes)
    companion_warnings = list(companion_warnings) + list(late_warnings)

    # ---- 机电相位专业流并行化（第 44 轮，本轮收益最大）----
    # 放在孤儿守卫之后：先让守卫把来路补全，再按 work_type 重排专业流。
    out, stream_changes = parallelize_specialty_streams(out, items)

    # ---- 验收相位阶段并行化（第 44 轮补）----
    # 与机电相位**互不重叠**（判据是相位名，机电命中"安装"、验收命中"验收/竣工"），
    # 所以放在专业流之后即可，不存在两条规则抢同一批边的情况。
    out, accept_changes = parallelize_acceptance_stages(out, items)

    # ---- 手续/报批类任务前置纠偏（第 44 轮）----
    # 放在孤儿守卫**之后**：顺序反过来会让守卫把刚摘掉的机械前置当成"缺前置"补回来。
    out, permit_changes = frontload_permit_deps(out, items)

    warnings = []
    if dropped:
        detail = "、".join("%s→%s" % (d.get("predecessor"), d.get("successor"))
                          for d in dropped[:20])
        warnings.append({
            "node": "deps",
            "message": ("依赖端点不存在，%d 条边已丢弃（任务 ID 不在 WBS 里）" % len(dropped)),
            "detail": detail,
        })
    if filled:
        detail = "；".join("%s（%s）← 前置 %s（%s）" % (it["id"], it["name"], cand["id"], why)
                          for it, cand, why in filled)
        warnings.append({
            "node": "deps",
            "message": ("工序依赖兜底：%d 条任务原本没有任何前置，会被排到开工第 1 天，"
                        "已按施工逻辑补上前置 —— 请核对是否与现场一致"
                        % len(filled)),
            "detail": detail,
        })
    if wired:
        detail = "；".join("%s（%s）→ 后续 %s（%s）" % (it["id"], it["name"], tgt["id"], why)
                          for it, tgt, why in wired)
        warnings.append({
            "node": "deps",
            "message": ("工序依赖兜底：%d 条回填类收尾任务原本没有任何后续，已接到室外"
                        "工程首条工序 —— 请核对是否与现场一致" % len(wired)),
            "detail": detail,
        })
    if unresolved:
        detail = "、".join("%s（%s，阶段 %s）" % (it["id"], it["name"], it["phase_no"])
                          for it in unresolved)
        warnings.append({
            "node": "deps",
            "message": ("工序依赖缺口：%d 条任务工程上应当有前置，却一条都没补上 —— "
                        "它们会被排到开工第 1 天，请人工补齐 dependencies" % len(unresolved)),
            "detail": "缺前置的任务：%s" % detail,
        })
    if companion_changes:
        names = {it["id"]: it["name"] for it in items}
        detail = "；".join("%s（%s）→ %s（%s）已改为 SS 并行"
                           % (c["predecessor"], names.get(c["predecessor"], ""),
                              c["successor"], names.get(c["successor"], ""))
                           for c in companion_changes)
        warnings.append({
            "node": "deps",
            "message": ("伴随型工序并行化：%d 条「监测/观测/降水/养护/成品保护」类工序原来被当成"
                        "后续主体工序的 FS 前置（会把主体开工锁到该工序完工），已改判为 SS 并行，"
                        "并写入该工序的 dependency_note —— 请核对是否与现场一致"
                        % len(companion_changes)),
            "detail": detail,
        })
    for sc in stream_changes:
        warnings.append({
            "node": "deps",
            "message": ("机电相位专业流并行化：相位「%s」原来被串成一条单链（%d 个专业顺序等"
                        "前一个专业收尾），已按 work_type 拆成 %d 条并行专业流，"
                        "各流入口统一挂到相位开工点 —— 请核对是否与现场一致"
                        % (sc["phase"], len(sc["removed"]), sc["streams"])),
            "detail": ("删除相位内串行边 %d 条：%s；重建专业流内顺序 + 入口/出口边 %d 条"
                       % (len(sc["removed"]),
                          "、".join("%s→%s" % (p, s) for p, s in sc["removed"]),
                          len(sc["added"]))),
        })
    for ac in accept_changes:
        warnings.append({
            "node": "deps",
            "message": ("验收相位阶段并行化：相位「%s」原来被串成一条单链（隐蔽验收 → 分项验收 → "
                        "分部验收 → 系统调试 → 竣工验收 逐条等前一条收尾），已按 work_type 归并成"
                        " %d 个验收阶段 —— 阶段内 ≥%d 条的并行推进、≤2 条的保持原先后顺序，"
                        "阶段之间仍整体先后（上一阶段全部验完才进下一阶段）"
                        % (ac["phase"], ac["stages"], ACCEPTANCE_PARALLEL_MIN)),
            "detail": ("删除相位内串行边 %d 条：%s；重建阶段内顺序 + 阶段间/入口/出口边 %d 条"
                       % (len(ac["removed"]),
                          "、".join("%s→%s" % (p, s) for p, s in ac["removed"]),
                          len(ac["added"]))),
        })
    if permit_changes:
        names = {it["id"]: it["name"] for it in items}
        detail = "；".join(
            "%s（%s）→ %s（%s）：前置改挂为 %s" % (
                c["predecessor"], names.get(c["predecessor"], ""),
                c["successor"], names.get(c["successor"], ""),
                ("%s（%s）" % (c["new_predecessor"], names.get(c["new_predecessor"], "")))
                if c["new_predecessor"] else "【未找到可用锚点，原边保留待人工核对】")
            for c in permit_changes)
        warnings.append({
            "node": "deps",
            "message": ("手续/报批类工序前置纠偏：%d 条「许可证/报审/登记/备案」类任务原来被"
                        "机械安装或调试类工序串行前置（等于「先装塔吊、再办许可证」），"
                        "已改挂到同相位技术准备之后 —— 请核对是否与现场一致"
                        % len(permit_changes)),
            "detail": detail,
        })
    for payload in companion_warnings:
        payload.setdefault("node", "deps")
    warnings.extend(companion_warnings)
    return out, warnings, applied


class DepsGenNode(BaseNode):
    name = "deps"
    title = "工序依赖关系"

    def __init__(self, llm=None):
        super().__init__()
        self.llm = llm or LLMClient()

    def run(self, ctx):
        wbs = ctx.get("wbs") or {}
        leaves = collect_leaf_ids(wbs)
        self.emit("node_progress", {"node": self.name, "progress": 30,
                                    "message": "交给模型判断工序先后"})
        deps, warnings = [], []
        raw = None
        try:
            raw = self.llm.chat_json(load("deps_gen.txt"),
                                     combine(ctx, json.dumps(wbs, ensure_ascii=False)), temperature=0.3)
            deps, warnings = normalize_deps(raw.get("dependencies", []), wbs)
            if has_cycle(deps, leaves):
                warnings.append("LLM 依赖存在环，已回退顺序链")
                deps = default_chain_deps(leaves)
        except Exception:
            warnings.append("LLM 不可用，已使用顺序链兜底")
            deps = default_chain_deps(leaves)

        # 并入节拍引擎结构搭接（节拍叶子搭接由代码独占；有环回退顺序链）
        beat_deps = ctx.get("beat_deps") or []
        if beat_deps:
            deps = merge_beat_deps(deps, beat_deps, ctx.get("beat_leaf_ids") or [], leaves)

        # ---- 孤儿守卫（第 43 轮）：该有前置的必须有，且必须有说法 ----
        # 放在所有来源（LLM / 顺序链 / 节拍搭接）之后：只有到这里才知道"最终有没有前置"。
        deps, orphan_warnings, applied = ensure_dependencies(deps, wbs)
        if has_cycle(deps, leaves):
            # 兜底补边理论上已逐条做过可达性检查；万一仍成环（畸形输入），
            # 宁可整棵回退顺序链也不交给 CPM 一个无解图 —— 并留一条告警。
            orphan_warnings.append({
                "node": self.name,
                "message": "依赖兜底补入后出现环，已回退整棵顺序链（请人工检查 dependencies）",
                "detail": ("补入的边：%s" % "；".join(
                    "%s←%s" % (a["successor"], a["predecessor"]) for a in applied))
                if applied else "本次没有补入任何边（环来自既有依赖，不是兜底造成的）",
            })
            deps = default_chain_deps(leaves)
            # 顺序链就是 WBS 相邻序，**照样可能**把「伴随型 → 主体」排成 FS
            # （实证：`plan_run_1789895021` 的 3.4.2 紧邻 4.1.1.1）。这条兜底路径
            # 在 `ensure_dependencies` **之后**才替换整棵依赖，必须再过一遍规则，
            # 否则"整棵回退顺序链"会把 E1 重新引入（契约 §8 不能只覆盖主路径）。
            deps, _chain_changes, _chain_warns = parallelize_companion_deps(
                deps, leaf_items(wbs))
            for _payload in _chain_warns:
                _payload.setdefault("node", self.name)
                orphan_warnings.append(_payload)
        for payload in orphan_warnings:
            payload.setdefault("node", self.name)
            self.emit("warning", payload)

        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "工序先后理清了，共 %d 条" % len(deps)})
        ctx["deps_warnings"] = warnings
        # 告警原文另存一份结构化键（ctx["deps_warnings"] 全仓没有消费方，见上方说明）：
        # 交付/追溯要的是"补了哪几条、依据是什么"。
        ctx["deps_orphan_repairs"] = list(applied)
        self.done_summary = ("工序先后关系已排定：%d 条" % len(deps)
                             + ("（含 %d 条提示）" % len(warnings) if warnings else "")
                             + ("（另兜底补入 %d 条缺失依赖）" % len(applied) if applied else ""))
        return {"dependencies": {"dependencies": deps}}
