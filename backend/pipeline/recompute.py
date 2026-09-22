# -*- coding: utf-8 -*-
"""修订后的重算回调 —— 让「自然语言改计划」真的改到工期与日期上。

为什么单独一个模块：`nodes/revise.py` 只管"把话翻译成规范修改指令 + 校验 + 落修订链"，
它**刻意不排程**（`default_recompute` 只按工程量/定额重算被影响叶子自身的工期）。
真正的重算要跑 依赖 → 排程（两版） → 回写日期，这需要流水线里的其它节点，
所以放在这里、用回调注入，避免 revise 节点反向依赖整条流水线（会成环）。

回写什么（都是"用户看得见、必须跟着变"的东西）：
  - 每条任务的 start_date / finish_date / duration_days / assigned_resources
  - overview.total_duration_days / planned_end_date
  - cpm_result.total_duration_days、关键路径
  - resource_plan.peak_manpower / total_manpower_days
  - meta.schedule_versions（两版工期，供看板与审计复查）

**计划必须自包含**：本模块需要 `extracted_params` / `boundary_conditions` 才能排出
与初版同口径的两版工期，因此 `plan_json.meta` 里会带上它们（见 plan_assembler.build_meta）。
取不到时按空处理并如实记一条 warning，绝不编造参数。

**修订只该改用户点名的那一项**（第 36 轮修正）。两条硬规则：
  1. 本次修订没有动到排程输入（`quantity` / `duration` / `norm` / `crew`）时**根本不排程**
     —— 改名、改细度、改成本口径、改施工段都只是文本/元数据，跟工期日期无关。
  2. 真的排程时，**未被波及的叶子一律冻结**为存档原值（`frozen_ids`），只有被点名的
     任务及其下游允许变。

为什么必须这么做（实测踩过的坑）：在一份真实的 209 条计划上，一句纯改名让 98 条
工期换了一套口径（59→68、7→21）、资源峰值 121→71、总工日 26756→27672，而
`overview.total_duration_days` 不变，总结里还写着"总工期 966 天（未变）"—— 用户完全
看不出来计划已经被改写。根因是"在已标注过的树上重跑排程不幂等"（详见
`nodes/scheduler.py` 里 `compute_schedules` 的说明）。

Python 3.8 兼容；不新增第三方依赖。
"""

from __future__ import print_function

import datetime
import math

# 真正会改变工期/日期/资源的字段。**只有**它们被改动才需要重排。
SCHEDULING_FIELDS = ("quantity", "duration", "norm", "crew", "add_task", "remove_task")


def _num(value, default=None):
    try:
        if value is None or value == "":
            return default
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _leaves(plan):
    """所有叶子。畸形结构一律跳过，绝不抛异常（脏档案也要能降级处理）。"""
    out = []
    wbs = plan.get("wbs") if isinstance(plan, dict) else None
    wbs = wbs if isinstance(wbs, dict) else {}
    for ph in wbs.get("phases") or []:
        if not isinstance(ph, dict):
            continue
        for wp in ph.get("work_packages") or []:
            if not isinstance(wp, dict):
                continue
            for sub in wp.get("sub_packages") or []:
                if isinstance(sub, dict):
                    out.append(sub)
    return out


def _add_days(start, days):
    return (start + datetime.timedelta(days=int(days))).isoformat()


def _closed_last_day(row):
    """排程行 → **闭区间末日**下标 `max(es, ef - 1)`（`ef` 是半开上界）。

    与 `plan_assembler._closed_last_day` / `delivery._compute_view` 同一口径：
    交付物给人看的是日期（首尾两天都算），`finish_date` 必须是 `开工 + (ef-1)`，
    否则每条任务的日期跨度都比排程跨度多一天。
    """
    try:
        es = int(row.get("es") or 0)
    except (TypeError, ValueError):
        es = 0
    try:
        ef = int(row.get("ef") or 0)
    except (TypeError, ValueError):
        ef = es
    return max(es, ef - 1)


def _start_date(plan):
    ov = plan.get("overview") or {}
    raw = str(ov.get("planned_start_date") or "")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(raw[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return datetime.date.today()


_LABOR_HINT = ("工",)
_MACHINE_HINT = ("车", "机", "泵", "吊", "塔", "夯", "钻", "锯", "焊", "搅")


def _is_labor(name):
    s = str(name or "")
    if any(h in s for h in _MACHINE_HINT) and "工" not in s:
        return False
    return any(h in s for h in _LABOR_HINT)


def recompute_after_revision(ctx, affected_ids):
    """修订后的真实重算：跑排程（两版）并把日期/工期/峰值回写到计划里。

    签名与 `nodes.revise.default_recompute` 一致（ctx, affected_ids）→ dict，
    可直接用 `ReviseNode(recompute=recompute_after_revision)` 注入。

    任何异常都降级：退回"只报受影响范围"，绝不让改写流程崩掉。
    """
    plan = (ctx or {}).get("plan_json")
    if not isinstance(plan, dict) or not plan:
        return {"changed": [], "duration_changes": [],
                "summary": "没有可重算的计划", "total_duration_days": None}

    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    params = meta.get("extracted_params") if isinstance(meta.get("extracted_params"), dict) else {}
    boundary = (meta.get("boundary_conditions")
                if isinstance(meta.get("boundary_conditions"), dict) else {})
    warnings = []

    try:
        from .nodes import scheduler as sched
    except Exception as exc:                                  # 极端情况：调度器不可用
        return {"changed": [], "duration_changes": [],
                "summary": "排程器不可用，未重算工期（%s）" % str(exc)[:80],
                "total_duration_days": None}

    # ---- ⓪ 本次修订到底有没有动到"排程输入"？----
    # 计划级字段（项目名称 / 细度 / 成本口径 / 施工段）改的是文本与元数据，跟工期、
    # 日期、资源毫无关系。旧实现不管改什么都一路重排，于是"改个名字"就能把一份
    # 209 条的计划悄悄重写掉一半。这里直接短路：没动排程输入，就一个工期都不许动。
    touched = (ctx or {}).get("recompute_touched")
    if touched is not None:
        fields = sorted(set(str(t.get("field") or "") for t in touched
                            if isinstance(t, dict)))
        if not (set(fields) & set(SCHEDULING_FIELDS)):
            named = "/".join(f for f in fields if f)
            if not any(fields):
                note = "这次修订没有生效的修改项"
                tail = "，没有动到排程输入 → 未重排，工期 / 日期 / 资源保持原样"
                brief = "计划未重排（工期与日期保持原样）"
            elif "start_date" in fields:
                # ⚠️ 开工日期**确实动了日期**（`apply_patch` 已把整份日程按天数平移），
                # 所以这里绝不能沿用"日期保持原样"那句话 —— 一句自相矛盾的提示
                # 比没有提示更糟。它没做的是"重跑排程"，不是"没动日期"。
                note = "只改了计划级信息（%s）" % named
                tail = ("，没有动到排程输入；开工日期改的是日历，整份日程已整体平移"
                        "（各任务工期与资源投入不变），但没有重跑排程")
                brief = "日程已整体平移，未重跑排程（工期与资源不变）"
            else:
                note = "只改了计划级信息（%s）" % named
                tail = "，没有动到排程输入 → 未重排，工期 / 日期 / 资源保持原样"
                brief = "计划未重排（工期与日期保持原样）"
            warnings.append(note + tail)
            return {
                "changed": [], "duration_changes": [],
                "affected": list(affected_ids or []),
                "total_duration_days": _num(
                    (plan.get("overview") or {}).get("total_duration_days")),
                "warnings": warnings,
                "summary": note + "，" + brief,
            }

    durations_before = dict((str(l.get("id")), _num(l.get("duration_days")))
                            for l in _leaves(plan))

    # ---- 用户**点名改工期**的任务：工期是命令，不是建议 ----
    # 排程器只会算"工日 ÷ 班组人数"，直接排回去会把用户写的工期覆盖掉。
    # 正确做法不是硬改工期，而是**按指定工期反解所需资源量**：定额工日不变，
    # 要在 N 天内干完就需要 ceil(工日 ÷ N) 个人。
    #
    # ⚠️ **2026-09-21 C 组裁定 G**：C8 第 6 项删掉了「叶子上写明的投入人工
    # (`norm_binding.crew`)」这个人数来源，所以反解结果**不许再写回
    # `binding["crew"]`**（写了也没人读）。改走 **C9 的正解 —— 用户限额通道**：
    #   `boundary_conditions["crew_design"][工种] = 反解人数`
    #   `boundary_conditions["_source"]["crew_design"] = "user"`
    # `scheduler.user_declared_crews()` 会把它并进 `limits["crew_design"]`，
    # 再经 `user_cap_for_task()` 进 `min(汇总容量, 用户同类限额)` —— 于是
    # 「用户要求 N 天」在唯一公式下等价于「该工种的同类限额 = ceil(工日 ÷ N)」，
    # 依据串里也会带出「用户申报同类限额」（可溯源、不静默）。
    #
    # 只对"改的是工期"的任务生效（`recompute_duration_locks`）。改工程量/改班组时
    # 用户没指定工期，工期本来就该由定额重新算 —— 若一并反解，工程量翻倍也看不出变化。
    locked = set(str(i) for i in ((ctx or {}).get("recompute_duration_locks") or []))
    if not isinstance(boundary, dict):
        boundary = {}
    _design = boundary.get("crew_design")
    design = dict(_design) if isinstance(_design, dict) else {}
    _src_map = boundary.get("_source")
    src_map = dict(_src_map) if isinstance(_src_map, dict) else {}
    for leaf in _leaves(plan):
        tid = str(leaf.get("id"))
        if tid not in locked:
            continue
        days = _num(leaf.get("duration_days"))
        qty = _num(leaf.get("quantity"))
        binding = leaf.get("norm_binding") if isinstance(leaf.get("norm_binding"), dict) else {}
        prod = _num(binding.get("productivity_value"))
        if prod is None:
            nv = _num(binding.get("norm_value"))
            # 第 37 轮修正：labor_norm_value 落库时已归一为「工日 / 1×单位」
            # （留档不变式 raw_value / raw_quantity_basis == labor_norm_value），
            # 所以产能 = 1 / norm_value。旧写法 basis / norm_value 把产能放大 basis 倍
            # （KB 里 1030 条 basis≠1，最大 basis=1000），班组与工期随之缩小 basis 倍；
            # `quantity_basis`（= raw_quantity_basis）只作溯源，不参与乘法。
            # 注意：机械台班定额**没有**归一，必须继续乘 basis（见 scheduler/resource）。
            prod = (1.0 / nv) if nv else None
        if not days or days < 1 or not qty or qty <= 0 or not prod or prod <= 0:
            continue
        need = int(max(1, math.ceil(qty / (prod * days))))
        types = binding.get("labor_types") if isinstance(binding.get("labor_types"), list) else []
        role = str(types[0]) if types else "普工"
        # 同一工种多条被点名 → 取**最紧**的那条（min），与 C9「同类取最小」同口径
        if role in design:
            design[role] = min(int(design[role]), need)
        else:
            design[role] = need
        src_map["crew_design"] = "user"
        warnings.append("%s：用户指定 %d 天 → 反解所需班组 %s %d 人"
                        "（工程量 %s ÷ (产能 %s × %d 天)），已作为**用户同类限额**"
                        "（`crew_design`）交给新链路：有效容量 = min(段容量, 该限额)"
                        % (tid, int(days), role, need, qty, prod, int(days)))
    if design:
        boundary["crew_design"] = design
        boundary["_source"] = src_map
        meta["boundary_conditions"] = boundary

    versions_before = (meta.get("schedule_versions") or {})

    # ---- 冻结：只允许**被点名的叶子**重算工期 ----
    # 为什么不用 `affected_ids`（下游闭包）：真实 WBS 里楼层任务首尾相接，
    # `dependency_closure(deps, ['4.1.1.1'])` 就是 **199/209 条** —— 拿它当"允许改"
    # 的范围等于没限制，那 36 条"存档 crew 与排程器口径对不上"的任务照样被重写
    # （实测：工期动了 37 条、总工期 966→543）。
    # 下游任务的**日期**本来就该随 CPM 顺延（那是用户改一条工序的正常后果），
    # 但它们的**工期**不该被重算 —— 用户只改了一条。
    all_ids = set(str(l.get("id")) for l in _leaves(plan) if l.get("id") is not None)
    named = set(str(i) for i in ((ctx or {}).get("recompute_locked_ids") or []))
    named_leaves = named & all_ids
    frozen = (all_ids - named_leaves) if named_leaves else set()

    if not boundary and params:
        warnings.append(
            "本计划缺少 meta.boundary_conditions（初版的资源/工期边界没有随计划存下来），"
            "重排只能按缺省施工组织推算，**结果可能与初版口径不一致**；"
            "如需严格复现初版工期，请重新生成一份计划。")

    try:
        out = sched.compute_schedules(
            plan.get("wbs") or {}, plan.get("dependencies") or [], boundary, params,
            plan.get("cpm_result"),
            # 修订路径：沿用叶子上写明的投入人工（首版实算结果），不重新摊派人力预算
            reuse_declared_crews=True,
            # 没被点名的任务，工期保持存档原值
            frozen_ids=frozen or None)
    except Exception as exc:
        return {"changed": [], "duration_changes": [],
                "summary": "重算排程失败，计划保持原样（%s）" % str(exc)[:80],
                "total_duration_days": None}

    new_versions = out.get("schedule_versions") or {}
    version = new_versions.get("resource_ok") or {}
    # 裁定 B 收口（2026-09-21）：**缺工作面容量的任务，工期不随工程量变化** ——
    # 这句话必须传到用户面前。否则「改了工程量、总工期一动不动」会让人以为改动没生效
    # （实测：无层面积 / 无 MWI 行的任务落到 `reported_missing`，工期沿用叶子原值）。
    # 排程层的 `schedule_versions.warnings` 此前只落进 `meta`，这里把容量相关那条挑出来。
    for _cap_w in (new_versions.get("warnings") or []):
        if "缺工作面容量数据" in str(_cap_w):
            warnings.append(str(_cap_w))
    rows = dict((str(r.get("task_id")), r) for r in (version.get("schedule") or []))
    if not rows:
        warnings.append("重算后没有排出任何任务，日期未回写")

    # ---- 回写叶子工期 ----
    changed = []
    for leaf in _leaves(plan):
        tid = str(leaf.get("id"))
        row = rows.get(tid)
        if not row:
            continue
        days = int(max(1, (row.get("ef") or 0) - (row.get("es") or 0)))
        old = durations_before.get(tid)
        if old is None or int(old) != days:
            leaf["duration_days"] = days
            changed.append({"target": tid, "old_duration_days": old, "new_duration_days": days})

    # ---- 回写日期与资源 ----
    start = _start_date(plan)
    total = int(version.get("total_duration_days") or 0)
    all_tasks = []
    for leaf in _leaves(plan):
        tid = str(leaf.get("id"))
        row = rows.get(tid)
        if not row:
            continue
        all_tasks.append({
            "task_id": tid,
            "task_name": leaf.get("name", tid),
            "start_date": _add_days(start, row.get("es") or 0),
            # 闭区间：末日 = 开工 + (ef - 1)（`ef` 半开上界），见 `_closed_last_day`
            "finish_date": _add_days(start, _closed_last_day(row)),
            "duration_days": int(max(1, (row.get("ef") or 0) - (row.get("es") or 0))),
            # 「WBS 目标天数」与上面的**排程跨度**分开存（交付物把两列并排印）。
            # 取叶子自己申报的工期；叶子也没有就与跨度同值——宁可两列相等，
            # 也不要凭空写 null 让交付物整列印「—」。
            "wbs_target_days": int(leaf.get("duration_days")
                                   or max(1, (row.get("ef") or 0) - (row.get("es") or 0))),
            "assigned_resources": dict((k, int(v)) for k, v in (row.get("crew") or {}).items()),
            # 裁定 B（2026-09-21）：容量的**来源与依据**随行落进计划檔案，
            # 否则"为什么是 N 人 / 为什么工期不动"在修订后的计划里查无对证。
            "capacity_source": row.get("capacity_source"),
            "capacity_basis": row.get("capacity_basis"),
        })
    if all_tasks:
        all_tasks.sort(key=lambda t: t["task_id"])
        plan["all_tasks_schedule"] = all_tasks
        crit = set(str(x) for x in (version.get("critical_path") or []))
        plan["critical_path_tasks"] = [t for t in all_tasks if t["task_id"] in crit]

    if not all_tasks:
        # 没排出任何任务 → 绝不把 0 写进总工期（那会让计划看起来"零工期"）
        warnings.append("重算没有产出任何任务，总工期与日期保持原值")
        meta["schedule_versions"] = meta.get("schedule_versions") or {}
        plan["meta"] = meta
        return {"changed": [], "duration_changes": [], "affected": list(affected_ids or []),
                "total_duration_days": _num((plan.get("overview") or {}).get("total_duration_days")),
                "warnings": warnings, "summary": "重算没有产出任何任务，计划保持原样"}

    ov = plan.setdefault("overview", {})
    ov["total_duration_days"] = total
    # 闭区间：竣工日 = 开工 + (总工期 - 1)，与逐条 finish_date 同口径
    ov["planned_end_date"] = _add_days(start, max(0, total - 1))
    ov["critical_path_length"] = len(plan.get("critical_path_tasks") or [])

    cpm = plan.setdefault("cpm_result", {})
    if isinstance(cpm, dict):
        cpm["total_duration_days"] = total
        cpm["critical_path"] = list(version.get("critical_path") or [])

    # ---- 资源峰值：只改"会变"的两个数字，材料总量属于项目参数、不随修订变 ----
    rp = plan.get("resource_plan")
    if isinstance(rp, dict):
        # 重算路径用的是**排程曲线**的 peak_labor（不是用户申报值），所以口径键必须
        # 同步成 "resource_curve"：否则会出现"值是曲线峰值、来源却还写着
        # model_estimate / user"的错标 —— 那正是第 40 轮要消灭的"同一个词指两个数"。
        # 但曲线取不到时（version 里没有 peak_labor）**不许**把 0 标成曲线口径：
        # 那比不标更糟，交付物会拿 0 去当"实算峰值"展示。此时保持原来源键不动。
        _peak_labor = int(version.get("peak_labor") or 0)
        rp["peak_manpower"] = _peak_labor
        if _peak_labor > 0:
            rp["peak_manpower_source"] = "resource_curve"
            if rp.get("curve_peak_manpower") is None:
                rp["curve_peak_manpower"] = _peak_labor
        man_days = 0.0
        for row in rows.values():
            days = int(max(1, (row.get("ef") or 0) - (row.get("es") or 0)))
            man_days += sum(float(v) for k, v in (row.get("crew") or {}).items()
                            if _is_labor(k)) * days
        rp["total_manpower_days"] = round(man_days, 1)

    # ---- 两版工期存回 meta（看板/审计要复查"改成了哪两版"）----
    meta["schedule_versions"] = {
        "theory_min_days": (new_versions.get("theory_min") or {}).get("total_duration_days"),
        "resource_ok_days": total,
        "delta_days": (new_versions.get("compare") or {}).get("delta_days"),
        "warnings": list(new_versions.get("warnings") or []),
    }
    meta["norm_coverage"] = out.get("norm_coverage") or meta.get("norm_coverage") or {}
    plan["meta"] = meta

    days_before = _num((versions_before or {}).get("resource_ok_days"))
    if days_before is None:
        days_before = _num((versions_before or {}).get("theory_min_days"))
    summary = "已按修订重排：总工期 %s 天" % total
    if days_before is not None and int(days_before) != total:
        summary = "已按修订重排：总工期 %d → %d 天" % (int(days_before), total)
    if changed:
        summary += "；%d 项任务工期随之变化" % len(changed)

    return {
        "changed": changed,
        "duration_changes": changed,
        "affected": list(affected_ids or []),
        "total_duration_days": total,
        "theory_min_days": (new_versions.get("theory_min") or {}).get("total_duration_days"),
        "warnings": warnings,
        "summary": summary,
    }
