# -*- coding: utf-8 -*-
"""域 5 · 第 27 个节点 `quantity_fill`（补全各工序工程量）

设计：`docs\\域5_补量与冻结_实现设计.md`（§3 的 5.1–5.7、§4 契约、§6 换算插入点、
§7 优先级、§8 冻结、§9 未入树清单、§14.1 父代理裁决 8 条）。

**一句话职责**：在 `WBSAuditNode`(R1) 与 `NormBindNode` 之间，把"每个 L4 的工程量"
从"占比表已拆好的 + 既有系数路径已算好的"扩展成**闭集逐个表态**，并把最终量**冻结在叶子上**。

三段式（与设计 §3.1 逐字对应）：

    段 A（占比表三工种）——**不算新数，只做确认 + 记账**：L4 量 = Σ(该 L4 全部叶子量, fsum)。
    段 B（其余所有 L4）——闭集逐个表态：用户已给 → 用用户值（不发给 LLM）；
                          树里已有量（既有系数路径）→ 用叶子量和（不发给 LLM）；
                          其余才发给模型（分批 25 条，漏项只重问漏的那几条，最多 2 轮）。
    段 C（换算 + 覆盖 + 冻结）——换到目标单位（= 字典单位）→ user > ratio > tree > llm
                          选一个 → 写回叶子 + 记 `ctx["quantity_coverage"]`。

五条不改写既有数值的护栏（这是本节点最容易把产品弄坏的地方，逐条都有测试）：

    1. `source ∈ {"ratio","tree"}` 且**单位没变**时，叶子量**一个字节都不重写** ——
       只补 `_qty_frozen` / `_qty_provenance` / `provenance.quantity`。
       （`test_unit_area_volume.py` 有「工程量一个字都不许改」的断言。）
    2. **同单位不重写、不 `round`**（父代理裁决 #5；`from == to` 时原值返回）。
    3. 换算不出来**绝不 1:1 兜底** → `unit_unresolved` + 中文 hint，量原样保留。
    4. LLM 量**不得**覆盖既有系数路径（`tree` 排在 `llm` 之前）。
    5. 本节点**不产生也不删除工序**（不新造叶子）：跑前跑后 `[l["id"] for l in leaves]`
       逐位相同（域 4 的编号冻结接口承诺）。

另：`backend\\prompts\\quantity_fill.txt` 的 `load()` 一律用 `try/except OSError` 包住 ——
`beat_node.py:317` 那个 `load("beat_config.txt")` 的 `FileNotFoundError` 被
`except (LLMError, Exception): pass` 吞掉的坑（设计 §11.2-1）在这里**不复刻**。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import kb_units
from .. import quantity_scope as qs
from ..base import BaseNode
from ..llm import LLMClient, LLMError

#: 提示词文件名（**另一代理辖区**，本文件只读它）。
PROMPT_NAME = "quantity_fill.txt"

#: `_qty_frozen_by` 是**常量串**（不写 `datetime.now()`，否则"重跑逐位一致"立不住）。
FROZEN_BY = "quantity_fill"

#: 喂给模型的**项目参数白名单**（只喂闭集 + 必要上下文，不喂整棵树）。
PARAM_KEYS = (
    "building_type", "structure_type", "foundation_type", "floors", "building_count",
    "total_area", "total_concrete", "total_rebar", "total_formwork", "total_masonry",
    "total_infill_wall", "total_pile", "total_earthwork", "planned_start_date",
)

#: "参数里到底有没有量"的判据用的分项总量键（与 `ratio_scope.GROUP_TOTAL_PARAMS` 的取值域
#: 同一批；`total_area` 是规模基数，有它说明用户确实给了项目参数）。
TOTAL_PARAM_KEYS = ("total_concrete", "total_rebar", "total_formwork", "total_masonry",
                    "total_earthwork", "total_pile", "total_infill_wall", "total_area")


class QuantityAgentNode(BaseNode):
    """补全各工序工程量：闭集逐个表态 + 单位换算 + 用户值覆盖 + 冻结。"""

    name = "quantity_fill"
    title = "补全各工序工程量"
    #: 引擎据此把 `ctx["quantity_warnings"]` 的**逐条原文**随 node_done 上行
    #: （照 `kb_scope.warning_ctx_key` 的既有约定，见 engine._done_payload）。
    warning_ctx_key = "quantity_warnings"

    def __init__(self, llm=None, batch_size=None, max_attempts=None,
                 unit_map=None, norm_units_map=None):
        super().__init__()
        self.llm = llm
        self.batch_size = int(batch_size or qs.BATCH_SIZE)
        self.max_attempts = int(max_attempts or qs.BATCH_MAX_ATTEMPTS)
        # 目标单位（字典单位）与定额分母单位集合的手工注入口 —— **只给离线单测用**：
        # 注入了就完全按注入的走、不读库（保证测试不依赖 kb.db 的具体内容）。
        self.unit_map = dict(unit_map) if isinstance(unit_map, Mapping) else None
        self.norm_units_map = dict(norm_units_map) if isinstance(norm_units_map, Mapping) else None
        self.warning_note = ""

    # ---------------- LLM 可用性（与 NormBindNode 同规） ----------------
    def _llm(self):
        """惰性创建 `LLMClient` —— 只有真的要调 LLM 时才会走到这里。"""
        if self.llm is None:
            self.llm = LLMClient()
        return self.llm

    @property
    def llm_usable(self) -> bool:
        """只有"显式注入了 LLM 客户端"才算可用（刻意不回退到 `config.LLM_API_KEY`）。

        与 `norm_bind.NormBindNode.llm_usable` 同一条契约：`self.llm is None` 时
        完全走代码路径、**绝不联网**（也让配了 key 的机器上测试依然离线可跑）。
        """
        try:
            return callable(getattr(self.llm, "chat_json", None))
        except Exception:                                   # noqa: BLE001
            return False

    # ==================================================================
    # 入口
    # ==================================================================
    def run(self, ctx):
        ctx = ctx if isinstance(ctx, dict) else {}
        wbs = ctx.get("wbs") or {}
        params = ctx.get("extracted_params") if isinstance(ctx.get("extracted_params"), dict) else {}
        closed = qs.closed_l4_set(ctx.get("kb_scope"))
        tree = qs.in_tree_l4_index(wbs)
        all_leaves = qs.audit_leaves(wbs)
        self.warning_note = ""

        warnings: List[str] = []
        degradations: List[Dict[str, Any]] = []
        coverage = self._skeleton(params, closed, tree)

        # ---- 闭集为空（kb_scope 缺失）：不 `_stop`，如实写"无可用工序范围" ----
        if not closed:
            coverage["summary"]["closed_total"] = 0
            coverage["warnings"] = []
            self.done_summary = "无可用工序范围（kb_scope 为空），跳过工程量补量"
            return {"quantity_coverage": coverage, "quantity_warnings": []}

        self.emit("node_progress", {"node": self.name, "progress": 5,
                                    "message": "开始核对 %d 条工序（闭集）的工程量"
                                               % len(closed)})

        # ---- 极端情形（父代理裁决 #1 ★② 的唯一 `_stop` 判据）----
        # 「树内叶子量为 0 的条数 == 树内叶子总数」**且**模型也没法补（无 key / 提示词缺失）
        # 且参数里一条量都没有 —— 这时继续下去只会产出一份"每条都是 0"的假计划。
        # 只要模型可用，就**绝不 `_stop`**：闭集里 394/408 本来就不在树里，
        # 把缺口升级成中断会把每次真实运行都按在同一个门上（门变死路）。
        if self._plan_has_no_quantity(all_leaves, params) and not self._model_path_usable():
            coverage["summary"]["closed_total"] = len(closed)
            coverage["summary"]["gap_count"] = len(closed)
            coverage["summary"]["gap_ids"] = sorted(closed)
            coverage["warnings"] = ["整份计划没有任何工程量，且模型补量路径不可用"]
            self.done_summary = "没有任何工程量可用（无分项总量、无逐工序量、模型路径不可用）"
            return {
                "_stop": "没有任何工程量可用：项目参数里没有分项总量，也没有逐工序量，"
                         "且模型补量路径不可用。请补充混凝土/钢筋/模板等总量后重跑。",
                "quantity_coverage": coverage,
                "quantity_warnings": coverage["warnings"],
            }

        pctx = self._convert_ctx(params)

        # ---- 段 B 第一步：算出"哪些 L4 需要问模型"（user / ratio / tree 都不问）----
        # `prior` = 上一轮产物里逐 L4 的账（存在即**复用**、不再问模型 —— 这就是冻结的可复现语义）。
        # `upstream` = **不是本节点上一轮写出来**的叶子：把"上一轮补的量"排除掉，
        # 否则重跑时它会被当成"既有系数路径的量"，`_qty_provenance` 从 llm 退回 tree。
        prior, reused = self._prior_items(ctx.get("quantity_coverage"))
        user_q = qs.user_l4_quantities(params)
        need: List[str] = []
        for aid in sorted(closed):
            leaves = self._upstream_leaves(tree.get(aid) or [])
            if self._prior_value(prior.get(aid)) is not None:
                continue
            if qs.fill_source_of(aid, params, leaves, prior.get(aid)) is None:
                need.append(aid)
        pending = list(need)

        model_calls = 0
        retry_batches = 0
        llm_available = self._model_path_usable()
        prompt_note = ""
        raw_answers: Dict[str, Dict[str, Any]] = {}
        for aid, it in prior.items():
            if isinstance(it, Mapping):
                raw = it.get("llm_raw")
                if isinstance(raw, Mapping):
                    raw_answers[aid] = dict(raw)
        #: **模型这条路对这条 L4 走不通**（无客户端 / 提示词缺失 / 该批调用失败）——
        #: 它们记 `model_unavailable`；模型确实响应了、只是漏了这条，才记 `unstated_*`。
        unavailable: set = set()

        if pending and self.llm_usable:
            system, prompt_note = self._load_prompt()
            if system is None:
                llm_available = False
                warnings.append(prompt_note)
                degradations.append({"code": "prompt_missing",
                                     "activity_ids": sorted(pending),
                                     "note": prompt_note})
                unavailable.update(pending)
            else:
                got, calls, retries, deg, failed = self._ask_all(system, pending, params,
                                                                 closed, tree)
                raw_answers.update(got)
                model_calls += calls
                retry_batches += retries
                degradations.extend(deg)
                unavailable.update(failed)
        elif pending:
            unavailable.update(pending)
        if pending and not llm_available and not prompt_note:
            warnings.append("模型补量路径不可用（未注入可用的 LLM 客户端）："
                            "%d 条 L4 未能由模型表态，量沿用既有值（不置 0、不编数）"
                            % len(pending))
        unavailable -= set(raw_answers)          # 有过表态的（含上一轮复用）不算"走不通"

        # ---- 段 A/B/C 主循环（逐 L4，顺序全确定：sorted(activity_id)）----
        rows = []
        for aid in sorted(closed):
            rows.append(self._decide_one(aid, closed[aid], tree, params, user_q,
                                         raw_answers, prior, pctx, unavailable,
                                         degradations, warnings))

        coverage["l4"] = {r["activity_id"]: r for r in rows}
        coverage["l4_rows"] = rows
        coverage["not_in_tree"] = self._not_in_tree(rows, ctx, closed, tree)
        coverage["unknown_user_ids"] = sorted(aid for aid in user_q if aid not in closed)
        coverage["degradations"] = degradations
        coverage["warnings"] = warnings
        coverage["summary"].update(self._summary(rows, closed, tree, params, coverage,
                                                model_calls, llm_available, retry_batches,
                                                reused))
        coverage["summary"]["prompt_note"] = prompt_note
        gaps = qs.coverage_gaps(coverage)

        # ---- 终端：一句人话 + 缺口警告（缺口**必须**上进 meta 与交付物，不只写日志）----
        s = coverage["summary"]
        self.done_summary = (
            "已补全 %d 条工序工程量（闭集）：占比表 %d 条 / 既有系数 %d 条 / 模型补量 %d 条 / "
            "用户指定 %d 条；其中 %d 条未进入 WBS（清单见交付物「工程量来源与未入树清单」）"
            % (s["closed_total"], s["by_source"][qs.SOURCE_RATIO], s["by_source"][qs.SOURCE_TREE],
               s["by_source"][qs.SOURCE_LLM], s["by_source"][qs.SOURCE_USER],
               len(coverage["not_in_tree"])))
        if gaps:
            msg = ("%d 条工序没有拿到工程量（模型未表态 / 无依据）：%s"
                   % (len(gaps), "、".join(r.get("activity_name") or r.get("activity_id")
                                          for r in gaps[:3])))
            warnings.append(msg)
            self.warning_note = "…其余 %d 条同类" % (len(gaps) - min(3, len(gaps)))
            self.emit("warning", {"node": self.name, "message": msg})
        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": self.done_summary})
        return {"quantity_coverage": coverage, "quantity_warnings": list(warnings)}

    # ==================================================================
    # 段 C：单个 L4 的判定 + 冻结
    # ==================================================================
    def _decide_one(self, aid, item, tree, params, user_q, raw_answers, prior, pctx,
                    unavailable, degradations, warnings) -> Dict[str, Any]:
        """一个 L4 的**完整一行账**：来源 → 换算 → 覆盖 → 冻结 → `coverage.l4[aid]`。"""
        leaves = list(tree.get(aid) or [])
        has_leaves = bool(leaves)
        upstream = self._upstream_leaves(leaves)
        target_unit, unit_evidence, unit_note = self._target_unit(aid)
        if unit_evidence == "unresolved":
            warnings.append("「%s」%s" % (item["activity_name"], unit_note))

        u_override = qs.user_override_of(params, aid)          # 用户**显式覆盖**（与占比表值不同）
        u_any = user_q.get(aid)
        ratio_q = qs.ratio_quantity_of(params, aid)
        lsum = qs.leaf_qty_sum(upstream)                       # 只用上游叶子（不含我们上一轮写的）
        leaf_srcs = qs.leaf_qty_source_set(upstream)
        tree_ok = lsum > qs.QTY_ZERO_TOL
        if ratio_q is None and tree_ok and qs.LEAF_SOURCE_RATIO in leaf_srcs:
            # 段 A 的情形：树里的叶子量本身就是占比表拆出来的（量 = Σ叶子量）
            ratio_q = lsum
        prior_item = prior.get(aid) if isinstance(prior, Mapping) else None
        prior_q = self._prior_value(prior_item)
        ans = raw_answers.get(aid)
        ans = dict(ans) if isinstance(ans, Mapping) else None
        ans_ok = bool(ans) and ans.get("in_project") is True \
            and qs.is_finite_number(ans.get("quantity")) and float(ans["quantity"]) > qs.QTY_ZERO_TOL

        # ---- 候选（优先级 user > ratio > tree > llm；"复用上一轮产物"插在 user 之后）----
        chosen: Optional[Tuple[str, float, str, str, str, str]] = None   # source,q,method,ev,note,from_unit
        if u_override is not None:
            ut = target_unit
            q, m, ev, nt = qs.convert_to_target(u_override, ut, ut, pctx)
            chosen = (qs.SOURCE_USER, q, m, ev, nt, ut)
        elif prior_item is not None and prior_q is not None:
            # 上一轮产物里有这一条 → **原样复用**（"量已冻结"的可复现语义；
            # 这也是重跑"0 次模型调用 + 叶子逐字节相同"的来源）。
            method = qs.as_text(prior_item.get("convert_method"))
            ev = "ok" if method else "dict"
            chosen = (qs.as_text(prior_item.get("source")) or qs.SOURCE_NONE,
                      prior_q, method or "同单位", ev, "",
                      qs.as_text(prior_item.get("from_unit")) or target_unit)
        elif ratio_q is not None:
            fu = self._ratio_unit(params, aid) or target_unit
            q, m, ev, nt = qs.convert_to_target(ratio_q, fu, target_unit, pctx)
            chosen = (qs.SOURCE_RATIO, q, m, ev, nt, fu)
        elif tree_ok:
            fu = self._leaf_unit(upstream) or target_unit
            q, m, ev, nt = qs.convert_to_target(lsum, fu, target_unit, pctx)
            chosen = (qs.SOURCE_TREE, q, m, ev, nt, fu)
        elif ans_ok:
            fu = qs.as_text(ans.get("unit")) or target_unit
            q, m, ev, nt = qs.convert_to_target(ans.get("quantity"), fu, target_unit, pctx)
            chosen = (qs.SOURCE_LLM, q, m, ev, nt, fu)

        status = qs.STATUS_QUANTIFIED
        source = qs.SOURCE_NONE
        qty: Optional[float] = None
        method = ""
        ev = ""
        unit_note2 = ""
        overridden = None

        if chosen is not None:
            source, q, method, ev, nt, _fu = chosen
            if ev in ("unresolved", "rejected"):
                # 量**原样保留、不阻断**（设计 §5.3 规则 1 / §4.5 第 4 行）
                status = qs.STATUS_UNIT_UNRESOLVED
                unit_note2 = nt or qs.as_text(prior_item.get("unit_note")) \
                    if isinstance(prior_item, Mapping) else nt
            qty = round(float(q), 6)
        else:
            # 没有选出量 → 未表态 / 明确不适用（**不报错、不 `_stop`**）
            if ans is not None and ans.get("in_project") is False:
                status = qs.STATUS_NOT_APPLICABLE
            else:
                # 模型"表态了但没给合式的数"（提示词的兜底 #3）→ 记 unstated_*，
                # 只有**模型这条路对这条走不通**（无客户端 / 提示词缺失 / 该批调用失败）
                # 时才记 model_unavailable（设计 §5.2 的那张表）。
                # `tree` / `absent` 的判据是"**树里有没有叶子**"（不是"叶子量是否 > 0"）：
                # 树里有叶子但量为 0 → `unstated_tree` + 量 0，它会被缺口判据抓出来。
                known = ans is not None or aid not in (unavailable or ())
                status = (qs.STATUS_UNSTATED_TREE if has_leaves else qs.STATUS_UNSTATED_ABSENT) \
                    if known else qs.STATUS_MODEL_UNAVAILABLE
                source = qs.SOURCE_TREE if has_leaves else qs.SOURCE_NONE
                qty = round(lsum, 6) if has_leaves else None

        # ---- 覆盖记账（被覆盖的原值与来源，一步都不能省）----
        # ⚠️ 只认**上游**的占比表 / 系数路径现值。上一轮产物里的定稿量**不算被覆盖**
        #    （那是同一次冻结的复用，不是新的覆盖）—— 否则重跑会凭空多出 `_qty_override`，
        #    "重跑逐位一致"当场破功。
        before_source = qs.SOURCE_NONE
        before_value: Optional[float] = None
        if ratio_q is not None:
            before_source, before_value = qs.SOURCE_RATIO, round(float(ratio_q), 6)
        elif tree_ok:
            before_source, before_value = qs.SOURCE_TREE, round(lsum, 6)
        if source in (qs.SOURCE_USER, qs.SOURCE_LLM) and before_source != qs.SOURCE_NONE:
            overridden = {"source": before_source, "value": before_value}

        # ---- 写回叶子（唯一写点）+ 冻结标记 ----
        rewrote = self._freeze(aid, leaves, source, qty, target_unit, method, ev,
                               ans, overridden, degradations, params)
        # `leaf_sum` 恒等于"该 L4 在树里的叶子量之和"；树里没有叶子 → `None`
        # （不许把 L4 层的量冒充成叶子量和，那会让交付物里两列看起来自相矛盾）。
        leaf_sum = qs.leaf_qty_sum(leaves) if has_leaves else None

        return {
            "activity_id": aid,
            "activity_name": item["activity_name"],
            "work_type_id": item["work_type_id"],
            "work_type_name": item["work_type_name"],
            "in_tree": bool(leaves),
            "leaf_ids": [qs.leaf_id(l) for l in leaves],
            "quantity": qty,
            "leaf_sum": (round(leaf_sum, 6) if qs.is_finite_number(leaf_sum) else None),
            "unit": target_unit or (self._leaf_unit(leaves) or ""),
            "from_unit": (chosen[5] if chosen is not None else ""),
            "convert_method": method if ev == "ok" else "",
            "unit_evidence": unit_evidence,
            "unit_note": unit_note2 or unit_note,
            "status": status,
            "source": source,
            "formula": self._formula(aid, source, params, leaves, ans),
            "llm_raw": ans,
            "user_raw": ({"value": float(u_override), "unit_assumed": target_unit}
                         if u_override is not None else
                         ({"value": float(u_any), "unit_assumed": target_unit,
                           "note": "值 ≤ 容差，未覆盖（ignored_user_ids）"}
                          if u_any is not None else None)),
            "overridden": overridden,
            "rewrote_leaves": bool(rewrote),
            "specs": {
                "applicability_level": item.get("applicability_level"),
                "structure_mapping_absent": item.get("structure_mapping_absent"),
                "recommended_production_mode": item.get("production_mode"),
                "labor_type": item.get("labor_type"),
            },
        }

    # ==================================================================
    # 叶子写入（唯一写点）
    # ==================================================================
    def _freeze(self, aid, leaves, source, qty, target_unit, method, ev, ans,
                overridden, degradations, params) -> bool:
        """把定稿量写回叶子并打冻结标记。返回**是否真的改写了叶子量**。

        ⚠️ 护栏：`source ∈ {"ratio","tree"}` 且单位没变（`ev == "dict"`）时**不重写数值** ——
        只补标记（见模块 docstring 第 1 条）。只有这三种情形才写回数值：
          · `source ∈ {"user","llm"}`（覆盖/补量本来就是要改数）；
          · 单位换算**真的换了**（`ev == "ok"` 且 method != "同单位"）；
        """
        if not leaves:
            return False
        rewrite = bool(source in (qs.SOURCE_USER, qs.SOURCE_LLM)
                       or (ev == "ok" and method != "同单位" and qs.is_finite_number(qty)))
        if rewrite and qs.is_finite_number(qty):
            qmap, uniform = qs.distribute_to_leaves(leaves, qty, target_unit)
            if uniform and len(leaves) > 1:
                degradations.append({
                    "code": ("llm_qty_uniform_split" if source == qs.SOURCE_LLM
                             else "qty_uniform_split"),
                    "activity_id": aid,
                    "note": "几何（段面积）取不到，%d 条叶子按均分落量" % len(leaves),
                })
            for leaf in leaves:
                q2 = qmap.get(qs.leaf_id(leaf))
                if q2 is None:
                    continue
                leaf["quantity"] = float(q2)
                if ev == "ok" and method != "同单位" and target_unit:
                    leaf["unit"] = target_unit
        self._mark_leaves(leaves, source, target_unit, ans, overridden, params)
        return rewrite

    def _mark_leaves(self, leaves, source, target_unit, ans, overridden, params):
        """逐条叶子打"量已定稿"标记 + 溯源（**不改数值**）。"""
        for leaf in leaves:
            leaf["_qty_frozen"] = True
            leaf["_qty_frozen_by"] = FROZEN_BY
            leaf["_qty_provenance"] = source
            if overridden:
                leaf["_qty_override"] = dict(overridden)
            if ans is not None:
                leaf["_llm_qty"] = {
                    "quantity": ans.get("quantity"), "unit": ans.get("unit"),
                    "reason": ans.get("reason"), "in_project": ans.get("in_project"),
                    "basis": ans.get("basis"), "confidence": ans.get("confidence"),
                    "batch": ans.get("batch"), "attempt": ans.get("attempt"),
                }
            # `_qty_source` 的**取值域不变**（仍只有"占比表拆分/参数推算/基线默认"）：
            # 模型补的量写 `参数推算` + `_qty_provenance="llm"` 区分（设计 §5.5）。
            if source == qs.SOURCE_LLM:
                leaf["_qty_source"] = qs.LEAF_SOURCE_PARAM
            elif source == qs.SOURCE_USER:
                if leaf.get("_qty_source") not in (qs.LEAF_SOURCE_RATIO, qs.LEAF_SOURCE_PARAM,
                                                   qs.LEAF_SOURCE_BASE):
                    leaf["_qty_source"] = qs.LEAF_SOURCE_PARAM
            # 量级自检（与 beat_configs._qty_suspect 同一对字段：**如实标记、绝不改数**）
            if source == qs.SOURCE_LLM:
                self._mark_suspect(leaf, params)
            prov = leaf.get("provenance")
            if not isinstance(prov, dict):
                prov = {}
            if source in (qs.SOURCE_USER, qs.SOURCE_LLM) or not isinstance(prov.get("quantity"), dict):
                prov["quantity"] = {
                    "value": leaf.get("quantity"),
                    "origin": qs.ORIGIN_OF_SOURCE.get(source, "unknown"),
                    "ref": self._prov_ref(source, ans),
                    "confidence": (qs.as_text((ans or {}).get("confidence")) or
                                   ("高" if source == qs.SOURCE_USER else "中")),
                    "note": self._prov_note(source, ans),
                }
                leaf["provenance"] = prov

    @staticmethod
    def _mark_suspect(leaf, params):
        """LLM 量超"按项目规模推算的合理上界"时打 `_qty_suspect`（**如实标记、绝不改数**）。

        `scheduler.quantity_scale_bounds` / `scale_violation` 是同一口径的既有实现，
        **惰性 import**：这个标记是"锦上添花"，不该为了它把节点的 import 图拉长，
        更不该在拿不到时让补量失败。
        """
        try:
            from .scheduler import quantity_scale_bounds, scale_violation
            reason = scale_violation({"unit": leaf.get("unit"),
                                      "quantity": leaf.get("quantity")},
                                     quantity_scale_bounds(params))
        except Exception:                                    # noqa: BLE001
            return
        if reason:
            leaf["_qty_suspect"] = True
            leaf["_qty_suspect_reason"] = reason

    @staticmethod
    def _prov_ref(source, ans) -> str:
        if source == qs.SOURCE_USER:
            return "用户输入（extracted_params.l4_quantities）"
        if source == qs.SOURCE_RATIO:
            return "Component_Ratio 占比表拆分"
        if source == qs.SOURCE_LLM:
            return "模型补量（prompts/quantity_fill.txt）"
        if source == qs.SOURCE_TREE:
            return "既有系数/参数路径（节拍引擎叶子量）"
        return "来源未标注"

    @staticmethod
    def _prov_note(source, ans) -> str:
        if source == qs.SOURCE_LLM and isinstance(ans, Mapping):
            return "模型补量原话：%s" % (qs.as_text(ans.get("reason")) or "（未给理由）")
        if source == qs.SOURCE_USER:
            return "用户显式指定的逐 L4 工程量（无单位，按该 L4 字典单位解释）"
        return ""

    # ==================================================================
    # 目标单位 / 换算 ctx / 公式
    # ==================================================================
    def _target_unit(self, aid) -> Tuple[str, str, str]:
        """该 L4 的目标单位（= 字典单位）+ 定额侧交叉校验证据。"""
        if self.unit_map is not None:
            nu = None if self.norm_units_map is None else self.norm_units_map.get(aid)
            return qs.norm_denominator_unit(aid, dict_unit=self.unit_map.get(aid, ""),
                                            norm_units=nu)
        return qs.norm_denominator_unit(aid)

    @staticmethod
    def _convert_ctx(params) -> Dict[str, float]:
        """换算 ctx —— **只能**来自已有的具名通道：`extracted_params` 里的
        `kb_units.CONTEXT_KEYS` 那 5 个键（逐键取值，与 `norm_bind._resolve_convert_ctx`
        的"①' 项目参数按键名取值"同一份来源）。

        **禁止**在这里给任何全局默认值（D5 已裁定：`kb_units.DEFAULT_WALL_THICKNESS_M`
        只作兼容导出，不再作兜底）。拿不到就 `unresolved`，如实报缺。
        """
        out: Dict[str, float] = {}
        for k in kb_units.CONTEXT_KEYS:
            v = (params or {}).get(k)
            if qs.is_finite_number(v) and float(v) > 0:
                out[k] = float(v)
        return out

    @staticmethod
    def _plan_has_no_quantity(all_leaves, params) -> bool:
        """树里有叶子、**每条量都是 0**、参数里既没有逐 L4 量也没有分项总量。

        这是父代理裁决 #1 ★② 那条"唯一允许的 `_stop`"的判据（口径与设计 §4.5 的极端情形
        一致）：继续跑只会产出一份"每条都是 0"的假计划。**只要模型这条路可用，就绝不 `_stop`**
        —— 闭集里 394/408 本来就不在树里，把缺口升级成中断会让每次真实运行都停在同一个门上。
        """
        if not all_leaves:
            return False
        if any(qs.leaf_qty(l) > qs.QTY_ZERO_TOL for l in all_leaves):
            return False
        p = params if isinstance(params, Mapping) else {}
        if qs.user_l4_quantities(p):
            return False
        if qs.ratio_l4_index(p):
            return False
        if any(qs.is_finite_number(p.get(k)) and float(p[k]) > 0 for k in TOTAL_PARAM_KEYS):
            return False
        return True

    @staticmethod
    def _upstream_leaves(leaves) -> List[Dict[str, Any]]:
        """**不是本节点上一轮写出来**的那些叶子。

        重复运行时，上一轮补的量已经落在叶子上；如果把它算进"树里已有的量"，
        这条 L4 会被判成 `tree`（既有系数路径），`_qty_provenance` 从 `llm` 退回 `tree`、
        重跑不再逐位一致。判据就是本节点自己写的常量标记 `_qty_frozen_by`。
        """
        return [l for l in (leaves or [])
                if qs.as_text((l or {}).get("_qty_frozen_by")) != FROZEN_BY]

    @staticmethod
    def _prior_value(item) -> Optional[float]:
        """上一轮产物里这一条的定稿量（没有 / 非数 / ≤0 → None）。"""
        if not isinstance(item, Mapping):
            return None
        q = item.get("quantity")
        if not qs.is_finite_number(q) or float(q) <= qs.QTY_ZERO_TOL:
            return None
        return float(q)

    def _model_path_usable(self) -> bool:
        """模型这条路能不能走：注入了客户端 **且** 提示词文件读得到。"""
        if not self.llm_usable:
            return False
        system, _note = self._load_prompt()
        return system is not None

    @staticmethod
    def _ratio_unit(params, aid) -> str:
        row = qs.ratio_l4_index(params).get(aid)
        return qs.as_text((row or {}).get("unit")) if isinstance(row, Mapping) else ""

    @staticmethod
    def _leaf_unit(leaves) -> str:
        for l in leaves or []:
            u = qs.as_text((l or {}).get("unit"))
            if u:
                return u
        return ""

    @staticmethod
    def _formula(aid, source, params, leaves, ans) -> str:
        """"这个数怎么来的"一句话（逐行可溯源是产品的硬卖点）。"""
        if source == qs.SOURCE_RATIO:
            f = qs.ratio_formula_of(params, aid)
            if f:
                return f
            for l in leaves or []:
                if qs.as_text((l or {}).get("_qty_formula")):
                    return "占比表拆分（叶子公式）：%s" % l.get("_qty_formula")
            return "占比表拆分（Component_Ratio）"
        if source == qs.SOURCE_TREE:
            for l in leaves or []:
                if qs.as_text((l or {}).get("_qty_formula")):
                    return "既有系数路径：%s" % l.get("_qty_formula")
            return "既有系数/参数路径（节拍引擎叶子量之和）"
        if source == qs.SOURCE_USER:
            return "用户指定（extracted_params.l4_quantities，按该 L4 字典单位解释）"
        if source == qs.SOURCE_LLM and isinstance(ans, Mapping):
            return "模型补量：%s" % (qs.as_text(ans.get("basis")) or
                                     qs.as_text(ans.get("reason")) or "（未给依据）")
        return ""

    # ==================================================================
    # 段 B：提示词 / 分批 / 重试
    # ==================================================================
    @staticmethod
    def _load_prompt() -> Tuple[Optional[str], str]:
        """读提示词。返回 `(system, note)`；读不到 → `(None, 中文说明)`。

        ⚠️ 必须 `try/except OSError`：`prompts_loader.load` 没有兜底，找不到文件会抛
        `FileNotFoundError`（= OSError）。`beat_node.py:317` 那条"死路径"就是被
        `except (LLMError, Exception): pass` 吞掉的（设计 §11.2-1 / §14.1.1）——
        这里**不复刻**：读不到就明确走"模型不可用"并留警告。
        """
        try:
            from ..prompts_loader import load
            return (load(PROMPT_NAME), "")
        except OSError as e:
            return (None, "提示词文件 backend/prompts/%s 读不到（%s）："
                          "模型补量路径按不可用处理，量沿用既有值"
                          % (PROMPT_NAME, e))
        except Exception as e:                               # noqa: BLE001
            return (None, "提示词加载异常（%s）：模型补量路径按不可用处理" % e)

    def _ask_all(self, system, pending, params, closed, tree):
        """把 `pending` 分批问模型；只重问**未表态/不合式**的那几条，最多 `max_attempts` 轮。

        返回 `(answers, model_calls, retry_batches, degradations, failed_ids)`。
        `answers[aid]` = 模型给的那一条（含 `batch` / `attempt` 留痕）；
        `failed_ids` = **调用本身失败过、且最终没拿到表态**的那些 `activity_id`
        （它们记 `model_unavailable`，与"模型响应了但漏了这条"的 `unstated_*` 分开）。
        """
        answers: Dict[str, Dict[str, Any]] = {}
        degradations: List[Dict[str, Any]] = []
        failed: set = set()
        calls = 0
        retry_batches = 0
        todo = list(pending)
        missed_note: List[str] = []
        for attempt in range(1, self.max_attempts + 1):
            if not todo:
                break
            batches = qs.plan_batches(todo, self.batch_size)
            if attempt > 1:
                retry_batches += len(batches)
            still: List[str] = []
            for bi, batch in enumerate(batches):
                got, err = self._ask_batch(system, batch, params, closed, tree,
                                           attempt, missed_note)
                calls += 1
                if got is None:
                    degradations.append({"code": "llm_batch_failed", "batch": bi,
                                         "attempt": attempt, "activity_ids": list(batch),
                                         "note": err})
                    failed.update(batch)
                    still.extend(batch)
                    continue
                for aid in batch:
                    it = got.get(aid)
                    if isinstance(it, Mapping) and it.get("in_project") is not None:
                        item = dict(it)
                        item["batch"] = bi
                        item["attempt"] = attempt
                        answers[aid] = item
                still.extend([aid for aid in batch if not self._is_answered(answers.get(aid))])
            missed_note = sorted(still)
            todo = still
        if todo:
            degradations.append({"code": "llm_unstated", "attempt": self.max_attempts,
                                 "activity_ids": sorted(todo),
                                 "note": "%d 条 L4 两轮都未表态：不报错、不中断，"
                                         "按 unstated_* 如实记账" % len(todo)})
        return answers, calls, retry_batches, degradations, (failed - set(answers))

    def _ask_batch(self, system, batch, params, closed, tree, attempt, missed_note):
        """问一批。**任何异常都不许抛到引擎**：返回 `(None, 中文原因)`。

        `retries=0` 是刻意的（设计 R4）：`chat_json` 的 `retries` 默认就是 1，
        会在 `chat_text` 里再 `time.sleep(1)` 重试一次；语义层的"漏项再问"是**另一层**，
        两层不许叠乘（`test_llm_retry.py::test_瞬时预算跨payload共享_不与4xx退回叠乘` 钉着）。
        """
        payload = {
            "项目参数": self._param_payload(params),
            "本轮工序（闭集内、待表态）": [self._pending_row(aid, closed[aid], tree)
                                          for aid in batch],
            "已定稿工序（不要重复表态，仅作参考）":
                self._done_rows(closed, tree, params, set(batch)),
        }
        if attempt > 1 and missed_note:
            payload["上一轮你漏掉的条目"] = list(missed_note)
        try:
            raw = self._llm().chat_json(system, json.dumps(payload, ensure_ascii=False),
                                        temperature=0.0, retries=0)
        except LLMError as e:
            return (None, "LLM 调用失败：%s" % e)
        except Exception as e:                               # noqa: BLE001
            return (None, "LLM 调用异常：%s" % e)
        if not isinstance(raw, Mapping):
            return (None, "模型输出不是 JSON 对象：%r" % (raw,))
        items = raw.get("items")
        if not isinstance(items, (list, tuple)):
            return (None, "模型输出缺少 items 数组")
        out: Dict[str, Dict[str, Any]] = {}
        allowed = set(batch)
        for it in items:
            if not isinstance(it, Mapping):
                continue
            aid = qs.as_text(it.get("activity_id")).strip()
            if aid not in allowed:
                continue                       # 多余项丢弃（不进闭集统计，见提示词约束 4）
            out[aid] = dict(it)
        return (out, "")

    @staticmethod
    def _param_payload(params) -> Dict[str, Any]:
        return {k: (params or {}).get(k) for k in PARAM_KEYS
                if (params or {}).get(k) is not None}

    @staticmethod
    def _done_rows(closed, tree, params, exclude) -> List[Dict[str, Any]]:
        """"已定稿工序（仅作参考）"：**已经有量**的那些 L4（占比表 / 既有系数路径）。

        只列真有量的（没有量的正是本轮要问的），带上量与来源 —— 模型据此避免
        把同一件事重复表态（设计 §5.2）。上限 60 行，防止把每批 user 侧撑爆。
        """
        out = []
        for aid in sorted(closed):
            if aid in exclude:
                continue
            q = qs.ratio_quantity_of(params, aid)
            src = qs.SOURCE_RATIO if q is not None else None
            if q is None:
                s = qs.leaf_qty_sum(tree.get(aid) or [])
                if s > qs.QTY_ZERO_TOL:
                    q, src = s, qs.SOURCE_TREE
            if q is None:
                continue
            out.append({"activity_id": aid, "activity_name": closed[aid]["activity_name"],
                        "unit": closed[aid]["unit"], "quantity": round(float(q), 6),
                        "source": ("占比表拆分" if src == qs.SOURCE_RATIO else "既有系数路径")})
            if len(out) >= 60:
                break
        return out

    @staticmethod
    def _pending_row(aid, item, tree) -> Dict[str, Any]:
        leaves = tree.get(aid) or []
        s = qs.leaf_qty_sum(leaves)
        return {
            "activity_id": aid,
            "activity_name": item["activity_name"],
            "unit": item["unit"],
            "work_type_id": item["work_type_id"],
            "work_type_name": item["work_type_name"],
            "recommended_production_mode": item.get("production_mode"),
            "applicability_level": item.get("applicability_level"),
            "structure_mapping_absent": item.get("structure_mapping_absent"),
            "in_tree": bool(leaves),
            "existing_quantity": (round(s, 6) if leaves and s > qs.QTY_ZERO_TOL else None),
            "note": ("闭集内、未被节拍引擎展开" if not leaves else "已入树，量待核对"),
        }

    @staticmethod
    def _is_answered(it) -> bool:
        """`answered` = 模型给了 `in_project`，且（说"没有" **或** 给了正数）。"""
        if not isinstance(it, Mapping):
            return False
        if it.get("in_project") is False:
            return True
        if it.get("in_project") is True:
            q = it.get("quantity")
            return qs.is_finite_number(q) and float(q) > qs.QTY_ZERO_TOL
        return False

    # ==================================================================
    # 账本：coverage 骨架 / 未入树清单 / summary
    # ==================================================================
    @staticmethod
    def _skeleton(params, closed, tree) -> Dict[str, Any]:
        return {
            "source": "quantity_fill",
            "structure_type_id": qs.as_text((params or {}).get("structure_type")),
            "building_type": qs.as_text((params or {}).get("building_type")),
            "target_unit_rule": "L4_Activity_Dictionary.unit（WBS 生成时已选定）",
            "l4": {},
            "l4_rows": [],
            "not_in_tree": [],
            "unknown_user_ids": [],
            "summary": {
                "closed_total": len(closed), "in_tree": sum(1 for a in closed if tree.get(a)),
                "stated": 0, "answered": 0, "retry_batches": 0,
                "by_source": {qs.SOURCE_RATIO: 0, qs.SOURCE_TREE: 0, qs.SOURCE_LLM: 0,
                              qs.SOURCE_USER: 0, qs.SOURCE_NONE: 0},
                "unit_unresolved": 0, "dict_norm_differs": 0,
                "gap_count": 0, "gap_ids": [],
                "ignored_user_ids": qs.ignored_user_ids(params),
                "conservation_note": "", "rounding_note": "",
                "model_calls": 0, "llm_available": False, "reused_previous": False,
                "manual_params_note": "",
            },
            "degradations": [],
            "warnings": [],
        }

    @staticmethod
    def _not_in_tree(rows, ctx, closed, tree) -> List[Dict[str, Any]]:
        """「未入树清单」：闭集里**在 WBS 树中没有任何叶子**的 L4（分母 = 闭集）。

        `reason` 按设计 §9.1 的三类优先级取；第 3 类**直接读** `kb_scope` 既有的
        `l4_excluded_by_quantity` 留痕（不重新跑一遍 `_is_zero`）。
        """
        scope = ctx.get("kb_scope") if isinstance(ctx.get("kb_scope"), dict) else {}
        excluded = {qs.as_text((e or {}).get("activity_id"))
                    for e in (scope.get("l4_excluded_by_quantity") or [])
                    if isinstance(e, Mapping)}
        has_beat = bool(ctx.get("beat_subtrees"))
        out = []
        for r in rows:
            if r["in_tree"]:
                continue
            aid = r["activity_id"]
            if has_beat:
                reason = ("节拍引擎未展开（节拍只对部分分部做流水展开，其余分部按工作包/单条列示）")
            elif (closed.get(aid) or {}).get("structure_mapping_absent"):
                reason = "结构映射缺失（structure_mapping_absent=true），本批不做"
            elif aid in excluded:
                reason = "占比表拆分后量为 0（量 0 出局）"
            else:
                reason = "不在节拍展开范围内（未被任何节拍阶段覆盖）"
            out.append({
                "activity_id": aid,
                "activity_name": r["activity_name"],
                "work_type_id": r["work_type_id"],
                "work_type_name": r["work_type_name"],
                "unit": r["unit"],
                "quantity": r["quantity"],
                "status": r["status"],
                "source": r["source"],
                "reason": reason,
                "llm_raw": r["llm_raw"],
                "note": r["unit_note"] or "",
            })
        return out

    def _summary(self, rows, closed, tree, params, coverage, model_calls,
                 llm_available, retry_batches, reused) -> Dict[str, Any]:
        """把逐行账汇总成交付物要用的那张表（**缺口必须可见**）。"""
        by_source = {qs.SOURCE_RATIO: 0, qs.SOURCE_TREE: 0, qs.SOURCE_LLM: 0,
                     qs.SOURCE_USER: 0, qs.SOURCE_NONE: 0}
        for r in rows:
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        stated = sum(1 for r in rows if r["llm_raw"] is not None
                     or r["source"] in (qs.SOURCE_USER, qs.SOURCE_RATIO, qs.SOURCE_TREE))
        answered = sum(1 for r in rows if r["status"] == qs.STATUS_QUANTIFIED
                       or r["status"] == qs.STATUS_NOT_APPLICABLE
                       or r["status"] == qs.STATUS_UNIT_UNRESOLVED)
        gaps = qs.coverage_gaps(coverage)
        manual = {}
        for k, v in (self._manual_applied(params) or {}).items():
            manual[k] = v
        note = ""
        if by_source[qs.SOURCE_LLM] or by_source[qs.SOURCE_USER]:
            note = ("本节点按覆盖后的版本作为最终工程量，**不做守恒回算**："
                    "不比对 Σ 各 L4 量与分项总量（域 5.5）。"
                    "差额如需核对，请以逐行「量 / 单位 / 来源」为准。")
        return {
            "stated": stated, "answered": answered, "retry_batches": retry_batches,
            "by_source": by_source,
            "unit_unresolved": sum(1 for r in rows
                                   if r["status"] == qs.STATUS_UNIT_UNRESOLVED),
            "dict_norm_differs": sum(1 for r in rows
                                     if r["unit_evidence"] == "dict+norm_differs"),
            "gap_count": len(gaps), "gap_ids": sorted(r["activity_id"] for r in gaps),
            "ignored_user_ids": qs.ignored_user_ids(params),
            "conservation_note": note,
            "rounding_note": ("叶子量按 round(,2) 后 fsum；L4 层按 round(,6)。"
                              "两者可能差几分，判据用叶子量和，**不回填**。"),
            "model_calls": model_calls, "llm_available": bool(llm_available),
            "reused_previous": bool(reused),
            "manual_params_note": ("用户在参数门上确认过的键：%s（**只作说明性留痕**，"
                                   "不作为逐 L4 量的覆盖判据）" % "、".join(sorted(manual))
                                   if manual else ""),
            "text": ("闭集 %d 个 L4，进树 %d 个，未入树 %d 个"
                     % (len(closed), sum(1 for a in closed if tree.get(a)),
                        len(closed) - sum(1 for a in closed if tree.get(a)))),
        }

    @staticmethod
    def _manual_applied(params) -> Dict[str, Any]:
        m = (params or {}).get("manual_params_applied")
        return dict(m) if isinstance(m, Mapping) else {}

    @staticmethod
    def _prior_items(prev) -> Tuple[Dict[str, Dict[str, Any]], bool]:
        """上一轮产物里逐 L4 的账（存在即**复用**：这就是冻结的可复现语义）。

        设计 §5.5 第 2 条：再次运行时若 `ctx` 里已有 `quantity_coverage`
        （`tools\\replay_plan.py` 已经会把 `meta["kb_scope"]` 搬回 ctx，同一手法）
        → **整段跳过 LLM**，直接复用上一轮的定稿量。
        """
        if not isinstance(prev, Mapping):
            return ({}, False)
        l4 = prev.get("l4")
        if not isinstance(l4, Mapping) or not l4:
            return ({}, False)
        out = {qs.as_text(aid): dict(it) for aid, it in l4.items() if isinstance(it, Mapping)}
        return (out, bool(out))
