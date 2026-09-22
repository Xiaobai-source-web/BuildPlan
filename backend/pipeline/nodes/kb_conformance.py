"""节点辅助：知识库范围一致性校验（纯代码，不调用任何 LLM）。

## 这个模块解决什么问题

`kb_scope`（第 7 个节点）会按「建筑类型 + 结构形式」算出一份**合法工序范围**
（`l4_candidates`：每个 L3 下允许出现哪些 L4）。上游把这个范围喂给 WBS 生成的
提示词，并且把被结构剔除的工序明确写成"严禁使用"清单。

问题在于：**那只是提示词层面的纪律，没有任何代码在事后核对。**

实测出来的后果（本模块的立项理由，不是假设）：

  · `beat_configs.BASE_BEAT_CONFIGS["二次结构与砌体"]` 里，"构造柱浇筑" 这条工序
    硬编码挂了 `kb_activity_id = "CONC_NEW_COLUMN"`（柱浇筑）。而知识库的
    `Structure_Type_L4_Mapping` 里，`CONC_NEW_COLUMN` 在**剪力墙结构下是 EXCLUDED**。
  · 节拍展开（`beat_node` → `layer_engine`）用的是配置，**根本不看 `kb_scope`**，
    也不经过 WBS 生成的提示词 —— 于是这条被明令禁止的工序在剪力墙住宅项目里
    照样进了计划，还带着编号一路走到定额锚定、排程、资源。
  · 全程没有一处报警。这正是"提示词纪律代替代码约束"的典型失效。

所以本模块补上**事后核对**：拿树与 `kb_scope` 对照，把"树里有、范围里没有"的
`kb_activity_id` 逐条抓出来，交给上游复评门（可以真的重做那一相）与 R1 审计门
（让用户看得见）。

## 判定口径（只用现成数据，不猜）

对树里**每一个**显式挂了 `kb_activity_id` 的叶子，取该活动在 KB 里所属的 L3
（`kb.l3_of_activity`），再按下面四条判定：

  ① 活动所属 L3 在 `scope["excluded_l3"]` 里（被建筑类型排除）→ `banned`
  ② 活动所属 L3 在 `l4_candidates` 里但该活动不在其清单中（被结构形式剔除）→ `banned`
  ③ 活动所属 L3 不在本工程的范围内（既没被选、也没被排除）→ `other_l3`
  ④ KB 里查不到这个活动编号（模型编的/写错的）→ `unknown`

**没有挂 `kb_activity_id` 的叶子一律不算违规。** 这是刻意的：KB 活动编号只对
"能锚定定额的结构主体工序"是必须的，临建、措施、验收这类工序本来就没有对应
活动，按"缺编号"报违规会把正常工程判成违规（宁可少报，不可误报）。

`unknown` 单独成一类而不是并进 `banned`：它不是"范围违规"，而是"编号对不上库"，
修法完全不同（前者重做 WBS，后者改编号或补库）。
"""

# 违规种类（顺序即展示优先级）
KIND_BANNED = "banned"          # ① ② 范围明确禁止
KIND_OTHER_L3 = "other_l3"      # ③ 不在本工程范围内
KIND_UNKNOWN = "unknown"        # ④ KB 里没有这个活动编号

_KIND_LABEL = {
    KIND_BANNED: "结构/建筑类型下不适用（严禁使用）",
    KIND_OTHER_L3: "不在本工程知识库范围内",
    KIND_UNKNOWN: "知识库里没有这个活动编号",
}

# 一条 findings 明细塞进提示词/事件时的条数上限（防止一屏事故）
DEFAULT_LIMIT = 20


def _as_text(value):
    """None 安全地转字符串。"""
    return "" if value is None else str(value)


def _scope_applied(scope):
    """这份 scope 是否真的做过范围装配（而不是空壳 / 没跑到 kb_scope）。

    判据只有一条：`l4_candidates` 是非空字典。kb_scope 正常产出时它必然非空
    （31 个 L3 各有一份清单）；取不到就是"本节点没跑或跑挂了"，
    此时**不能**把满树的编号全判成违规 —— 那会把"上游没运行"误报成"计划违规"。
    """
    if not isinstance(scope, dict):
        return False
    cand = scope.get("l4_candidates")
    return isinstance(cand, dict) and bool(cand)


def _iter_leaves(wbs):
    """遍历树的全部叶子 → `(阶段名, 工作包名, 叶子 dict)`。"""
    for ph in (wbs or {}).get("phases") or []:
        if not isinstance(ph, dict):
            continue
        phase_name = _as_text(ph.get("phase"))
        for wp in ph.get("work_packages") or []:
            if not isinstance(wp, dict):
                continue
            wp_name = _as_text(wp.get("name"))
            for leaf in wp.get("sub_packages") or []:
                if isinstance(leaf, dict):
                    yield phase_name, wp_name, leaf


def _allowed_index(scope):
    """把 `l4_candidates` 压成 `{activity_id: work_type_id}`（白名单索引）。"""
    allowed = {}
    for wt, items in (scope.get("l4_candidates") or {}).items():
        for it in items or []:
            if isinstance(it, dict) and it.get("activity_id"):
                allowed[_as_text(it["activity_id"])] = _as_text(wt)
    return allowed


def _excluded_index(scope):
    """被建筑类型排除的 L3 → `{work_type_id: 原因}`。"""
    out = {}
    for it in scope.get("excluded_l3") or []:
        if isinstance(it, dict) and it.get("work_type_id"):
            out[_as_text(it["work_type_id"])] = _as_text(it.get("reason"))
    return out


def scope_audit(scope):
    """A4/A5/A6 的**范围审计留痕**（kb_scope 产出，本模块只读不改）。

    A4：每次「L3 被排除 → 其下 L4 一律不进」时留一条
        `{work_type_id, work_type_name, source, source_label, level, reason, l4_count}`，
        `source` ∈ `building_type`（建筑类型排除）/ `structure_type`（结构类型剔除整棵）/
        `user_exclusion`（用户明确排除）。
    A5：L4「该不该单独成工序」的通用校验留痕（与档位无关）。
    A6：用户输入参与筛选的留痕（L3 强化 / 量=0 剔除 / 明确排除项进闸门与待确认）。

    拿不到留痕（旧 plan_json / 上游没跑）→ 返回空壳，**不报错**（这是留痕，不是判据）。
    """
    empty = {"excluded_subtrees": [], "excluded_subtree_count": 0,
             "l3_strengthened": [], "l4_excluded_by_quantity": [],
             "user_exclusions_applied": [], "user_exclusions_pending": [],
             "standalone": {}}
    if not isinstance(scope, dict):
        return empty

    def _list(key):
        v = scope.get(key)
        return [dict(x) for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    subtrees = _list("excluded_subtrees")
    st = scope.get("standalone_audit")
    standalone = dict(st) if isinstance(st, dict) else {}
    return {
        "excluded_subtrees": subtrees,
        "excluded_subtree_count": len(subtrees),
        "l3_strengthened": _list("l3_strengthened"),
        "l4_excluded_by_quantity": _list("l4_excluded_by_quantity"),
        "user_exclusions_applied": _list("user_exclusions_applied"),
        "user_exclusions_pending": _list("user_exclusions_pending"),
        "standalone": standalone,
    }


def format_scope_audit(result, limit=5):
    """把范围审计留痕渲染成几行中文（没有留痕返回空串）。"""
    audit = (result or {}).get("scope_audit") or {}
    lines = []
    subtrees = audit.get("excluded_subtrees") or []
    if subtrees:
        lines.append("范围审计：%d 棵 L3 子树因被排除而整棵不进树" % len(subtrees))
        for it in subtrees[:limit]:
            lines.append("  · %s（%s）：%s ← 排除来源：%s"
                         % (it.get("work_type_name") or it.get("work_type_id"),
                            it.get("work_type_id"), it.get("reason"),
                            it.get("source_label") or it.get("source")))
        if len(subtrees) > limit:
            lines.append("  …其余 %d 棵（完整清单见 plan_json.meta.kb_scope_conformance）"
                         % (len(subtrees) - limit))
    pending = audit.get("user_exclusions_pending") or []
    if pending:
        lines.append("用户明确排除项待确认 %d 条（局部/定位不了，不进硬闸门）：%s"
                     % (len(pending),
                        "、".join(_as_text(x.get("text")) for x in pending[:limit])))
    standalone = audit.get("standalone") or {}
    if standalone.get("judged"):
        lines.append("L4「该不该单独成工序」判据命中 %d 条（A5，与档位无关）"
                     % int(standalone.get("judged") or 0))
    return "\n".join(lines)


def check_scope_conformance(wbs, scope, limit=DEFAULT_LIMIT):
    """核对 WBS 树里的 `kb_activity_id` 是否都落在 `kb_scope` 给的范围里。

    返回：
      {
        "checked":  bool,     # False = 没有可用的范围，本次没做核对（不是"通过"）
        "applied":  bool,     # 范围是否真的约束过（checked 通过时恒 True）
        "leaves":   int,      # 树里叶子总数
        "anchored": int,      # 其中显式挂了 kb_activity_id 的条数
        "violations": int,    # 违规叶子条数（同编号同相会合并计数，这里仍是原始条数）
        "by_kind":  {kind: 条数},
        "issues": [           # 按首次出现顺序、每个(阶段,编号)一条
            {"kind","kind_label","phase","wp","activity_id","leaf_name",
             "work_type_id","reason","count"}
        ],
        "summary": str,       # 一句话（可直接进 done_summary / 门正文）
        "note":    str,       # 明细行（"…其余 N 条"之类），没有则为空串
      }

    纯函数：只读入参与知识库，不改树、不写 ctx、不抛异常（KB 不可用时按 `unknown` 处理，
    因为"查不到"与"库里真没有"对用户是同一件事：编号对不上）。
    """
    result = {"checked": False, "applied": False, "leaves": 0, "anchored": 0,
              "violations": 0, "by_kind": {}, "issues": [], "summary": "", "note": "",
              # A4/A5/A6 的范围审计留痕（配合 kb_scope 的留痕，进一致性校验输出）
              "scope_audit": scope_audit(scope)}
    if not _scope_applied(scope):
        result["summary"] = "未取得知识库范围，本次未做范围一致性核对"
        result["note"] = ""
        return result

    allowed = _allowed_index(scope)
    excluded = _excluded_index(scope)
    # 本工程**在范围内的工种**（= l4_candidates 的键）。
    # ⚠️ 不能拿 `allowed.values()` 代替：某个工种的 L4 被结构形式**全剔空**时
    # （例如工业厂房下的钢结构工程），它一条 allowed 都没有，但它在范围内 ——
    # 用 allowed.values() 会把"被结构剔空"误判成"工种不在范围内"，修法完全指错。
    in_scope_l3 = {_as_text(k) for k in (scope.get("l4_candidates") or {})}
    result["checked"] = True
    result["applied"] = True

    seen = {}          # (phase, activity_id) → issues 下标（同一处只报一条）
    for phase, wp, leaf in _iter_leaves(wbs):
        result["leaves"] += 1
        aid = _as_text(leaf.get("kb_activity_id")).strip()
        if not aid:
            continue                     # 没挂编号的叶子不参与判定（见模块注释）
        result["anchored"] += 1
        try:
            l3 = _as_text(kb_l3_of(aid))
        except Exception:                # noqa: BLE001 — KB 不可用 → 当成查不到
            l3 = ""

        kind, work_type_id, reason = _resolve_kind(aid, allowed, excluded, l3, in_scope_l3)
        if kind is None:
            continue
        result["violations"] += 1
        result["by_kind"][kind] = result["by_kind"].get(kind, 0) + 1

        key = (phase, aid)
        hit = seen.get(key)
        if hit is not None:
            result["issues"][hit]["count"] += 1
            continue
        if len(result["issues"]) >= max(1, int(limit)):
            continue                     # 明细限流：计数照记，明细不再膨胀
        seen[key] = len(result["issues"])
        result["issues"].append({
            "kind": kind,
            "kind_label": _KIND_LABEL.get(kind, kind),
            "phase": phase,
            "wp": wp,
            "activity_id": aid,
            "leaf_name": _as_text(leaf.get("name")),
            "work_type_id": work_type_id,
            "reason": reason,
            "count": 1,
        })

    result["summary"] = format_summary(result)
    result["note"] = format_note(result)
    return result


def _resolve_kind(aid, allowed, excluded, l3, in_scope_l3):
    """判定一条编号 → `(kind|None, work_type_id, reason)`。

    三种情形分得清清楚楚（修法完全不同，不能混成一句）：
      · 编号在范围内            → 不违规；
      · 库里查不到这个编号      → `unknown`（改编号或补库）；
      · 该工种在本建筑类型下排除 → `banned`；
      · 该工种在范围内但这条被结构剔除 → `banned`（"严禁使用"清单里那类）；
      · 该工种压根不在本工程范围 → `other_l3`。
    """
    if aid in allowed:
        return None, allowed[aid], ""
    if not l3:
        return KIND_UNKNOWN, "", "知识库活动字典里查不到该编号"
    if l3 in excluded:
        why = excluded[l3]
        return KIND_BANNED, l3, "该工种在本建筑类型下已排除" + ("（%s）" % why if why else "")
    if l3 in in_scope_l3:
        return KIND_BANNED, l3, "该工种在本工程范围内，但这条工序被结构形式剔除（严禁使用）"
    return KIND_OTHER_L3, l3, "该活动所属工种（%s）不在本工程的知识库范围内" % l3


def kb_l3_of(activity_id):
    """`kb.l3_of_activity` 的间接引用（便于测试打桩，避免模块级循环导入）。"""
    from .. import kb
    return kb.l3_of_activity(activity_id)


def format_summary(result):
    """一句话摘要（0 条违规时给"已核对且通过"，不要给空串 —— 两者含义不同）。"""
    if not result.get("checked"):
        return _as_text(result.get("summary")) or "未取得知识库范围，本次未做范围一致性核对"
    n = int(result.get("violations") or 0)
    if not n:
        return ("知识库范围一致性核对通过（%d 条已锚定工序全部在范围内）"
                % int(result.get("anchored") or 0))
    by = result.get("by_kind") or {}
    bits = "；".join("%d 条「%s」" % (by[k], _KIND_LABEL[k]) for k in
                     (KIND_BANNED, KIND_OTHER_L3, KIND_UNKNOWN) if by.get(k))
    return "知识库范围一致性核对发现 %d 处违规（%s）" % (n, bits)


def format_note(result, limit=3):
    """明细行：前 `limit` 条 + "其余 N 处"一行；没有明细返回空串。

    ⚠️ 这里的"其余"要按**违规条数**（`violations`）算，不能只看 `issues` 的长度：
    `issues` 本身已被 `check_scope_conformance(limit=...)` 截断，只比较它的长度会把
    "被截掉的那些"算漏（10 处违规、issues 只剩 3 条、却报"其余 0 处"）。
    """
    issues = result.get("issues") or []
    if not issues:
        return ""
    lines = []
    for it in issues[:limit]:
        lines.append("  · [%s] %s / %s：%s（%s）"
                     % (it.get("kind_label"), it.get("phase") or "?",
                        it.get("wp") or "?", it.get("activity_id"),
                        it.get("leaf_name") or ""))
    rest = int(result.get("violations") or 0) - len(lines)
    if rest > 0:
        lines.append("  …其余 %d 处（完整清单见 plan_json.meta.kb_scope_conformance）" % rest)
    return "\n".join(lines)


def gate_issues(result, dimension="结构"):
    """把违规转成**复评门认得的 issue**（HIGH），供人工门与一键修复使用。

    `target` 用"阶段名"而不是 id：`_repair_options` 的 `by_phase` 是靠
    `spec["phase"] in t` 匹配的，给阶段名最稳（给 id 时复评门的编号在重编号后会漂）。
    """
    out = []
    for it in (result or {}).get("issues") or []:
        if it.get("kind") == KIND_UNKNOWN:
            continue                     # 编号对不上库 → 不是"范围"问题，不进结构维度
        out.append({
            "severity": "HIGH",
            "dimension": dimension,
            "target": _as_text(it.get("phase")),
            "finding": ("知识库范围校验：%s 挂着 %s（%s），%s"
                        % (_as_text(it.get("leaf_name")) or "一条叶子",
                           it.get("activity_id"), it.get("phase") or "?",
                           _as_text(it.get("reason")))),
            "suggestion": ("把该工序改成本工程结构形式下适用的工序，或删除该条；"
                           "禁止使用的工序见 kb_scope 的剔除清单。"),
        })
    return out


def metadata(result):
    """进 `plan_json.meta` 的留档块（交付物 / 修订链 / /sources 可核对）。"""
    r = result or {}
    return {
        "checked": bool(r.get("checked")),
        "leaves": int(r.get("leaves") or 0),
        "anchored": int(r.get("anchored") or 0),
        "violations": int(r.get("violations") or 0),
        "by_kind": dict(r.get("by_kind") or {}),
        "summary": _as_text(r.get("summary")),
        "issues": [dict(it) for it in (r.get("issues") or [])],
        # A4：L3 整棵子树被排除的审计留痕（kb_scope 产出）
        "excluded_subtrees": [dict(x) for x in
                              ((r.get("scope_audit") or {}).get("excluded_subtrees") or [])],
    }
