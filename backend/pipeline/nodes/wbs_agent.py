"""WBS 多级分工节点 —— 代码骨架 + 逐相 LLM + 跨相融合 + 复评人工门

取代原「单一 LLM 一次产整棵」的 wbs_worker 链路：
  A 代码骨架   ：default_phases() 生成近乎必备的 1级 实体阶段（含特化 hint + KB 键）
                —— 就是"前 10 个字工作包"，专项不再独立成相
  C 逐相展开   ：每个 1级 由一个 LLM 独立展开 2/3级（注入 doc_summary + 该相 KB 活动），
              规避「一个 LLM 上下文塞不下整棵树」；单相失败按模板合并回退
  D 组装       ：合并所有相 → 全局重编号 + normalize_wbs；全失败 → 项目模板全量兜底
  G 跨相融合   ：主体 LLM（wbs_fusion.txt）在整棵树展开后开放 2/3级 修改权限：
              把专项措施归入宿主实体阶段、标记跨相重复、保证衔接（不新增 1级）
  E 复评       ：复用 wbs_review.txt 全局评审 + 脚本自检证据
  F 人工门     ：HIGH → Y 通行；自由文本 → overview LLM 拆成「目标1级+修改意见」→
              只重跑目标相 → 重组装复评（≤ 上限轮），超限强制放行

复用：kb.py（in-process KB）、wbs_gen 的 normalize_wbs/load_template/_pick_template、
docctx.combine、原 _human_gate（EV_NODE_PAUSED）。
"""

import copy
import json
import uuid

from .. import config, kb
from ..base import BaseNode
from ..engine import PipelineCancelled
from ..events import EV_NODE_PAUSED
from ..llm import LLMClient, LLMError
from ..prompts_loader import load
from ..registry import GATE_TIMEOUT_SECONDS
from .docctx import combine
from .kb_conformance import (check_scope_conformance, gate_issues as scope_gate_issues,
                             format_note as scope_format_note)
from .wbs_gen import normalize_wbs, load_template, _pick_template
from .wbs_phases import (build_phase_kb_injection, default_phases,
                         host_phase_map, resolve_prompt)
from .beat_configs import BEAT_PHASE_NAMES, resolve_beat_phase_prompt
# §D1 的 WBS 树取数只此一份实现（audit_gate 是本轮 §D 契约的归属文件）：
# R1 结构审计门与这里的复评人工门给用户的必须是**同一棵**树。
from .audit_gate import wbs_tree_payload

# 复评人工门重试上限
MAX_REVIEW_ROUNDS = 3
MAX_RETRY_ON_HUMAN = 2

# 正则量级参考（每平方米总建筑面积）
CONC_M3_PER_M2 = (0.20, 0.35)
REBAR_KG_PER_M2 = (40.0, 70.0)

# 「节拍型阶段尚未展开」的自证说明（进自检证据 + 进评审提示词，两处必须同一句话）。
# 背景：第 32 轮起 `beat_build` 已移到本节点**上游**（builder.py），生产路径上不再
# 走到这里；但本节点可以被单独调用（单测 / 探针 / 老调用方），那时节拍相里仍是
# `_beat_placeholder_phase()` 的占位叶子（`_beat_placeholder=True`，量=1/单位=项），
# `conc_m3/rebar_t` 必然是极小值或 0。这不是"工程量缺失"，是**本门看不到**。
# 早先这份"0"被当成缺失证据喂给评审模型 → 模型写出「混凝土缺失超 93%」的假 HIGH，
# 用户被白白拦下（用户截图里最扎眼的那条）。所以局限必须显式写进证据，
# `prompts/wbs_review.txt` 里也明确禁止据此报 HIGH。
BEAT_NOT_EXPANDED_NOTE = (
    "节拍型阶段尚未展开（节拍分段在本节点属于下游步骤），本阶段的 conc_m3/rebar_t "
    "不代表最终工程量，不得据此判定工程量缺失；工程量完整性请留到下游的【R1】审计门"
    "（那里节拍已按层铺开）")

# ==================== 一键修复（选择题）====================
#
# 为什么做在**这个**门（wbs_agent 的复评人工门），而不是 R1 审计门：
#   · 那 4 条 HIGH `issues` 由本文件的 `_review_loop` → `_review()` 产生、经
#     `_human_gate()`（本文件 :446 附近）以 node_paused 发出 —— R1 审计门
#     （audit_gate.py 的 WBSAuditNode）压根没有 `issues` 字段；
#   · 引擎 `_reenter()` 只能重跑"当前" pause_point，没有回到上游重跑的能力；
#   · 真正能重做 WBS 的机制**只在本节点内部**：`_expand_phase(retry_req=...)`
#     与 `_fuse_cross_phase(user_note=...)`。
# 所以"选了就真的重做"只能落在本门。R1 保持"看 WBS 树 + Y 确认"的职责不变。

REPAIR_REEXPAND = "reexpand_wbs"        # 按参数重做受影响相的 WBS 展开
REPAIR_FILL_TYPES = "fill_missing_types"  # 补知识库里缺失的必含工程类型

# 五维评审里**从 issues 文本就能确定性判定**"本架构下无法自动修"的类别：
# 都指向「节拍型阶段的工程量还没铺」——那要等下游 BeatExpandNode 才发生，
# 本门拿不到，任何"重做"都改不动它。宁可少一个选项，也不许假修。
_NO_AUTO_FIX_HINTS = (
    ("conc_m3", "混凝土用量对不上（脚本自检 conc_m3 与项目参数 total_concrete 不同量级）"),
    ("concrete", "混凝土工程量总量对不上项目参数"),
    ("rebar_t", "钢筋总量与项目参数不同量级"),
    ("占地", "工程量口径与项目参数不同量级"),
    ("占位", "节拍型阶段此刻还只是占位子树"),
    ("按层", "主体未按标准层展开"),
    ("层数对不上", "层数与项目参数不符"),
    ("单层", "疑似只统计了单层"),
    ("仅含", "节拍型阶段此刻还只是占位子树"),
)

# REQUIRED 的 L3 里，代码骨架 10 个 1级 相**确实没有任何相拥有**的那几个。
# 这些不是"重做一遍就会好"，而是骨架缺键（真缺陷）→ 补齐是唯一出路。
_EXTRA_PHASE_KB = {
    "施工准备": (["material_transport"],
                 "材料运输与加工工程（TRANS_NEW_046 混凝土运输 / TRANS_NEW_068 砂运输 / "
                 "TRANS_NEW_103 钢筋运输 等，按本工程实际材料各列一条足量工序）"),
    "竣工验收": (["demobilization"],
                 "退场与恢复（DEMOB_AI_001 临设拆除 / DEMOB_AI_002 机械退场 / "
                 "DEMOB_AI_003 场地恢复）"),
}


def _issue_text(issue):
    it = issue if isinstance(issue, dict) else {}
    return "%s %s %s" % (it.get("dimension") or "", it.get("finding") or "",
                         it.get("suggestion") or "")


def issue_needs_user_info(issue):
    """这条问题在本架构下**能不能**被自动修？不能 → 返回"要问用户什么"，能 → None。

    只按 issues 文本做确定性判定（不调 LLM、不猜）：
      · 命中 `_NO_AUTO_FIX_HINTS`（工程量/层数类，根因是节拍工程量在本门还没铺）
        → 无法自动修，返回具体要问的问题；
      · 其余（dimension=覆盖/工期/结构，或文本里是缺类型/缺工序/工期脱钩）
        → 有本节点内部的重做机制可走，返回 None。
    """
    it = issue if isinstance(issue, dict) else {}
    dim = str(it.get("dimension") or "")
    text = _issue_text(it)
    for key, what in _NO_AUTO_FIX_HINTS:
        if key in text:
            # 措辞三原则（第 32 轮，用户实测原话：「这些是什么意思，作为一个第一次
            # 使用的用户，根本看不懂」「把所有话语都应该精简化，通俗化，不要刻意
            # 使用一些英文和专业术语」）：
            #   ① 一句说清"我们看到了什么"，不解释内部节点名；
            #   ② 不用 `**` 加粗（终端不渲染，会露出两颗星）、不用 ①②③ 编号
            #      （门上的编号已经被"选项编号"占用了）；
            #   ③ 不再让用户在"数据库基线口径 / 你的项目口径"之间做技术选择 ——
            #      直接给出**默认动作**，用户只要不满意就说一句。
            return ("这条需要你提供信息：%s。\n"
                    "    系统在本门还不能替你改它：这一阶段的工程量要等后面的"
                    "「节拍流水分段」按层铺开才算得准，本门看到的只是一行占位。\n"
                    "    你可以不回答，直接回车/选继续：系统会按数据库里的基线用量"
                    "（各部位占比、单层量）在后面的步骤补足并算给你看。\n"
                    "    如果补出来的量不对，再把这句改成你的项目口径（只写一句就行，"
                    "例如「地下室外墙混凝土按 3200 立方」）。" % what)
    if dim in ("覆盖", "工期", "结构"):
        return None
    if any(w in text for w in ("缺", "漏", "未展开", "脱钩")):
        return None
    # 判不出来的一律当作"不会自动修"——宁可少一个选项，也不许假修
    return ("这条需要你提供信息：%s。系统判不出可自动修的改法，"
            "请直接说明你的修改要求（选 {FREE} 或直接输入意见）。"
            % (str(it.get("finding") or "（未给出描述）")[:60]))


def _leaf_kb_ids(wbs):
    """树里所有叶子显式挂靠的 kb_activity_id 集合。"""
    out = set()
    for ph in (wbs.get("phases") or []):
        for wp in (ph.get("work_packages") or []):
            for leaf in (wp.get("sub_packages") or []):
                if isinstance(leaf, dict) and leaf.get("kb_activity_id"):
                    out.add(str(leaf["kb_activity_id"]))
    return out


# ---------- 必含工程类型：判据三态 + 「校验到底跑成了没有」 ----------
# L3 类型名去掉通用尾巴后当关键词（「材料运输与加工工程」→「材料运输」）。
_L3_NAME_SUFFIXES = ("与加工工程", "工程")
# 只有验收/检测类节点**不能**当成"该分项存在"的证据。实测踩过：全树唯一带"防水"的
# 叶子是「防水隐蔽验收」（挂在 hidden_accept 下），而防水施工工序一条都没有 ——
# 若把验收节点算作"防水工程存在"，就会把真缺口放过去。
_INSPECTION_WORDS = ("验收", "检测", "试验", "隐蔽", "资料", "移交", "试运转", "调试", "复核", "见证")
_KIND_LABEL = {
    "missing": "真缺：树里没有任何相关工序",
    "inspection_only": "只挂了验收/检测类节点，没有施工工序",
    "unanchored": "树里有相关工序，但没挂数据库工序编号",
}


def _leaf_rows(wbs):
    """树里 3级 叶子的 (name, kb_activity_id) 列表（判据只看叶子）。"""
    out = []
    for ph in (wbs.get("phases") or []):
        for wp in (ph.get("work_packages") or []):
            for leaf in (wp.get("sub_packages") or []):
                if isinstance(leaf, dict):
                    out.append((str(leaf.get("name") or ""),
                                str(leaf.get("kb_activity_id") or "")))
    return out


def _l3_keywords(l3_id, l3_name):
    """该 L3 的匹配关键词 = 类型名去尾巴 + 名下所有 L4 活动名（都要求 ≥2 字）。"""
    kws = set()
    nm = str(l3_name or "")
    for suf in _L3_NAME_SUFFIXES:
        if nm.endswith(suf) and len(nm) > len(suf):
            nm = nm[: -len(suf)]
            break
    if len(nm) >= 2:
        kws.add(nm)
    for aid in _ids_of_l3(l3_id):
        try:
            an = str((kb.activity_info(aid) or {}).get("activity_name") or "")
        except Exception:                      # noqa: BLE001
            an = ""
        if len(an) >= 2:
            kws.add(an)
    return kws


def _classify_missing(l3_id, l3_name, leaves):
    """把"这一类型没锚定到"细分成三种确定性情形（不调 LLM、不猜）。

    为什么必须分：三种情形的修法完全不同 ——
      · `missing`         树里压根没有 → 补工序；
      · `inspection_only` 只有验收/检测节点 → 补**施工**工序（验收节点不能当证据）；
      · `unanchored`      有工序但没挂编号 → 补 kb_activity_id。
    只报一个"缺"字，用户既不知道是哪种，也不知道该补什么。
    """
    kws = _l3_keywords(l3_id, l3_name)
    hits = [nm for nm, _aid in leaves if any(k in nm for k in kws)]
    if not hits:
        return "missing", []
    if all(any(w in nm for w in _INSPECTION_WORDS) for nm in hits):
        return "inspection_only", hits[:4]
    return "unanchored", hits[:4]


def kb_essentials_report(wbs, params):
    """必含工程类型校验：**把「校验没跑成」与「真的不缺」分开**。

    判定只用两份数据，不猜：
      · 知识库 `Building_Type_L3_Mapping` 里 REQUIRED（必含）的 L3 清单；
      · 当前树里每个叶子显式挂靠的 `kb_activity_id`（`l3_of_activity` 反查其 L3）；
      · 再加一层名字证据，把"缺"细分成真缺 / 只挂验收节点 / 没挂编号。

    返回 {"checked": bool, "reason": str, "required": int, "anchored": int, "missing": [...]}，
    每条 missing = {"l3","name","phase","keys","kind","kind_label","evidence","activities"}。

    `checked=False` = 这项校验**没能执行**（取不到建筑类型 / 知识库认不出 / 不可用）。
    此时 `missing` 为空**不代表不缺** —— 实测踩过：`building_type=None` 的那份计划
    读数恒为"缺 0 类"，与"真的不缺"在界面上完全无法区分，用户以为没问题。
    调用方必须把 `reason` 说出来（见 `_repair_options` / `_repair_stats` / `_repair_gate`）。
    """
    bt = str(params.get("building_type") or params.get("structure_type") or "")
    if not bt:
        return {"checked": False, "reason": "项目参数里没有建筑类型（building_type）",
                "required": 0, "anchored": 0, "missing": []}
    try:
        r = kb.resolve_building_type(bt)
        why = "建筑类型「%s」在知识库里认不出来" % bt
    except Exception as exc:                   # noqa: BLE001
        r = None
        why = "知识库查询建筑类型失败（%s）" % exc.__class__.__name__
    if not r:
        return {"checked": False, "reason": why, "required": 0, "anchored": 0, "missing": []}
    try:
        req = [x for x in kb.l3_for(r[0]) if x.get("applicability_level") == "REQUIRED"]
    except Exception as exc:                   # noqa: BLE001
        return {"checked": False, "reason": "知识库不可用（%s）" % exc.__class__.__name__,
                "required": 0, "anchored": 0, "missing": []}
    if not req:
        return {"checked": False,
                "reason": "知识库里「%s」没有标 REQUIRED 的必含工程类型" % (r[1],),
                "required": 0, "anchored": 0, "missing": []}
    present = set()
    for aid in _leaf_kb_ids(wbs):
        try:
            l3 = kb.l3_of_activity(aid)
        except Exception:
            l3 = None
        if l3:
            present.add(str(l3))
    # activity_id → 相名 的宿主索引。分两张表：
    #   · skeleton_of：代码骨架**本来就有的** kb 键（相的分内之事，不需要动 spec）；
    #   · owner_of   ：骨架键 ∪ 本轮补的键（用来给缺失类型找宿主相）。
    skeleton_of, owner_of = {}, {}
    for spec in default_phases():
        for k in list(spec.get("kb") or []):
            try:
                for a in kb.l4_for(k) or []:
                    skeleton_of.setdefault(str(a.get("activity_id")), spec["phase"])
            except Exception:
                continue
        extra = list(_EXTRA_PHASE_KB.get(spec["phase"], ([], ""))[0])
        for k in list(spec.get("kb") or []) + extra:
            try:
                for a in kb.l4_for(k) or []:
                    owner_of.setdefault(str(a.get("activity_id")), spec["phase"])
            except Exception:
                continue

    leaves = _leaf_rows(wbs)
    out = []
    for x in req:
        lid = str(x.get("work_type_id") or "")
        name = str(x.get("work_type_name") or lid)
        if not lid or lid in present:
            continue
        owner = None
        for aid in _ids_of_l3(lid):
            owner = owner_of.get(aid)
            if owner:
                break
        # 只有"骨架键都覆盖不到、必须靠本轮补的键才找到宿主"时，才需要动 spec 的 kb 键
        skeleton_covers = any(aid in skeleton_of for aid in _ids_of_l3(lid))
        # 名字证据：真缺 / 只挂验收节点 / 有工序但没挂编号（三种修法不同）
        kind, evidence = _classify_missing(lid, name, leaves)
        acts = []
        for aid in sorted(_ids_of_l3(lid))[:6]:
            try:
                info = kb.activity_info(aid) or {}
            except Exception:                  # noqa: BLE001
                info = {}
            acts.append({"id": aid, "name": info.get("activity_name") or aid,
                         "unit": info.get("unit") or "项"})
        out.append({"l3": lid, "name": name, "phase": owner,
                    "keys": ([] if skeleton_covers
                             else list(_EXTRA_PHASE_KB.get(owner or "", ([], ""))[0])),
                    "kind": kind, "kind_label": _KIND_LABEL[kind],
                    "evidence": evidence, "activities": acts})
    return {"checked": True, "reason": "", "required": len(req),
            "anchored": len([x for x in req
                             if str(x.get("work_type_id") or "") in present]),
            "missing": out}


def missing_kb_essentials(wbs, params):
    """兼容入口：只要缺失清单本身（老调用点/老测试用它）。

    注意它**丢掉了"校验没跑成"的信息**。凡是要区分「不缺」与「没校验成」的地方，
    请直接用 `kb_essentials_report()`。
    """
    return kb_essentials_report(wbs, params)["missing"]


def _kb_scope_gap(spec, wbs, l3_ids):
    """本相 KB 敞口里**已经列出来、但树里一条叶子都没挂**的必含活动名（缺口的确定性判据）。

    判据只有两份现成数据：该相 `spec["kb"]` 键在知识库里的活动清单，以及树上已有的
    `kb_activity_id`。不猜、不调 LLM。返回最多 40 个中文活动名（去重、保序）。
    """
    present = _leaf_kb_ids(wbs)
    want = set()
    for l3 in (l3_ids or []):
        want |= _ids_of_l3(l3)
    out, seen = [], set()
    for k in (spec or {}).get("kb") or []:
        try:
            acts = kb.l4_for(k) or []
        except Exception:
            continue
        for a in acts:
            aid = str(a.get("activity_id") or "")
            name = str(a.get("activity_name") or aid)
            # 只点名"属于缺失 L3"的活动：避免把整段 KB 敞口都列进去（噪声）
            if want and aid not in want:
                continue
            if aid in present or name in seen:
                continue
            seen.add(name)
            out.append(name)
            if len(out) >= 40:
                return out
    return out


def _ids_of_l3(l3_id):
    """某 L3 下的全部 activity_id（KB 不可用 → 空集）。"""
    try:
        return {str(a.get("activity_id")) for a in (kb.l4_for(l3_id) or [])}
    except Exception:
        return set()


def _safe_text(value):
    """None 安全地转字符串（证据/事件里不出现 null）。"""
    return "" if value is None else str(value)


def _fmt_stat(v):
    """回显用的数字格式：整数不带 .0，小数量级保留一位。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return "{:,}".format(int(round(f))) if abs(f - round(f)) < 1e-9 else "{:,.1f}".format(f)

# 轻量：把用户自由文本解析成「目标1级 + 修改意见」的总览提示（内联，避免碎片化 prompt 文件）
_OVERVIEW_OPINION_SYSTEM = (
    "你是 WBS 总览。给定一个 1级 阶段清单和一条人工修改要求，判断这条要求属于哪个 1级 阶段，"
    "并把它转成该阶段展开 LLM 可直接执行的、具体到量的修改意见。\n"
    "只输出 JSON：{\"phase\": \"匹配到的1级阶段名（完全匹配清单中的阶段名；匹配不到返回 null）\",\n"
    "  \"opinion\": \"给该阶段 LLM 的修改意见（含具体数值/改动）\"}\n"
    "若要求不针对任何单个阶段，phase 返回 null。只输出 JSON，不要说明文字。"
)


def _renumber(phases):
    """跨相重编号，保证全局唯一点分 id：phase_no.wp_no.leaf_no。"""
    for pi, ph in enumerate(phases, 1):
        wps = ph.get("work_packages") or []
        for wi, wp in enumerate(wps, 1):
            wp["id"] = f"{pi}.{wi}"
            for li, leaf in enumerate(wp.get("sub_packages") or [], 1):
                leaf["id"] = f"{pi}.{wi}.{li}"
                if not leaf.get("name"):
                    leaf["name"] = leaf["id"]
    return phases


# ==================== 域 3.2：LLM 有序 L4 工序清单（l4_order）====================
#
# 契约（第 5 批冻结）：
#   · `l4_order` 挂在 **1级阶段 dict** 上，形状固定为
#     [{"kb_activity_id","work_type_id","activity_name","unit","quantity"}, ...]；
#   · **顺序 = LLM 给的施工先后顺序**（保序）：下游按它在**所属 L3 内**从 1 重编 L4 工序号；
#   · **去重**：同一个 kb_activity_id 只留第一次出现的位置；
#   · LLM 没给 → **不许编造**，写 `[]`，调用方退回 KB 默认；
#   · 只接受能在 kb.db `L4_Activity_Dictionary` 里查到的 id，查不到 → 记 warning 后丢弃；
#   · 兼容 LLM 换键名（下面 4 个都收，取第一个命中的）；
#   · 保底：LLM 没单独给时，从该相已解析出的 `sub_packages` 顺序提取（含 quantity=0 的叶子）。
# 为何要"量 0 也列"：下游编号对 L4 逐条发号，LLM 若漏掉量 0 的工序就会**跳号**，
# 编号与量的冻结表随之错位。
L4_ORDER_KEYS = ("l4_order", "l4_sequence", "工序清单", "activities")


def _safe_qty(value):
    """工程量 → float；非数值一律 0.0（LLM 常给 None / "" / "待定"）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean_l4_order(items, warn_missing_id=False):
    """把候选清单洗成契约形状：**保序 + 去重 + 只留知识库查得到的 kb_activity_id**。

    返回 `(order, warnings)`。查不到的项**丢弃并记 warning**，绝不编造。

    `work_type_id`（L3 键）**以知识库为准**（`kb.l3_of_activity`）：它才是"该 L4 属于
    哪个 L3"的权威口径 —— 域 4 的② L3 工种号与"L4 所属 L3 ∈ 本分部 kb 列表"的硬约束
    都读它；只有知识库不可用（返回空）时才回落到 LLM 给的值。
    """
    out, warns, seen = [], [], set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        aid = str(it.get("kb_activity_id") or it.get("activity_id")
                  or it.get("kb_id") or "").strip()
        if not aid:
            if warn_missing_id:
                warns.append("l4_order 有一项缺 kb_activity_id，已丢弃")
            continue
        if aid in seen:
            continue                        # 去重：保留第一次出现的位置
        try:
            info = kb.activity_info(aid)     # 复用本仓既有的 KB 查询入口，不自己连 sqlite
        except Exception:                    # noqa: BLE001
            info = None
        if not info:
            warns.append("l4_order 里的 kb_activity_id 在知识库中查不到，已丢弃：" + aid)
            continue
        try:
            l3 = str(kb.l3_of_activity(aid) or "")
        except Exception:                    # noqa: BLE001
            l3 = ""
        seen.add(aid)
        out.append({
            "kb_activity_id": aid,
            "work_type_id": l3 or str(it.get("work_type_id") or ""),
            "activity_name": str(it.get("activity_name") or it.get("name")
                                 or info.get("activity_name") or aid),
            "unit": str(it.get("unit") or info.get("unit") or ""),
            "quantity": _safe_qty(it.get("quantity")),
        })
    return out, warns


def _l4_order_from_sub_packages(wps):
    """保底来源：按 `sub_packages` 的先后顺序提取有序清单（返回 list）。

    只收带 `kb_activity_id` 的叶子（无编号的叶子无法溯源，**静默跳过**、不刷 warning
    —— 树上缺编号已由 `normalize_wbs` 记过一遍了）；
    `quantity=0` 的叶子**照样收**，下游编号必须对"量变 0"免疫。
    遍历口径固定为 `work_packages → sub_packages` 两层，不引入新层。
    """
    items = []
    for wp in wps or []:
        for leaf in (wp or {}).get("sub_packages") or []:
            if isinstance(leaf, dict) and leaf.get("kb_activity_id"):
                items.append({"kb_activity_id": leaf.get("kb_activity_id"),
                              "work_type_id": leaf.get("work_type_id"),
                              "activity_name": leaf.get("name"),
                              "unit": leaf.get("unit"),
                              "quantity": leaf.get("quantity")})
    return _clean_l4_order(items)[0]


def parse_l4_order(raw, wps):
    """从该相 LLM 返回的 JSON 里取 `l4_order`；取不到 → 从 `sub_packages` 保底提取。

    返回 `(order, warnings)`。`order == []` 表示"LLM 没给、树上也提不出来"——
    调用方据此退回 KB 默认，**不要**自己造。
    """
    warns, items = [], None
    if isinstance(raw, dict):
        for key in L4_ORDER_KEYS:
            v = raw.get(key)
            if isinstance(v, list) and v:
                items = v
                break
    if items is not None:
        order, warns = _clean_l4_order(items, warn_missing_id=True)
        if order:
            return order, warns
    return _l4_order_from_sub_packages(wps), warns


def refresh_l4_order_with_tree(phases):
    """把树上出现、但 `l4_order` 里还没有的 kb_activity_id **按树序追加到末尾**。

    为什么需要：跨相融合（`_apply_fusions`，见本文件 G 段）会在各相展开**之后**往相里
    补叶子，那些叶子的 kb_activity_id 不在 LLM 当时给的清单里 —— 若不管，下游按
    `l4_order` 编号时它们会没有 L4 工序号。追加**只动"清单缺的那些"**，
    LLM 给的相对顺序一字不改；没有 `l4_order` 键的相（模板/占位兜底）不动。
    """
    for ph in phases or []:
        if not isinstance(ph, dict):
            continue
        order = ph.get("l4_order")
        if not isinstance(order, list):
            continue
        have = {str(x.get("kb_activity_id")) for x in order if isinstance(x, dict)}
        for it in _l4_order_from_sub_packages(ph.get("work_packages")):
            if it["kb_activity_id"] not in have:
                have.add(it["kb_activity_id"])
                order.append(it)
    return phases


def _template_phase(spec, prompt, params):
    """从项目类型模板里按阶段名取一份子树，作为该相回退。找不到 → None。"""
    for ph in load_template(_pick_template(prompt, params)).get("phases", []):
        if ph.get("phase") == spec["phase"]:
            return {"phase": spec["phase"], "work_packages": ph.get("work_packages") or []}
    return None


def _minimal_phase(spec):
    return {"phase": spec["phase"], "work_packages": [{
        "id": "1.1", "name": spec["phase"],
        "sub_packages": [{"id": "1.1.1", "name": spec["phase"], "duration_days": 1,
                          "quantity": 1, "unit": "项", "work_type": "土建临建"}],
    }]}


def _beat_placeholder_phase(spec):
    """节拍型阶段(4/5/6/8)的逐相占位子树。

    真实节拍流水由 BeatExpandNode 在复评后按代码引擎铺开；此处只产 1 工作包 + 1
    占位叶子，让复评看到完整嵌套层次，不误报「只算一层」，也不让 LLM 铺全楼层。
    """
    return {"phase": spec["phase"], "work_packages": [{
        "id": "1.1", "name": f"{spec['phase']}节拍流水（占位，由节拍引擎展开）",
        "sub_packages": [{"id": "1.1.1", "name": f"{spec['phase']}Ⅰ区首段首工序（占位）",
                          "duration_days": 2, "quantity": 1, "unit": "项",
                          "work_type": "土建临建", "_beat_placeholder": True}],
    }]}


class WBSAgentNode(BaseNode):
    name = "wbs_agent"
    title = "WBS 生成(多级分工)"
    # 引擎读这个键：把 ctx["scope_violations"] 随 node_done 一起上行 → 终端逐条渲染。
    # 与 kb_scope 的 kb_warnings 同一约定（见 kb_scope.KBScopeNode.warning_ctx_key）。
    warning_ctx_key = "scope_violations"

    def __init__(self, llm=None):
        super().__init__()
        self.llm = llm
        self.specs = []          # [(spec,...)] 与 self.frags 对齐
        self.frags = []          # 各 1级 阶段的子树 frag（{phase, work_packages}）
        self.review_round = 0
        self.retry_used = 0
        self.last_repair = None   # 最近一次"一键修复"的回显（终端/演示脚本用）
        # 最近一次"必含工程类型"校验的结构化结果（含 checked/reason）——
        # 供终端回显与测试取用；见 `_essentials()`。
        self.last_essentials = None
        # 范围违规"已经拦过一次"的键集合（phase|activity_id）。
        # 为什么需要：`_review_loop` 会循环（一键修复会重跑目标相），若同一处违规
        # 每轮都当成新 HIGH 就变成"同一个问题反复拦人"。拦一次、修不掉就放过并留痕，
        # 把最终判定交给 R1 审计门（第 32 轮起节拍分段已在本节点上游，R1 看到的是
        # **完整**树）。
        self._scope_gated = set()

    def _llm(self):
        if self.llm is None:
            self.llm = LLMClient()
        return self.llm

    # ---------------- 入口 ----------------
    def run(self, ctx):
        prompt = ctx.get("prompt", "")
        params = ctx.get("extracted_params") or {}
        structure_id = None
        if params.get("structure_type"):
            r = kb.resolve_structure_type(str(params["structure_type"]))
            structure_id = r[0] if r else None
        if params.get("building_type"):
            r = kb.resolve_building_type(str(params["building_type"]))
            if r:
                ctx["kb_building_type"] = r[0]

        # A. 代码骨架：10 个实体 1级 阶段（专项不再独立成相）
        # 先按施工阶段搭好框架（10 个实体阶段，"实体"是内部分类名，不摆给用户）
        self.emit("node_progress", {"node": self.name, "progress": 10,
                                    "message": "先按 %s 个施工阶段搭好框架"
                                               % len(default_phases())})
        self.specs = default_phases()

        # C. 逐相展开
        self.frags = []
        n_total = len(self.specs)
        for i, spec in enumerate(self.specs, 1):
            self.emit("node_progress", {"node": self.name,
                                        "progress": 25 + int(50 * i / n_total),
                                        "message": "正在编「%s」的工序（%d/%d）"
                                                   % (spec['phase'], i, n_total)})
            frag = self._expand_phase(ctx, spec, retry_req=None) \
                or _template_phase(spec, prompt, params) or _minimal_phase(spec)
            # 域 3.2 兜底：模板相/最小相/节拍占位相没有 LLM 清单，照样按 sub_packages 顺序
            # 补一份（提不出来就留 []，调用方退回 KB 默认）——保证每个 1级 相都有该键。
            if frag.get("l4_order") is None:
                frag["l4_order"] = _l4_order_from_sub_packages(frag.get("work_packages"))
            self.frags.append(frag)

        # D. 组装
        self.emit("node_progress", {"node": self.name, "progress": 78,
                                    "message": "把各阶段拼成一棵完整 WBS 树"})
        wbs = self._rebuild(ctx, prompt, params)

        # G. 跨相融合：主体 LLM 开放 2/3级 修改权限，专项归入宿主阶段、去重、保衔接
        n_fuse = 0
        if self.llm_usable:
            self.emit("node_progress", {"node": self.name, "progress": 84,
                                        "message": "去掉重复工序，把专项措施放回它所属的阶段"})
            n_fuse = self._fuse_cross_phase(ctx, params)

        # E/F. 复评 + 人工门
        self._review_loop(ctx, params)

        n_p = len(wbs["phases"])
        n_leaf = sum(len(wp.get("sub_packages", [])) for ph in wbs["phases"]
                     for wp in ph.get("work_packages", []))
        self.done_summary = (f"WBS树已生成(代码{n_total}相+融合{n_fuse}条)："
                             f"{n_p}阶段/{n_leaf}叶子 · 复评{self.review_round}轮")
        self.emit("node_progress", {"node": self.name, "progress": 100, "message": "WBS 校验通过"})
        return {"wbs": wbs}

    # ---------------- 跨相融合（主体 LLM，展开后开放 2/3级 修改） ----------------
    def _fuse_cross_phase(self, ctx, params, user_note=None):
        """整棵树展开后，让主体 LLM 输出跨相融合补丁并应用到既存阶段。

        职责（不再新增 1级）：专项措施归入宿主实体阶段、标记去重、保证衔接。
        user_note：可选的人工修改意见（人工门自由输入），主体 LLM 必须据此修订既有 WBS。
        返回成功应用的补丁条数；无 LLM/失败/空补丁 → 0。
        """
        if not self.llm_usable:
            return 0
        user = ("项目参数：\n" + json.dumps(params or {}, ensure_ascii=False)
                + ("\n\n【人工修改意见（必须落实）】\n" + str(user_note) if user_note else "")
                + "\n\n完整三层WBS：\n" + json.dumps(ctx.get("wbs") or {}, ensure_ascii=False))
        try:
            raw = self._llm().chat_json(load("wbs_fusion.txt"), combine(ctx, user),
                                        temperature=0.2)
        except (LLMError, Exception):
            return 0
        fusions = (raw or {}).get("fusions") or []
        if not fusions:
            return 0
        applied = self._apply_fusions(ctx.get("wbs") or {}, fusions)
        if applied:
            self._renumber_apply(ctx)
        return applied

    def _apply_fusions(self, wbs, fusions):
        """把融合补丁应用到整棵 wbs。返回成功应用的条数。"""
        phases = {ph.get("phase"): ph for ph in wbs.get("phases") or []}
        applied = 0
        for f in fusions:
            if not isinstance(f, dict):
                continue
            phase_name = str(f.get("target_phase") or "").strip()
            phase = phases.get(phase_name)          # 严禁第 11 相：只命中既有阶段
            if phase is None:
                continue
            wp_name = str(f.get("wp_name") or "").strip()
            action = str(f.get("action") or "add_wp").strip()
            wps = phase.setdefault("work_packages", [])
            if action in ("add_wp", "add_leaf"):
                wp = next((w for w in wps if (w.get("name") or "") == wp_name), None)
                if wp is None:
                    # 新建工作包
                    if not wp_name:
                        continue
                    wp = {"id": "", "name": wp_name, "sub_packages": []}
                    wps.append(wp)
                leaf = {
                    "id": "", "name": str(f.get("leaf_name") or wp_name).strip(),
                    "duration_days": int(f.get("duration_days") or 1),
                    "quantity": float(f.get("quantity") or 0),
                    "unit": str(f.get("unit") or "项"),
                    "work_type": str(f.get("work_type") or "土建临建"),
                }
                if f.get("kb_activity_id"):
                    leaf["kb_activity_id"] = str(f["kb_activity_id"])
                # 去重：同工作包内已有同名叶子则跳过
                if any((l.get("name") or "") == leaf["name"] for l in wp["sub_packages"]):
                    continue
                wp["sub_packages"].append(leaf)
                applied += 1
            elif action == "adjust":
                for wp in wps:
                    for l in wp.get("sub_packages", []):
                        if (l.get("name") or "") == str(f.get("leaf_name") or "").strip():
                            if f.get("duration_days"):
                                l["duration_days"] = int(f["duration_days"])
                            if f.get("quantity") is not None:
                                l["quantity"] = float(f["quantity"]) if f["quantity"] else 0
                            if f.get("unit"):
                                l["unit"] = str(f["unit"])
                            if f.get("work_type"):
                                l["work_type"] = str(f["work_type"])
                            applied += 1
                            break
        return applied

    def _renumber_apply(self, ctx):
        """应用补丁后重编号 + 归一化（沿用 _rebuild 的 normalize 兜底）。"""
        _renumber(ctx["wbs"].get("phases") or [])
        # 域 3.2：融合补进来的叶子不在 LLM 原始 l4_order 里 → 按树序追加到各相清单末尾，
        # 否则下游按 l4_order 编号时这些叶子会没有 L4 工序号（LLM 给的相对顺序不动）。
        refresh_l4_order_with_tree(ctx["wbs"].get("phases") or [])
        wbs, warns = normalize_wbs(ctx["wbs"])
        if wbs is None:
            return
        if warns:
            ctx.setdefault("wbs_warnings", []).extend(warns)
        ctx["wbs"] = wbs

    def _llm_beat_l4_order(self, ctx, spec, retry_req):
        """节拍分部专用：只问模型要"有序的 L4 工序清单"，树仍由代码引擎展开（域 3.2）。

        返回 list（形状同 `parse_l4_order`）；拿不到 → `[]`（**绝不编造**）。
        只走模型：模型不可用时直接返回 `[]`，行为与第 4 批一致。
        """
        if not self.llm_usable:
            return []
        params = ctx.get("extracted_params") or {}
        user = (resolve_beat_phase_prompt(spec)
                + "\n\n本阶段：\n" + json.dumps(spec.get("phase"), ensure_ascii=False)
                + "\n\n项目参数：\n" + json.dumps(params, ensure_ascii=False))
        inj = build_phase_kb_injection(spec.get("kb"), scope=ctx.get("kb_scope"))
        if inj:
            user += "\n\n" + inj
        if retry_req:
            user += f"\n\n【修改要求（本阶段必须落实）】\n{retry_req}"
        try:
            raw = self._llm().chat_json(load("wbs_phase.txt"), combine(ctx, user), temperature=0.3)
        except (LLMError, Exception):
            return []
        if not isinstance(raw, dict):
            return []
        order, warns = parse_l4_order(raw, raw.get("work_packages"))
        if warns:
            ctx.setdefault("wbs_warnings", []).extend(warns)
        return order

    # ---------------- 逐相展开 ----------------
    def _expand_phase(self, ctx, spec, retry_req):
        # 节拍型阶段：复评期间只给占位子树（真实流水由 BeatExpandNode 展开）
        if (spec or {}).get("phase") in BEAT_PHASE_NAMES:
            frag = _beat_placeholder_phase(spec)
            # 域 3.2（第 5 批）：**"有哪些工序、什么顺序"必须来自模型**。
            # 节拍分部的树由代码引擎铺开（不让模型铺全楼层），但工序顺序不能继续由
            # `beat_configs.BASE_BEAT_CONFIGS.cycle` 写死 —— 所以这里单独要一次
            # `l4_order`（只排序、不编号；量 0 的工序也要列），交给 BeatExpandNode。
            # 取不到（离线 / 模型不可用 / 老计划）→ 不写该键：下游退回
            # "声明顺序即施工先后"，行为与第 4 批逐字段一致（`test_algorithm_parity.py` 钉着）。
            order = self._llm_beat_l4_order(ctx, spec, retry_req)
            if order:
                frag["l4_order"] = order
            return frag
        if not self.llm_usable:
            return None
        params = ctx.get("extracted_params") or {}
        user = (resolve_prompt(spec) + "\n\n本阶段：\n" + json.dumps(spec["phase"], ensure_ascii=False)
                + "\n\n项目参数：\n" + json.dumps(params, ensure_ascii=False))
        # v2.1：传入知识库范围（kb_scope），按项目结构形式过滤候选工序，
        # 并把不适用的工序明确列为"严禁使用"（例如剪力墙楼不得出现柱浇筑）。
        inj = build_phase_kb_injection(spec.get("kb"), scope=ctx.get("kb_scope"))
        if inj:
            user += "\n\n" + inj
        if retry_req:
            user += f"\n\n【修改要求（本阶段必须落实）】\n{retry_req}"
        try:
            raw = self._llm().chat_json(load("wbs_phase.txt"), combine(ctx, user), temperature=0.3)
            wps = raw.get("work_packages")
            if not wps:
                return None
            # 域 3.2：本相的有序 L4 工序清单（LLM 只排序、不编号；量 0 的工序也在内）。
            # 取不到 → 从 sub_packages 保底；再取不到 → []（调用方退回 KB 默认）。
            order, l4_warns = parse_l4_order(raw, wps)
            if l4_warns:
                ctx.setdefault("wbs_warnings", []).extend(l4_warns)
            # 深拷贝：防御 LLM/桩返回复用 dict，避免跨相共享引用在 _renumber 原地改时互相覆盖
            return {"phase": spec["phase"], "work_packages": copy.deepcopy(wps),
                    "l4_order": order}
        except (LLMError, Exception):
            return None

    # ---------------- 组装 ----------------
    def _rebuild(self, ctx, prompt="", params=None):
        _renumber(self.frags)
        wbs, warns = normalize_wbs({"phases": self.frags})
        if wbs is None or not (wbs.get("phases") or []):
            self.emit("node_progress", {"node": self.name, "progress": 80,
                                        "message": "模型展开不成，改用项目模板"})
            wbs, warns = normalize_wbs(load_template(_pick_template(prompt or ctx.get("prompt", ""),
                                                                    params or ctx.get("extracted_params") or {})))
            if wbs is None:
                raise RuntimeError("模板兜底也失败")
            ctx["wbs_source"] = "template"
        else:
            if warns:
                ctx.setdefault("wbs_warnings", []).extend(warns)
            ctx["wbs_source"] = "llm"
        ctx["wbs"] = wbs
        return wbs

    # ---------------- 复评 + 人工门 ----------------
    def _review_loop(self, ctx, params):
        self.review_round = 0
        while self.review_round < MAX_REVIEW_ROUNDS:
            self.review_round += 1
            self.emit("node_progress", {"node": self.name, "progress": 85,
                                        "message": "自查一遍这棵 WBS 树"})
            # ① 代码核对 kb_scope 范围一致性（不调 LLM）：结果既进评审证据，也进人工门。
            # 顺序很重要：必须在 `_review` **之前**算出来，评审模型才拿得到这份硬事实
            # （否则模型只能凭"看着像不像"猜，实测会漏掉挂错编号的工序）。
            scope_check = self._run_scope_check(ctx)
            evidence = self._self_check(params, ctx["wbs"], scope_check=scope_check)
            review = self._review(ctx, params, ctx["wbs"], evidence)
            issues, verdict = self._resolve_review(review)
            # ② 范围违规并进 issues（每次重算，修好了就自动消失）。
            # 为什么用 HIGH：它不依赖模型判断，是"计划里出现了明令禁止的工序"，
            # 属于用户必须看见、且本节点**真的能改**（重跑该相）的一类。
            issues = list(issues) + self._new_scope_issues(scope_check)
            high = [i for i in issues if i.get("severity") == "HIGH"]
            if high and self._registry is not None:
                decision = self._human_gate(high, ctx["wbs"], params)
                if decision.get("action") == "approve":
                    return
                if decision.get("action") == "abort":
                    raise PipelineCancelled
                # ① 选了编号（一键修复）→ 走本节点内部的重做机制，真改 WBS
                if decision.get("repair_key"):
                    self.emit("node_progress", {"node": self.name, "progress": 87,
                                                "message": "按你选的那条重做 WBS 展开"})
                    ok, line = self._repair_gate(ctx, params, decision, high)
                    if ok:
                        self.emit("node_progress", {"node": self.name, "progress": 88,
                                                    "message": "改动回显：" + line})
                        continue
                    self.emit("node_progress", {"node": self.name, "progress": 88,
                                                "message": "这一条没有改动：" + str(line)})
                    return
                # ② 自由文本意见 → 主体 LLM 按意见融合修订既有 WBS（全树 2/3级 修改权限）
                raw = decision.get("raw")
                if raw and self.retry_used < MAX_RETRY_ON_HUMAN and self.llm_usable:
                    self.retry_used += 1
                    self.emit("node_progress", {"node": self.name, "progress": 87,
                                                "message": "按你写的意见修订 WBS"})
                    n = self._fuse_cross_phase(ctx, params, user_note=raw)
                    if n:
                        continue
                return
            if verdict == "PASS":
                return
            # MED/LOW 独有 → 静默收敛：按 REPORT 维度镜像给对应相重跑一轮
            if self.retry_used < MAX_RETRY_ON_HUMAN and self.llm_usable:
                if self._apply_feedback(ctx, params, issues):
                    self.retry_used += 1
                    continue
            return

    # ---------------- 知识库范围一致性（代码硬校验） ----------------
    def _run_scope_check(self, ctx):
        """核对树里的 kb_activity_id 是否都在 kb_scope 范围内，并写回 ctx。

        结果落两个键：
          · `ctx["kb_scope_conformance"]` —— 结构化全量（plan_assembler 会留档进
            `plan_json.meta`，交付物 / 修订链可核对）；
          · `ctx["scope_violations"]` —— 逐条中文原文（引擎随 node_done 上行，
            终端逐条渲染，与上游 kb_scope 的 kb_warnings 同一约定）。
        任何异常都降级成"没核对"，绝不因为校验把 WBS 生成搞挂。
        """
        try:
            res = check_scope_conformance(ctx.get("wbs"), ctx.get("kb_scope"))
        except Exception:                        # noqa: BLE001
            res = {"checked": False, "leaves": 0, "anchored": 0, "violations": 0,
                   "by_kind": {}, "issues": [],
                   "summary": "知识库范围一致性核对未能执行（不影响计划生成）", "note": ""}
        ctx["kb_scope_conformance"] = res
        lines = []
        for it in res.get("issues") or []:
            lines.append("%s（%s）：%s 挂了 %s，%s" % (
                it.get("phase") or "未知阶段", it.get("wp") or "未知工作包",
                it.get("leaf_name") or "一条叶子", it.get("activity_id"),
                it.get("reason") or it.get("kind_label") or "不在范围内"))
        note = scope_format_note(res)
        if note:
            lines.append(note.strip())
        ctx["scope_violations"] = lines
        return res

    def _new_scope_issues(self, scope_check):
        """范围违规 → 复评门 issues（只报**没拦过**的，避免循环里反复拦同一个问题）。"""
        fresh = []
        for it in (scope_check or {}).get("issues") or []:
            key = "%s|%s" % (it.get("phase"), it.get("activity_id"))
            if key in self._scope_gated:
                continue
            self._scope_gated.add(key)
            fresh.append(it)
        if not fresh:
            return []
        return scope_gate_issues({"issues": fresh})

    def _review(self, ctx, params, wbs, evidence):
        user = ("项目参数：\n" + json.dumps(params, ensure_ascii=False) +
                "\n\n候选三层WBS：\n" + json.dumps(wbs, ensure_ascii=False) +
                "\n\n脚本自检证据：\n" + json.dumps(evidence, ensure_ascii=False))
        try:
            return self._llm().chat_json(load("wbs_review.txt"), combine(ctx, user), temperature=0.0)
        except (LLMError, Exception):
            return None

    @staticmethod
    def _resolve_review(review):
        if review and isinstance(review, dict):
            issues = review.get("issues") or []
            verdict = str(review.get("verdict", "REVISE")).upper()
            return issues, ("PASS" if not issues else verdict)
        return [], "REVISE"

    def _overview_opinion(self, ctx, user_text, specs):
        if not self.llm_usable:
            for s in specs:
                if s["phase"] and s["phase"] in (user_text or ""):
                    return s["phase"], user_text
            return None, None
        names = "\n".join(f"  {s['phase']}" for s in specs)
        try:
            raw = self._llm().chat_json(_OVERVIEW_OPINION_SYSTEM,
                                        f"1级 阶段清单：\n{names}\n\n人工修改要求：\n{user_text}",
                                        temperature=0.0)
            ph = (raw or {}).get("phase")
            op = (raw or {}).get("opinion")
            if not ph or not any(s["phase"] == str(ph).strip() for s in specs):
                return None, None
            return str(ph).strip(), str(op or user_text)
        except (LLMError, Exception):
            return None, None

    def _re_expand_target(self, ctx, target_phase, opinion):
        """重跑目标 1级 → 替换其子树 → 组装。"""
        for i, spec in enumerate(self.specs):
            if spec["phase"] == target_phase:
                frag = self._expand_phase(ctx, spec, retry_req=opinion)
                if frag:
                    self.frags[i] = frag
                    self._rebuild(ctx)
                return

    def _apply_feedback(self, ctx, params, issues):
        """把 MED/LOW 问题按跨相目标分发给对应相重跑；有改动返回 True。"""
        by_phase = {}
        for it in issues:
            t = str(it.get("target") or "")
            try:
                idx = int(t.split(".")[0]) - 1
            except (ValueError, TypeError):
                continue
            if 0 <= idx < len(self.specs):
                by_phase.setdefault(self.specs[idx]["phase"], []).append(
                    f"[{it.get('severity')}][{it.get('dimension')}] {it.get('finding')} → {it.get('suggestion')}")
        changed = False
        for ph_name, notes in by_phase.items():
            for i, spec in enumerate(self.specs):
                if spec["phase"] == ph_name:
                    frag = self._expand_phase(ctx, spec, retry_req="；".join(notes))
                    if frag:
                        self.frags[i] = frag
                        changed = True
                    break
        if changed:
            self._rebuild(ctx)
        return changed

    # ---------------- 脚本自检（软证据） ----------------
    def _self_check(self, params, wbs, scope_check=None):
        """数一遍 WBS 里**真实有什么**，作为审计门的对照证据。

        ⚠️ 这里踩过一个很贵的坑（第 24 轮修）：早先为了让节拍型阶段不干扰统计，
        代码把节拍型阶段**整个排除**（`if ph["phase"] not in beat_phases`）。
        当时节拍阶段里只有「占位」任务，排除看着"干净"；但后来 `beat_build` 真的
        按层铺开了工程量 —— 排除逻辑就变成了**把主体工程量全丢掉**：
        实测同一份 WBS（住宅 14200㎡ / 18 层），自检报 `conc_m3=0 / rebar_t=0 /
        leaves=9`，而真实值是 **20216 m³ / 18996 吨 / 209 条叶子**。
        这份"0"被当成证据送进审计模型，于是模型写出「混凝土缺失超 93%」这类
        **假 HIGH**，门把用户白白拦下。
        → 结论：**统计必须覆盖全部阶段**；节拍型阶段的数量单独报出来供人对照。

        ⚠️ 但"覆盖全部阶段"还不够（第 25 轮补）：本门的所在位置决定了它**看不到**
        节拍型阶段的真实工程量 —— 复评门跑在 `beat_build` **之前**（builder.py:65-68），
        此刻节拍相里是 `_beat_placeholder_phase()` 的占位叶子（量=1 / 单位=项 /
        `_beat_placeholder=True`）。所以 `conc_m3=0` 是**结构上必然**的，不是"缺失"。
        → 证据必须**自证局限**：`beat_expanded` / `placeholder_leaves` / `note`
        三个字段就是给评审模型的"别据此报 HIGH"信号（见 BEAT_NOT_EXPANDED_NOTE
        与 prompts/wbs_review.txt 的硬约束）。
        """
        phases = wbs.get("phases") or []
        beat_phases = {p.get("phase") for p in phases if p.get("phase") in BEAT_PHASE_NAMES}

        def _leaves_in(ph_list):
            return [l for ph in ph_list
                    for wp in (ph.get("work_packages") or [])
                    for l in (wp.get("sub_packages") or [])]

        leaves = _leaves_in(phases)                       # 全部阶段，一个不落
        beat_leaves = _leaves_in([p for p in phases if p.get("phase") in beat_phases])
        floors = int(params.get("floors") or 0)
        area = float(params.get("area") or params.get("total_area") or 0)
        gfa = area * floors if area and floors else 0.0
        conc = sum(float(l.get("quantity") or 0) for l in leaves
                   if "混凝土" in (l.get("name") or "") + (l.get("work_type") or "")
                   and (l.get("unit") or "") == "m³")
        rebar = sum(float(l.get("quantity") or 0) for l in leaves
                    if "钢筋" in (l.get("work_type") or ""))
        beat_expanded, n_placeholder = self._beat_expansion_state(phases, beat_phases)
        ev = {"GFA": f"{gfa:.0f}", "conc_m3": conc, "rebar_t": rebar,
              "leaves": len(leaves), "beat_leaves": len(beat_leaves),
              "phases": len(phases), "beat_phases": len(beat_phases),
              # 节拍型阶段有没有真的展开（见 BEAT_NOT_EXPANDED_NOTE）：
              # 证据必须自证局限，否则评审模型会拿"本门看不到的 0"当成"缺失"报假 HIGH。
              "beat_expanded": beat_expanded, "placeholder_leaves": n_placeholder,
              "note": (BEAT_NOT_EXPANDED_NOTE if not beat_expanded
                       else "节拍型阶段已展开，conc_m3/rebar_t 可代表当前 WBS 的工程量")}
        # 知识库范围一致性（代码核对结果）**必须进评审证据**：
        # ① 让评审模型知道哪些工序是"被明令禁止却出现了"，不再把它当"覆盖不足"讨论；
        # ② 反过来也拦住一类假 HIGH —— 模型常把"柱浇筑没出现"当成漏项，
        #    而剪力墙结构下它本来就在禁用清单里。
        conformance = {"checked": bool((scope_check or {}).get("checked")),
                       "summary": _safe_text((scope_check or {}).get("summary")),
                       "violations": int((scope_check or {}).get("violations") or 0),
                       "issues": [
                           {k: it.get(k) for k in ("phase", "wp", "activity_id",
                                                   "leaf_name", "kind_label", "reason")}
                           for it in ((scope_check or {}).get("issues") or [])[:10]]}
        if conformance["checked"]:
            ev["kb_scope_conformance"] = conformance
        return ev

    @staticmethod
    def _beat_expansion_state(phases, beat_names):
        """节拍型阶段是否真的展开过？返回 (beat_expanded, 占位叶子数)。

        判据只用树上现成的字段，不猜：
          · 节拍型阶段为空 → 没展开；
          · 阶段里**全部**叶子都带占位标记（`_beat_placeholder`，由本文件
            `_beat_placeholder_phase()` 打上）或数量 ≤ 0 → 还是占位形态；
          · 只要有一条真实叶子（有正数量）就算已展开 —— 不把正常情况误标成未展开。
        没有节拍型阶段（阶段名对不上）→ 视为已展开（与本门无关，不误报）。
        """
        if not beat_names:
            return True, 0
        beat = [ph for ph in (phases or []) if ph.get("phase") in beat_names]
        if not beat:
            return True, 0
        n_placeholder = 0
        n_real = 0
        for ph in beat:
            for wp in (ph.get("work_packages") or []):
                for leaf in (wp.get("sub_packages") or []):
                    if not isinstance(leaf, dict):
                        continue
                    try:
                        qty = float(leaf.get("quantity") or 0)
                    except (TypeError, ValueError):
                        qty = 0.0
                    if leaf.get("_beat_placeholder") or qty <= 0:
                        n_placeholder += 1
                    else:
                        n_real += 1
        return (n_real > 0), n_placeholder

    # ---------------- 一键修复：选项怎么来、怎么真的重做 ----------------
    def _essentials(self, ctx, wbs, params):
        """必含工程类型校验的统一入口：算一次、落 ctx、把"没校验成"说出来。

        为什么必须有这个方法：`missing_kb_essentials()` 在取不到建筑类型时**静默返回空**，
        与"真的不缺"在界面上完全无法区分（实测：`building_type=None` 的那份计划恒为
        "缺 0 类"，用户以为没问题）。这里统一落两个键：
          · `ctx["kb_essentials"]`   —— 结构化结果（plan_assembler 留档进 meta，可事后核对）；
          · `ctx["scope_violations"]` —— 一句中文（本节点的告警通道，终端会渲染）。
        任何异常都降级成"没校验成"，**绝不**因为这项校验把 WBS 生成搞挂。
        """
        try:
            rep = kb_essentials_report(wbs, params)
        except Exception as exc:               # noqa: BLE001
            rep = {"checked": False,
                   "reason": "必含类型校验异常（%s）" % exc.__class__.__name__,
                   "required": 0, "anchored": 0, "missing": []}
        self.last_essentials = rep
        if isinstance(ctx, dict):
            ctx["kb_essentials"] = rep
            if not rep.get("checked"):
                line = ("必含工程类型未能校验：%s（这不等于不缺类型）"
                        % (rep.get("reason") or "原因未知"))
                warns = ctx.setdefault("scope_violations", [])
                if isinstance(warns, list) and line not in warns:
                    warns.append(line)
        return rep

    def _repair_stats(self, ctx, params):
        """重做前后给用户对照的三个数：叶子 / 混凝土 / 缺失的必含工程类型。"""
        wbs = ctx.get("wbs") or {}
        ev = self._self_check(params, wbs)
        rep = self._essentials(ctx, wbs, params)
        return {"leaves": ev.get("leaves") or 0,
                "conc_m3": float(ev.get("conc_m3") or 0.0),
                "missing": len(rep.get("missing") or []),
                "missing_checked": bool(rep.get("checked")),
                "missing_reason": str(rep.get("reason") or "")}

    @staticmethod
    def _stat_line(before, after):
        """一行中文回显"改了什么"（这行会进终端 + 审计链）。"""
        line = ("重做前后：叶子任务 %s → %s 条 ｜ 混凝土 %s → %s m³ ｜ "
                "缺失必含工程类型 %s → %s 类"
                % (_fmt_stat(before.get("leaves")), _fmt_stat(after.get("leaves")),
                   _fmt_stat(before.get("conc_m3")), _fmt_stat(after.get("conc_m3")),
                   _fmt_stat(before.get("missing")), _fmt_stat(after.get("missing"))))
        # "0 类"与"没校验成"必须分开说：否则取不到建筑类型时用户看到"缺 0 类"，
        # 会以为一切正常（实测坑，见 kb_essentials_report 的 docstring）。
        for tag, st in (("重做前", before), ("重做后", after)):
            if st.get("missing_checked") is False:
                line += ("；注意：%s的必含工程类型没能校验（%s），"
                         "这里的 0 类不代表不缺" % (tag, st.get("missing_reason") or "原因未知"))
        return line

    def _repair_options(self, ctx, params, issues):
        """本门**可提供**的修复选项 + "需要你提供信息"的说明。

        只放**有真实机制**能改动的选项（`_repair_gate` 里能走的代码路径），
        走不通的一个都不放 —— 否则就是"看起来修了其实没修"。
        """
        notes = []
        for it in issues or []:
            need = issue_needs_user_info(it)
            if need:
                notes.append(need)
        if self.retry_used >= MAX_RETRY_ON_HUMAN:
            return [], notes, ("系统本轮已替你重做 %d 次（上限 %d 次），不再自动重做；"
                               "仍不满意请直接输入你的意见（{FREE}）或 /abort。"
                               % (self.retry_used, MAX_RETRY_ON_HUMAN))
        opts = [{
            "key": REPAIR_REEXPAND,
            "label": "按项目参数重新展开受影响的 WBS 阶段（工程量和工期一次改齐）",
            "hint": "系统重做 WBS 展开：对评审点到名的阶段，按项目参数（层数/面积/总量）"
                    "重新推总量与工期，要求每条工序都带工程量+单位+工期，再重算一遍自查。",
            "kind": "重新展开",
        }]
        rep = self._essentials(ctx, ctx.get("wbs") or {}, params)
        miss = rep["missing"]
        if not rep.get("checked"):
            # 静默坑：校验没跑成时缺失清单恒为空，用户会以为"不缺"。
            notes.append("必含工程类型这一项本轮没能校验：%s。"
                         "所以这次没报「缺类型」不等于真的不缺 —— "
                         "请把建筑类型写进项目参数（例如「住宅」）后重做。"
                         % (rep.get("reason") or "原因未知"))
        # 只在**确有宿主相**时才给这个选项：宿主相为 None 的类型在本架构下补不了
        # （WBS 只允许固定 10 个 1级 相，凭空造新相是改产品结构，不是"修复"）。
        fixable = [m for m in miss if m.get("phase")]
        if fixable:
            names = "、".join(sorted({m["name"] for m in fixable}))
            # 把"缺"的三种情形亮在选项上：真缺 / 只有验收节点 / 没挂编号（修法不同）。
            brief = {"missing": "真缺", "inspection_only": "只有验收节点",
                     "unanchored": "没挂编号"}
            cnt = {}
            for m in fixable:
                k = str(m.get("kind") or "missing")
                cnt[k] = cnt.get(k, 0) + 1
            detail = "｜".join("%s %d 类" % (brief.get(k, k), cnt[k])
                               for k in ("missing", "inspection_only", "unanchored")
                               if cnt.get(k))
            opts.append({
                "key": REPAIR_FILL_TYPES,
                "label": "补齐知识库里缺失的必含工程类型（%d 类：%s）%s"
                         % (len(fixable), names[:48] + ("…" if len(names) > 48 else ""),
                            ("（%s）" % detail) if detail else ""),
                "hint": "按数据库里这类项目必须有的工序逐条补齐：把缺的类型交回它本来所属的"
                        "阶段重新展开，点名该阶段还缺哪些必含工序，"
                        "并给每条工序标上数据库里的工序编号。"
                        "只挂了验收/检测节点的类型（如只有「防水隐蔽验收」）必须补出施工工序，"
                        "验收节点不算施工。",
                "kind": "补齐类型",
            })
        return opts, notes, ""

    def _repair_gate(self, ctx, params, decision, issues):
        """选了编号 → **真的重做**。返回 (True, 一行中文回显) 或 (False, 原因)。

        走的全是**本节点内部现成机制**：
          · `reexpand_wbs`     → `_expand_phase(spec, retry_req=...)` 重跑受影响的相；
          · `fill_missing_types` → 同上，但重试要求是知识库 REQUIRED 缺失类型的确定性清单。
        """
        key = str((decision or {}).get("repair_key") or "").strip()
        if key not in (REPAIR_REEXPAND, REPAIR_FILL_TYPES):
            return False, "没认出这个修复编号（%s）" % (key or "空")
        if self.retry_used >= MAX_RETRY_ON_HUMAN:
            return False, ("本轮已重做 %d 次（上限 %d 次），不再自动重做"
                           % (self.retry_used, MAX_RETRY_ON_HUMAN))
        if not self.llm_usable:
            return False, "当前没有可用的大模型，无法重做 WBS"

        before = self._repair_stats(ctx, params)
        rep = self._essentials(ctx, ctx.get("wbs") or {}, params)
        miss = rep["missing"]
        if key == REPAIR_FILL_TYPES and not rep.get("checked"):
            return False, ("必含工程类型这项校验本轮没能执行：%s。"
                           "补不了就直说 —— 请把建筑类型写进项目参数（例如「住宅」）后重试。"
                           % (rep.get("reason") or "原因未知"))
        targets, reqs = self._repair_targets(ctx, params, issues, key, miss)
        if key == REPAIR_FILL_TYPES and not any(m.get("phase") for m in miss):
            # 缺失类型在代码骨架里**没有可信的宿主阶段** —— 当前架构下做不到自动修：
            # WBS 只允许固定 10 个一级阶段，凭空造一个新阶段就是改产品结构，不是"修复"。
            return False, ("这几类工程在当前结构里找不到可以挂进去的阶段，系统改不了"
                           "（已在门上写明要你补充信息）")
        if not targets:
            return False, "没找到可以改的阶段（这几条评审问题没落到任何一级阶段上）"

        changed = 0
        for idx, spec in enumerate(self.specs):
            if spec["phase"] not in targets:
                continue
            req = reqs.get(spec["phase"]) or ""
            frag = self._expand_phase(ctx, spec, retry_req=req)
            if frag and (frag.get("work_packages") or []):
                self.frags[idx] = frag
                changed += 1
        if not changed:
            return False, "重做没有产出可用的子树（该相展开失败），WBS 保持原样"

        # 重做用到的新 kb 键（比如「材料运输与加工工程」）常驻本相 spec ——
        # 后续所有注入（KB 范围 / 复评）都按同一份口径，不再"只在这一轮看见"。
        for ph_name in targets:
            xkeys = list(_EXTRA_PHASE_KB.get(ph_name, ([], ""))[0])
            if not xkeys:
                continue
            for spec in self.specs:
                if spec["phase"] == ph_name:
                    spec["kb"] = list(dict.fromkeys(list(spec.get("kb") or []) + xkeys))
        self._rebuild(ctx)
        self.retry_used += 1
        after = self._repair_stats(ctx, params)
        line = self._stat_line(before, after)
        if key == REPAIR_FILL_TYPES and miss:
            line += ("；已把缺失类型交回宿主相重新展开：%s"
                     % "、".join("%s→%s" % (m["name"], m["phase"] or "（骨架无宿主相）")
                                for m in miss[:6]))
        if after.get("conc_m3") == 0:
            line += ("；说明：混凝土这类量要等「节拍流水分段」按层铺开才算得出来，"
                     "本门此刻看到的只是一行占位")
        self.last_repair = {"key": key, "line": line,
                            "before": before, "after": after}
        return True, line

    def _repair_targets(self, ctx, params, issues, key, miss):
        """算出这次要重做哪些 1级 相，以及每相的重试要求（纯确定性）。"""
        by_phase = {}
        for it in (issues or []):
            it = it if isinstance(it, dict) else {}
            t = str(it.get("target") or "")
            for i, spec in enumerate(self.specs, 1):
                if t.startswith(str(i)) or t == spec["phase"] or spec["phase"] in t:
                    by_phase.setdefault(spec["phase"], []).append(
                        "[%s][%s] %s → %s" % (it.get("severity"), it.get("dimension"),
                                              it.get("finding"), it.get("suggestion")))
                    break
        reqs = {}
        if key == REPAIR_FILL_TYPES:
            # 两条腿一起走，缺一类都不算补齐：
            #   ① 知识库里 REQUIRED 但树里一条叶子都没锚定到的类型（确定性清单）；
            #   ② 该相自己 KB 敞口里**已经列出来、但树里没出现**的活动
            #      （LLM 漏掉的必含工序，同一份 kb 范围就是判据）。
            for m in miss:
                owner = m.get("phase")
                if not owner:              # 骨架里没有宿主相 → 这个选项就不提供（见 _repair_options）
                    continue
                acts = []
                for aid in sorted(_ids_of_l3(m["l3"]))[:6]:
                    try:
                        info = kb.activity_info(aid) or {}
                    except Exception:
                        info = {}
                    acts.append("%s(%s/%s)" % (info.get("activity_name") or aid, aid,
                                               info.get("unit") or "项"))
                extra = _EXTRA_PHASE_KB.get(owner, ([], ""))[1]
                reqs[owner] = ((reqs.get(owner, "") + "\n" if owner in reqs else "")
                               + "本次必须补齐【%s（%s）】这一类工程类型：\n"
                                 "  候选知识库活动：%s\n"
                                 "  要求：把与本工程实际相符的工序展开为叶子，每条叶子写 "
                                 "work_type 并挂 kb_activity_id；单位按括号内单位。"
                               % (m["name"], m["l3"], "、".join(acts) or "（知识库无活动）")
                               + ("\n  " + extra if extra else ""))
                # 名字证据要进重试要求：只有验收节点 / 有工序没挂编号 —— 把话说死，
                # 否则 LLM 很可能再产一条"XX验收"就算补过了（实测就是这个坑）。
                if m.get("kind") == "inspection_only" and m.get("evidence"):
                    reqs[owner] += ("\n  注意：树里目前只有%s这类验收/检测节点，"
                                    "【验收节点不算施工】—— 本次必须补出真正的施工工序。"
                                    % "、".join("「%s」" % e for e in m["evidence"]))
                elif m.get("kind") == "unanchored" and m.get("evidence"):
                    reqs[owner] += ("\n  注意：树里已有%s这类工序但没挂数据库编号，"
                                    "本次必须给它们补上 kb_activity_id。"
                                    % "、".join("「%s」" % e for e in m["evidence"]))
            # ② 按相各自的 KB 敞口，把"树里还没出现的必含活动"点名补齐
            by_l3 = {}
            for m in miss:
                if m.get("phase"):
                    by_l3.setdefault(m["phase"], []).append(m["l3"])
            for ph_name, l3s in by_l3.items():
                spec = next((s for s in self.specs if s["phase"] == ph_name), None)
                if spec is None:
                    continue
                gap = _kb_scope_gap(spec, ctx.get("wbs") or {}, l3s)
                if gap:
                    reqs[ph_name] = (reqs.get(ph_name, "") + "\n"
                                     + "另外，本阶段 KB 敞口里下列必含工序树里还没有，"
                                       "请一并补齐（每条写 work_type + kb_activity_id + 工程量 + 工期）：\n  "
                                     + "、".join(gap[:14]))
        if key == REPAIR_REEXPAND or not reqs:
            for ph_name, found in by_phase.items():
                if ph_name in reqs:
                    reqs[ph_name] += "\n同时落实评审意见：\n  " + "\n  ".join(found)
                else:
                    reqs[ph_name] = "本次重做必须落实评审意见：\n  " + "\n  ".join(found)
        targets = set(reqs.keys()) or set(by_phase.keys())
        if not targets and key == REPAIR_REEXPAND:
            # 评审没点到具体相（例如只有"覆盖"类问题）：逐个重新展开所有非节拍相
            for spec in self.specs:
                if spec["phase"] not in BEAT_PHASE_NAMES:
                    reqs[spec["phase"]] = (
                        "本次重做按项目参数把本相展开到位：每条叶子必须有 work_type、"
                        "quantity + unit、duration_days；工程量按项目参数推总量"
                        "（严禁只给一层/一项）；工期与工程量匹配，不要与总量脱钩。")
            targets = set(reqs.keys())
        return targets, reqs

    # ---------------- 人工门（HIGH / 打断） ----------------
    def _human_gate(self, issues, candidate, params):
        pid = f"wbsr_{getattr(self, '_run_id', 'run')}_{uuid.uuid4().hex[:4]}"
        self._registry.register(pid)
        # 结构化问题清单：终端按「一条一行」竖版渲染。
        # 老写法是把每条截断到 40 字再用 ` · ` 拼成**一整行**，实测在终端里糊成一片、
        # 完全没法读（用户反馈"一坨丢过来可读性太差"）。这里改为给全量结构化字段。
        shown = [{"severity": str(i.get("severity") or ""),
                  "dimension": str(i.get("dimension") or ""),
                  "finding": str(i.get("finding") or "")} for i in issues[:8]]
        # 一键修复：可选编号选项 + "这条需要你提供信息"的说明。
        # 取数/建选项失败**绝不能**让门挂掉 —— 选项没有就退回老界面（老行为不变）。
        repairs, needs, limit_note = [], [], ""
        try:
            repairs, needs, limit_note = self._repair_options(
                {"wbs": candidate}, params, issues)
        except Exception:
            repairs, needs, limit_note = [], [], ""
        payload = {
            "pause_id": pid, "node": self.name,
            "output_summary": f"评审发现 {len(issues)} 条高优先级问题，请决定",
            "issues": shown,
            # 兼容字段：给不认 issues 的消费者（旧终端 / 日志）留一条摘要
            "context_summary": " · ".join(
                f"[{x['severity']}]{x['dimension']}: {x['finding']}" for x in shown),
        }
        # 只增不改：repairs 缺失/为空时，老终端与老渲染路径完全不受影响。
        if repairs:
            payload["repairs"] = repairs
        if needs:
            payload["issues_need_info"] = needs
        if limit_note:
            payload["repair_limit_note"] = limit_note
        # 上一次"一键修复"改了什么（选了编号之后再来一轮时回显给用户）
        if self.last_repair and self.last_repair.get("line"):
            payload["repair_done"] = self.last_repair["line"]
        payload["retry_used"] = self.retry_used
        payload["retry_limit"] = MAX_RETRY_ON_HUMAN
        # §D1：复评门同样要带 WBS 树（阶段→工作包→叶子，限流 6 阶段 / 40 叶子），
        # 用户才看得见"评审到底在说什么结构"。取不到就不带该字段，绝不让门挂掉。
        try:
            tree = wbs_tree_payload(candidate)
        except Exception:
            tree = None
        if tree is not None:
            payload["wbs_tree"] = tree
        self.emit(EV_NODE_PAUSED, payload)
        decision = self._registry.wait(pid, cancel_evt=getattr(self, "_cancel_evt", None), timeout=GATE_TIMEOUT_SECONDS)
        action = decision.get("action", "continue")
        if action == "abort":
            return {"action": "abort"}
        # 一键修复：选了编号（repair_key）→ 交给 _repair_gate 真的重做。
        # 默认选项的 label 同时放进 raw：万一 repair_key 不被下游认识，
        # 就退化成"这条自由意见"（老路径），不会崩、也不会静默什么都不做。
        repair_key = str(decision.get("repair_key") or "").strip()
        if repair_key:
            label = str(decision.get("instruction") or decision.get("manual_input") or "")
            return {"action": "repair", "repair_key": repair_key,
                    "note": f"【人工·一键修复】{label}", "raw": label}
        # 指令（instruction）与编辑器（edits）统一视为人工意见原文
        raw = decision.get("instruction") or decision.get("raw") or ""
        if raw:
            if action == "edit":                       # 键=值式编辑 → 转为意见文本交给主体LLM
                raw = "按下列修改落实：" + json.dumps(decision.get("edits"), ensure_ascii=False)
            return {"action": "revise", "note": f"【人工】{raw}", "raw": str(raw)}
        # 无附带意见 → 视为继续（Y / 空 / continue）
        return {"action": "approve"}

    # 启用逐相/总览 LLM 的条件：注入过显式 llm（测试/调用方给 stub 或已配好 key 的客户端），
    # 或配置了 QWEN 密钥（生产路径）。两者皆无 → 走模板兜底，避免无 key 每相空打网络。
    @property
    def llm_usable(self):
        try:
            if self.llm is not None:
                return True
            return bool(config.LLM_API_KEY)
        except Exception:
            return False