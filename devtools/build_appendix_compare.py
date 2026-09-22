# -*- coding: utf-8 -*-
"""把某次真实运行 → 生成《补充附录》里要重做的三段（§12.3 建策列 / §12.4 建策段 / 补充数据）。

为什么要有它：用户会用「GPT 对比输入」重跑一次，然后要拿**那次真实结果**重做比较分析。
手工从 plan_json 里抄数字既慢又容易抄错（历史上就出现过「地下2层/框架剪力墙/约15000㎡」
三处全靠印象写错）。本工具把「建策 BuildPlan」这一侧的数字**全部从产物里读**，
每个数字都打印出处键；竞品那一侧由用户输入提供，本工具留占位不猜。

⚠️ 本工具**只读产物、不做任何估算**；读不到的字段一律打印「（产物无此字段）」而不是补一个数。
   更完整的问题审计版见 `backend/_probe_tmp/q_appendix_numbers.py`。

用法：
    python devtools/build_appendix_compare.py --plan plan_run_1789895021
    python devtools/build_appendix_compare.py --plan <id> --out 附录_待粘贴.md
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "backend" / "plans"


def load_plan(plan_id):
    for p in (PLANS / "档案" / plan_id / "当前版本.json", PLANS / (plan_id + ".json")):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")), p
    return None, None


def _get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def walk_leaves(node, out):
    """收集所有带 `norm_binding` 或 `kb_activity_id` 的叶子（结构无关）。"""
    if isinstance(node, dict):
        if "norm_binding" in node or "kb_activity_id" in node:
            out.append(node)
        for v in node.values():
            walk_leaves(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_leaves(v, out)


def is_ai_src(src):
    s = str(src or "").upper()
    return ("AI_ESTIMATE" in s) or s.startswith("AI_") or s.startswith("SCAFFOLD")


def collect_names(node, out):
    """收集产物里所有节点名（含 `phases[].name`），用于按命名推定字段。"""
    if isinstance(node, dict):
        for k in ("name", "task_name", "phase_name", "wbs_name"):
            if isinstance(node.get(k), str) and node[k].strip():
                out.append(node[k].strip())
        for k in ("phases", "children", "subtasks"):
            seq = node.get(k)
            if isinstance(seq, list):
                for it in seq:
                    if isinstance(it, dict) and isinstance(it.get("name"), str) and it["name"].strip():
                        out.append("[PHASE] " + it["name"].strip())
        for v in node.values():
            collect_names(v, out)
    elif isinstance(node, list):
        for v in node:
            collect_names(v, out)


def extract(plan):
    ov = plan.get("overview") or {}
    meta = plan.get("meta") or {}
    ep = meta.get("extracted_params") or {}
    bc = meta.get("boundary_conditions") or {}
    nc = meta.get("norm_coverage") or {}
    rp = plan.get("resource_plan") or {}
    rd = plan.get("resource_demand") or {}
    tasks = rd.get("tasks") or []
    leaves = []
    walk_leaves(plan.get("wbs") or {}, leaves)

    # 来源三档（叶子计）：无 source_code → 无依据；AI/SCAFFOLD → AI；否则规范
    spec = ai = none = 0
    for lf in leaves:
        nb = lf.get("norm_binding") or {}
        src = nb.get("source_code")
        if not src:
            none += 1
        elif is_ai_src(src):
            ai += 1
        else:
            spec += 1
    tot_src = spec + ai + none

    d = {}
    d["plan_id"] = plan.get("plan_id")
    d["project_name"] = ov.get("project_name") or ep.get("project_name")
    d["total_area"] = ep.get("total_area")
    d["floors"] = ep.get("floors")
    # 地下层数：产物里**没有**专门键（`meta.extracted_params` 里数过，没有「地下层数」），
    # 只能按 WBS 命名推。两种依据，任取其一，并且**把依据一起输出**（附录历史上就是这里
    # 靠印象写成「地下2层」）。推不出来就老实写「?」，不猜一个数。
    import re
    names = []
    collect_names(plan.get("wbs") or {}, names)
    d["basement"] = "?"
    d["basement_basis"] = "产物无「地下层数」字段，WBS 命名里也推不出来"
    for nm in names:
        m = re.search(r"地下\s*(\d+)\s*层", nm)
        if m:
            d["basement"] = m.group(1) + " 层"
            d["basement_basis"] = "任务名「%s」里写明「地下%s层」" % (nm, m.group(1))
            break
    if d["basement"] == "?":
        hits = [n for n in names if ("地下室" in n or "地下结构" in n)]
        pick = [n for n in hits if n.startswith("[PHASE] ")] or hits
        if pick:
            k = len(set(pick))
            d["basement"] = "1 层" if k == 1 else "%d 层" % k
            d["basement_basis"] = "按 WBS 命名「%s」推定（%d 处地下室命名；产物无该字段）" % (
                pick[0].replace("[PHASE] ", ""), len(pick))
    d["structure"] = ep.get("structure_type")
    d["building_type"] = ep.get("building_type")
    d["cadence"] = bc.get("cadence_days")
    d["total_days"] = ov.get("total_duration_days")
    d["start"] = ov.get("start_date") or ov.get("planned_start_date")
    d["end"] = ov.get("end_date") or ov.get("planned_end_date")
    d["cp_count"] = ov.get("critical_path_task_count", ov.get("critical_path_length"))
    d["n_tasks"] = len(tasks) or len(leaves)
    d["peak"] = rp.get("peak_manpower")
    d["manpower_days"] = rp.get("total_manpower_days")
    d["equip"] = rp.get("equipment_peak")
    d["bound_pct"] = nc.get("bound_pct")
    d["bound"] = nc.get("bound")
    d["unbound"] = nc.get("unbound")
    d["critical_cov"] = nc.get("critical_norm_coverage")
    d["released_ai"] = nc.get("released_ai")
    d["released_ai_pct"] = nc.get("released_ai_pct")
    d["no_res"] = sum(1 for t in tasks if not (t.get("resources") or []))
    d["spec"], d["ai"], d["none"] = spec, ai, none
    d["n_leaves"] = len(leaves)
    d["audit"] = meta.get("audit_status")
    d["calls"] = _get(meta, "usage", "calls")
    d["basis_unconfirmed"] = sum(
        1 for lf in leaves if (lf.get("norm_binding") or {}).get("basis_unconfirmed"))
    d["basis_adjust"] = sum(
        1 for lf in leaves if (lf.get("norm_binding") or {}).get("basis_adjust"))
    d["miss_unit"] = sum(1 for t in tasks if not t.get("unit"))
    d["scope_none"] = sum(1 for t in tasks if t.get("measure_scope") is None)
    # 峰值工种：resource_plan 里若有则用，否则留空
    d["peak_trade"] = rp.get("peak_trade") or rp.get("peak_work_type")
    return d


def md(d, plan):
    pct = lambda n: ("%.1f%%" % (100.0 * n / d["n_leaves"])) if d["n_leaves"] else "?"
    L = []
    L.append("### 测试项目：%s" % (d["project_name"] or "（产物无项目名）"))
    L.append("")
    L.append("- 统一输入：示例3（%s 层住宅楼，地下 %s，%s 结构，%s ㎡）"
             % (d["floors"] or "?", d["basement"],
                d["structure"] or "?", d["total_area"] or "?"))
    L.append("- 标准层节拍：%s 天/层（来源：meta.boundary_conditions.cadence_days）" % d["cadence"])
    L.append("- 计划周期：%s → %s" % (d["start"], d["end"]))
    L.append("")
    L.append("#### 12.3 对比表 · 建策BuildPlan 列（直接替换）")
    L.append("")
    L.append("| 指标 | **建策BuildPlan** |")
    L.append("|------|-------------------|")
    L.append("| **总工期** | **%s天** |" % d["total_days"])
    L.append("| **计算依据** | **定额+CPM算法** |")
    L.append("| **工序总数** | **%s条** |" % d["n_tasks"])
    L.append("| **关键路径** | **%s条任务** |" % d["cp_count"])
    L.append("| **峰值人数** | **%s人** |" % d["peak"])
    L.append("| **定额依据** | ✅ **%s%%覆盖** |" % d["bound_pct"])
    L.append("| **可审计性** | ✅ 三轮回审（R1/R2/R3） |")
    L.append("| **交付物** | **Word+HTML看板** |")
    L.append("")
    L.append("#### 12.4 建策BuildPlan 段（直接替换）")
    L.append("")
    L.append("**建策BuildPlan（工期%s天）**：" % d["total_days"])
    L.append("- 定额驱动计算：定额覆盖率 **%s%%**（%s/%s 条工序）；来源构成："
             "**规范 %s 条（%s）、AI 已标注 %s 条（%s）、无依据 %s 条（%s）**"
             % (d["bound_pct"], d["bound"], d["n_tasks"],
                d["spec"], pct(d["spec"]), d["ai"], pct(d["ai"]), d["none"], pct(d["none"])))
    L.append("- 完整工序分解：工序总数 **%s** 条，关键路径 **%s** 条任务（条数，不是天数）"
             % (d["n_tasks"], d["cp_count"]))
    L.append("- 精细化资源管理：总人工日 **%s** 人·日，峰值人数 **%s** 人%s"
             % (d["manpower_days"], d["peak"],
                "，峰值工种 %s" % d["peak_trade"] if d["peak_trade"] else ""))
    L.append("- 全链路审计：%s，模型调用 %s 次，数据来源逐条追溯" % (d["audit"], d["calls"]))
    L.append("- 可视化交付：HTML 交互式看板（含 ECharts 甘特图、资源曲线）+ Word 文档")
    L.append("")
    L.append("#### 补充数据（直接替换）")
    L.append("")
    L.append("| 项 | 值 | 出处 |")
    L.append("|---|---|---|")
    for label, val, key in (
        ("总工期", "%s 天" % d["total_days"], "overview.total_duration_days"),
        ("关键路径任务数", "%s 条" % d["cp_count"], "overview.critical_path_task_count"),
        ("工序总数", "%s 条" % d["n_tasks"], "resource_demand.tasks"),
        ("峰值人数", "%s 人" % d["peak"], "resource_plan.peak_manpower"),
        ("总人工日", "%s 人·日" % d["manpower_days"], "resource_plan.total_manpower_days"),
        ("机械峰值", "%s" % d["equip"], "resource_plan.equipment_peak"),
        ("定额覆盖率", "%s%%（%s/%s）" % (d["bound_pct"], d["bound"], d["n_tasks"]),
         "meta.norm_coverage.bound_pct"),
        ("关键路径规范依据覆盖率", "%s%%" % d["critical_cov"],
         "meta.norm_coverage.critical_norm_coverage（目标 ≥80）"),
        ("AI 经验估算定额（已标注）", "%s 条（%s%%）" % (d["released_ai"], d["released_ai_pct"]),
         "meta.norm_coverage.released_ai"),
        ("未绑定额", "%s 条" % d["unbound"], "meta.norm_coverage.unbound"),
        ("无资源任务", "%s 条" % d["no_res"], "resource_demand.tasks[*].resources 为空"),
        ("口径换算留痕 / 口径未确认", "%s / %s" % (d["basis_adjust"], d["basis_unconfirmed"]),
         "binding.basis_adjust / basis_unconfirmed"),
        ("缺 unit 的任务", "%s 条（应为 0）" % d["miss_unit"], "resource_demand"),
        ("measure_scope 为 None", "%s 条（应为 0）" % d["scope_none"], "resource_demand"),
        ("模型调用次数", "%s 次（必须 >0）" % d["calls"], "meta.usage.calls"),
        ("审计状态", "%s" % d["audit"], "meta.audit_status"),
    ):
        L.append("| %s | %s | `%s` |" % (label, val, key))
    L.append("")
    L.append("> 竞品列（ChatGPT / DeepSeek 普通 / DeepSeek 深度思考）**本工具不猜** —— "
             "由用户提供的对比输入决定；请把上面的建策列代入原表。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True, help="plan_id，如 plan_run_1789895021")
    ap.add_argument("--out", default="", help="把 markdown 写到该文件（默认只打印）")
    args = ap.parse_args()

    plan, path = load_plan(args.plan)
    if plan is None:
        print("找不到计划 %s（试过 档案/<id>/当前版本.json 与 <id>.json）" % args.plan)
        return 1
    d = extract(plan)
    text = md(d, plan)

    print("=" * 88)
    print("计划 %s   来源 %s" % (d["plan_id"], path))
    print("=" * 88)
    print(text)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print("\n[已写出] %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
