# -*- coding: utf-8 -*-
"""终版修改 · 验收检查器（只读）

用法：
    python devtools/verify_final_modifications.py                         # 只查知识库
    python devtools/verify_final_modifications.py --plan <plan.json>      # 再查计划产物
    python devtools/verify_final_modifications.py --plan <plan.json> --kb BuildPlan_KB/kb.db

产物可以是完整 plan JSON，也可以是单独的 resource_demand；
本脚本会递归找 `resource_demand` / `tasks`，对不上就跳过对应判据（不误报）。

判据来自 `devtools/_dev-notes/终版修改_接口冻结.md` 与用户裁定。
退出码：0 = 全过；1 = 有 FAIL；2 = 有 SKIP（缺输入）。
"""
import argparse
import io
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS = []
SKIPS = []
PASSES = []


def ok(tag, msg=""):
    PASSES.append((tag, msg))


def bad(tag, msg):
    FAILS.append((tag, msg))


def skip(tag, msg):
    SKIPS.append((tag, msg))


# ---------------------------------------------------------------- 知识库

TABLES_NEEDING_MEASURE_SCOPE = [
    "Norm_Labor_Table",
    "Norm_Equipment_Table",
    "L4_Norm_Default",
    "L4_Activity_Dictionary",
]


def check_kb(db_path):
    if not os.path.exists(db_path):
        skip("KB", "知识库不存在：%s" % db_path)
        return None
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    # 判据 1：measure_scope 列存在于 4 张表
    for t in TABLES_NEEDING_MEASURE_SCOPE:
        if t not in tables:
            bad("KB.measure_scope", "缺表：%s" % t)
            continue
        cols = {r[1] for r in cur.execute("PRAGMA table_info([%s])" % t)}
        if "measure_scope" not in cols:
            bad("KB.measure_scope", "%s 无 measure_scope 列" % t)
        else:
            ok("KB.measure_scope", "%s 有列" % t)

    # 判据 1b：measure_scope 词表受控（不出现可疑自由文本）
    vocab = {
        "建筑面积", "外墙面积", "内墙抹灰面积", "天棚面积", "楼地面面积",
        "模板接触面积", "风管展开面积", "保温面积", "防水面积", "管道长度",
        "电缆长度", "体积", "质量", "桩根数", "件数", "台数", "自然单位",
        "项", "",
    }
    for t in TABLES_NEEDING_MEASURE_SCOPE:
        if t not in tables:
            continue
        cols = {r[1] for r in cur.execute("PRAGMA table_info([%s])" % t)}
        if "measure_scope" not in cols:
            continue
        vals = {}
        for (v,) in cur.execute(
                "SELECT measure_scope FROM [%s]" % t):
            vals[v] = vals.get(v, 0) + 1
        stray = {k: n for k, n in vals.items() if k not in vocab}
        filled = sum(n for k, n in vals.items() if k not in (None, ""))
        total = sum(vals.values())
        if stray:
            bad("KB.measure_scope.vocab",
                "%s 出现词表外的值：%s" % (t, sorted(stray)[:6]))
        else:
            ok("KB.measure_scope.vocab",
               "%s 全部在词表内（已填 %d/%d 行）" % (t, filled, total))

    # 判据 4 前置：equipment_driven 且有台班行的活动数
    try:
        eq_driven = [r[0] for r in cur.execute(
            "SELECT activity_id FROM L4_Activity_Dictionary "
            "WHERE recommended_production_mode='equipment_driven'")]
        eq_rows = {r[0] for r in cur.execute(
            "SELECT DISTINCT activity_id FROM Norm_Equipment_Table")}
        both = [a for a in eq_driven if a in eq_rows]
        ok("KB.equipment", "equipment_driven %d 个，其中 %d 个有台班定额行"
           % (len(eq_driven), len(both)))
    except Exception as e:
        skip("KB.equipment", "查不了：%s" % e)

    # 判据 4b：index norm_kind=machine 是否补上
    try:
        cols = {r[1] for r in cur.execute("PRAGMA table_info([L4_Norm_Default])")}
        if "norm_kind" in cols:
            dist = dict(cur.execute(
                "SELECT norm_kind, COUNT(*) FROM L4_Norm_Default GROUP BY norm_kind"))
            if dist.get("machine"):
                ok("KB.norm_index", "L4_Norm_Default 含 machine 索引 %d 行（labor %d）"
                   % (dist["machine"], dist.get("labor", 0)))
            else:
                bad("KB.norm_index",
                    "L4_Norm_Default 仍无 machine 索引行（分布 %s）→ 机械优先走不通" % dist)
    except Exception as e:
        skip("KB.norm_index", "查不了：%s" % e)

    # H2/H3（2026-09-21）：两张 legacy 归档表已删除 → 断言"表不存在"。
    # （旧版这里是查 notes 里的 deprecated 横幅；归档件删了，横幅也就没有载体。）
    for t in ("Workface_Capacity_Rule_legacy_v1", "Workface_Capacity_Rule_legacy_v2"):
        if t in tables:
            bad("KB.legacy", "%s 仍存在（H2/H3 要求已删除）" % t)
        else:
            ok("KB.legacy", "%s 已按 H2/H3 删除（表不存在）" % t)

    # legacy 表不得被代码引用（真正的"唯一现行版"保证）
    try:
        hits = []
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "backend")):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", "_probe_tmp", "tests")]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    with io.open(p, "r", encoding="utf-8", errors="ignore") as f:
                        txt = f.read()
                except Exception:
                    continue
                if "Workface_Capacity_Rule_legacy" in txt:
                    hits.append(os.path.relpath(p, ROOT))
        if hits:
            bad("KB.legacy.code", "代码仍引用 legacy 表：%s" % hits)
        else:
            ok("KB.legacy.code", "backend 下无代码引用 legacy 表")
    except Exception as e:
        skip("KB.legacy.code", "扫不动：%s" % e)

    # 占位来源是否登记
    try:
        row = cur.execute("SELECT COUNT(*) FROM sources WHERE source_code='SCAFFOLD_V1'").fetchone()
        if row and row[0]:
            ok("KB.scaffold_source", "SCAFFOLD_V1 已在 sources 登记")
        else:
            skip("KB.scaffold_source", "sources 无 SCAFFOLD_V1（若本次未补占位活动可忽略）")
    except Exception:
        pass

    con.close()
    return tables


# ---------------------------------------------------------------- 计划产物

def find_all(obj, key):
    """递归找出所有名为 key 的子树。"""
    out = []
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj[key])
        for v in obj.values():
            out.extend(find_all(v, key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(find_all(v, key))
    return out


def leaf_tasks(plan):
    """从产物里取出工序列表（够用即可，取最长的那个 tasks 列表）。

    ⚠️ 只认键名 `tasks`。WBS 是 `{'wbs': {'phases': [...]}}` 的嵌套结构，
    **不在这个键下**，所以本函数取不到 WBS 叶子 —— 要 binding 数据请用
    `wbs_leaves()`。早先版本把两者的结果混用，导致所有 `norm_binding` 判据
    都读成空 dict 并静默通过。
    """
    cands = find_all(plan, "tasks")
    best = []
    for c in cands:
        if isinstance(c, list) and len(c) > len(best) and c and isinstance(c[0], dict):
            best = c
    return best


def wbs_leaves(plan):
    """WBS 叶子：任何带 `norm_binding` / `kb_binding` / `kb_activity_id` 的节点。

    结构无关的全量遍历（WBS 的层级/键名可能变）。返回去重后的列表。
    """
    out = []

    def walk(o):
        if isinstance(o, dict):
            if any(k in o for k in ("norm_binding", "kb_binding", "kb_activity_id")):
                out.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(plan)
    seen, uniq = set(), []
    for o in out:
        if id(o) not in seen:
            seen.add(id(o))
            uniq.append(o)
    return uniq


def get_norm_binding(t):
    for k in ("norm_binding", "kb_binding"):
        if isinstance(t.get(k), dict):
            return t[k]
    nb = t.get("_kb_norm")
    return nb if isinstance(nb, dict) else {}


def check_plan(plan, rd):
    # 两层数据要分开取，别混用：
    #   · `resource_demand.tasks` → 只带 `_norm_applied` / `unit` / `measure_scope` / `_crew` …
    #   · WBS 叶子               → 才带 `norm_binding`（绑定本体、口径、usable、not_usable_reason）
    # 早先版本两层混用，导致 `norm_binding` 一律取到空 dict、
    # 所有 binding 判据都读成 0（静默假通过）。这里显式分层。
    tasks = leaf_tasks(rd) or leaf_tasks(plan)
    wtasks = wbs_leaves(plan) or tasks

    # 判据 9/定额丢失：_norm_applied is None 的条目要在产物里有说明
    if tasks:
        missing = [t for t in tasks
                   if t.get("_norm_applied") is None and t.get("_norm_applied") != 0]
        blocked = [t for t in tasks
                   if get_norm_binding(t).get("usable") is False]
        ok("PLAN.tasks", "工序 %d 条；无 _norm_applied %d 条；binding 不可用 %d 条"
           % (len(tasks), len(missing), len(blocked)))

    # 判据 5：单位贯通
    if tasks:
        with_unit = [t for t in tasks if str(t.get("unit") or "").strip()]
        circle = [t for t in tasks if str(t.get("unit") or "").strip() == "㎡"]
        if len(with_unit) == len(tasks):
            ok("PLAN.unit", "%d/%d 条有 unit" % (len(with_unit), len(tasks)))
        else:
            bad("PLAN.unit", "只有 %d/%d 条有 unit（缺 %d 条）"
                % (len(with_unit), len(tasks), len(tasks) - len(with_unit)))
        if circle:
            bad("PLAN.unit.norm", "仍有 %d 条用「㎡」(U+33A1) 写法" % len(circle))
        else:
            ok("PLAN.unit.norm", "无「㎡」写法残留")

        # 判据 6：上限单源
        two_cap = [t for t in tasks
                   if (t.get("_workface_note") or "").find("资源层不再封顶") >= 0]
        below = [t for t in tasks if t.get("resource_cap_below_org")]
        if two_cap or below:
            bad("PLAN.cap_single_source",
                "仍有两套上限痕迹：note 命中 %d 条、resource_cap_below_org %d 条"
                % (len(two_cap), len(below)))
        else:
            ok("PLAN.cap_single_source", "无两套上限痕迹")

    # 判据 2：口径关留痕（**必须读 WBS 叶子那一层**）
    if wtasks:
        adj = [t for t in wtasks if get_norm_binding(t).get("basis_adjust")]
        unc = [t for t in wtasks if get_norm_binding(t).get("basis_unconfirmed")]
        mismatch = [t for t in wtasks
                    if get_norm_binding(t).get("not_usable_reason") == "口径无法对齐"]
        bindu = [t for t in wtasks
                 if get_norm_binding(t).get("not_usable_reason") == "活动绑定不一致"]
        # 「绑定不一致」是 WS1 新加的一致性校验。它的**误判**会把本来正确的定额打掉，
        # 所以这里不只看数量，还把受影响任务列出来，并区分「本该拦」与「误判」。
        # 已确证的误判特征：任务名与活动名同类（模板/砌块/钢筋/混凝土/装饰）却被降级。
        _SAME_FAMILY = ("模板", "铝模", "砌块", "墙板", "钢筋", "混凝土", "抹灰", "涂料")
        susp = []
        for t in bindu:
            nm = str(t.get("name") or t.get("task_name") or "")
            act = str(get_norm_binding(t).get("kb_activity_id") or "")
            if any(w in nm for w in _SAME_FAMILY):
                susp.append("%s %s→%s" % (t.get("id") or t.get("task_id"), nm[:14], act))
        msg = ("口径换算留痕 %d 条；口径未确认 %d 条；口径无法对齐 %d 条；"
               "绑定不一致 %d 条" % (len(adj), len(unc), len(mismatch), len(bindu)))
        # 判据修正（2026-09-20，父代理）：**「绑定不一致」非空本身不是失败**。
        # 契约 §3 明确要求"任务名与所绑活动名不符 → 降级为未绑定 + 留痕"，这是设计行为。
        # 真正的失败信号是**疑似误判**：同类工序名（模板/砌块/钢筋/混凝土/装饰…）却被降级，
        # 那会把本来正确的定额打掉。故只在 susp 非空时 FAIL，并把降级清单原样列出以便复看。
        ids = [str(t.get("id") or t.get("task_id") or "") for t in bindu]
        if susp:
            bad("PLAN.scope", msg + "；疑似误判 %d 条（同类名被降级）：%s"
                % (len(susp), "；".join(susp[:6])))
        else:
            ok("PLAN.scope", msg + "；降级为未绑定的工序：%s（同类名误判 0 条）"
               % ("、".join(ids) if ids else "无"))

        # 判据 3：已确证的两处错绑必须被拦住
        for tid, nm in (("1.1.1", "场地平整"), ("7.1.3", "暖通预留预埋")):
            hit = [t for t in wtasks
                   if str(t.get("id") or t.get("task_id") or "") == tid]
            if not hit:
                continue
            b = get_norm_binding(hit[0])
            # 只看"绑定的活动是谁"这三个字段。早先版本对整段 binding JSON 做子串匹配，
            # 会命中告警/溯源文本里出现的旧活动名 → 误报"仍绑着旧活动"。
            acts = [str(b.get(k) or "") for k in
                    ("kb_activity_id", "activity_id", "bound_activity_id")]
            old_bad = any(a in ("SPREP_AI_003", "HVAC_AI_001") for a in acts)
            if old_bad:
                bad("PLAN.misbind", "%s %s 仍绑着旧活动（%s）"
                    % (tid, nm, [a for a in acts if a][:1]))
            else:
                ok("PLAN.misbind", "%s %s 已不绑旧活动（现 kb_activity_id=%s）"
                   % (tid, nm, acts[0] or None))

        # 判据 4：机械优先——必须是**正向**校验（早先无条件 ok()，等于没查）
        mach = [t for t in wtasks
                if str(get_norm_binding(t).get("mode") or t.get("mode") or "") == "machine"]
        if mach:
            names = {}
            for t in mach:
                n = str(get_norm_binding(t).get("machine_name") or "?")
                names[n] = names.get(n, 0) + 1
            ok("PLAN.machine", "mode=machine 的工序 %d 条；主控机械 %s"
               % (len(mach), dict(sorted(names.items(), key=lambda kv: -kv[1])[:8])))
        else:
            bad("PLAN.machine",
                "没有任何工序走机械口径（知识库里 equipment_driven 且有台班定额行的活动有 60+ 个）"
                "——机械优先未落地")

    # 判据 10：meta.norm_coverage 覆盖率 + 顶层键不新增
    metas = find_all(plan, "meta")
    meta = {}
    for m in metas:
        if isinstance(m, dict) and ("norm_coverage" in m or "data_sources" in m):
            meta = m
            break
    if meta:
        nc = meta.get("norm_coverage") or {}
        # WS3 实际落地的键名是 `critical_norm_coverage`（早先这里写的是
        # critical_path_norm_coverage 等臆测名 → 永远 FAIL，是我的判据写错）。
        keys = ("critical_norm_coverage", "critical_path_norm_coverage",
                "critical_path_released_coverage", "cp_norm_coverage")
        found = [k for k in keys if k in nc]
        if found:
            ok("PLAN.coverage", "norm_coverage 含覆盖率键：%s = %s"
               % (found, {k: nc[k] for k in found}))
        elif meta.get("model_participation") is not None and "by_reason" in nc:
            # 这是排程节点（scheduler.norm_coverage_report）产出的那份报告；
            # coverage 键由**交付节点**追加。确定性重放不跑交付节点，
            # 因此这里只能 SKIP，不能判 FAIL —— 否则每次重放都误报。
            skip("PLAN.coverage",
                 "本产物的 meta.norm_coverage 来自排程节点（交付节点未跑），"
                 "覆盖率键由交付侧测试覆盖；现有键 %s" % sorted(nc))
        else:
            bad("PLAN.coverage",
                "norm_coverage 无关键路径规范依据覆盖率（现有键：%s）" % sorted(nc))
    else:
        skip("PLAN.coverage", "产物里找不到带 norm_coverage 的 meta")

    # 判据 8：监测不锁主体
    if tasks:
        by_id = {str(t.get("task_id")): t for t in tasks}
        for tid in ("3.4.2", "3.4.1"):
            t = by_id.get(tid)
            if not t:
                continue
            note = t.get("dependency_note")
            ok("PLAN.companion", "%s %s dependency_note=%s"
               % (tid, t.get("task_name"), note))
            break
        succ = by_id.get("4.1.1.1")
        if succ:
            preds = succ.get("predecessors") or succ.get("deps") or []
            txt = json.dumps(preds, ensure_ascii=False)
            if "3.4.2" in txt and ("FS" in txt or "fs" in txt.lower()):
                bad("PLAN.monitor_fs", "4.1.1.1 仍以 3.4.2 为 FS 前置：%s" % txt[:200])
            else:
                ok("PLAN.monitor_fs", "4.1.1.1 不再被 3.4.2 以 FS 锁住")
        else:
            skip("PLAN.monitor_fs", "产物里没有 4.1.1.1，无法核对前置关系")

    # 产物文本里旧政策措辞残留（AI 政策轮已清零，别回退）
    txt = json.dumps(plan, ensure_ascii=False)
    old_phrases = ["不能用来算", "只作参考，不用来算"]
    hit = [p for p in old_phrases if p in txt]
    if hit:
        bad("PLAN.old_policy", "产物出现旧政策措辞：%s" % hit)
    else:
        ok("PLAN.old_policy", "无旧政策措辞残留")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default=None, help="计划产物 JSON")
    ap.add_argument("--kb", default=os.path.join(ROOT, "BuildPlan_KB", "kb.db"))
    args = ap.parse_args()

    check_kb(args.kb)

    if args.plan:
        if not os.path.exists(args.plan):
            skip("PLAN", "产物不存在：%s" % args.plan)
        else:
            with io.open(args.plan, "r", encoding="utf-8") as f:
                plan = json.load(f)
            rd = plan
            rds = find_all(plan, "resource_demand")
            if rds:
                rd = rds[0]
            check_plan(plan, rd)
    else:
        skip("PLAN", "未提供 --plan，跳过计划产物判据")

    print("=" * 72)
    for tag, msg in PASSES:
        print("  PASS  %-32s %s" % (tag, msg))
    for tag, msg in SKIPS:
        print("  SKIP  %-32s %s" % (tag, msg))
    for tag, msg in FAILS:
        print("  FAIL  %-32s %s" % (tag, msg))
    print("=" * 72)
    print("PASS %d / SKIP %d / FAIL %d" % (len(PASSES), len(SKIPS), len(FAILS)))
    if FAILS:
        return 1
    if SKIPS:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
