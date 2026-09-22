# -*- coding: utf-8 -*-
"""两份计划 JSON 的逐项对比（改前 vs 改后），只读，不调 LLM。

用法：
    python devtools/compare_plans.py --old backend/plans/plan_run_1789827002.json \
                                     --new backend/plans/plan_run_<新的>.json
    python devtools/compare_plans.py --old <a> --new <b> --json out.json
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "devtools"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 复用复跑脚本里的 summarise()，保证两边口径完全一致
_spec = importlib.util.spec_from_file_location("rerun_sample3", ROOT / "devtools" / "rerun_sample3.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
summarise = _mod.summarise


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def row(label, a, b, note=""):
    sa, sb = str(a), str(b)
    mark = "  " if sa == sb else "≠ "
    print("  %s%-26s | %-38s | %-38s %s" % (mark, label, sa[:38], sb[:38], note))


def dict_diff(title, a, b):
    print("\n--- %s ---" % title)
    keys = sorted(set(a) | set(b))
    for k in keys:
        va, vb = a.get(k), b.get(k)
        mark = "  " if va == vb else "≠ "
        print("  %s%-22s 改前=%s  改后=%s" % (mark, k, va, vb))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    so = summarise(load(args.old))
    sn = summarise(load(args.new))

    print("=" * 120)
    print("计划对比  改前=%s   改后=%s" % (so["plan_id"], sn["plan_id"]))
    print("=" * 120)

    row("项目名", so["project"], sn["project"])
    row("总工期(天)", so["total_duration_days"], sn["total_duration_days"])
    row("任务数", so["task_count"], sn["task_count"])
    row("无资源任务数", so["tasks_without_resource"], sn["tasks_without_resource"])
    row("进度表行数", so["schedule_rows"], sn["schedule_rows"])
    rpo, rpn = so["resource_plan"], sn["resource_plan"]
    row("总人工日", rpo["total_manpower_days"], rpn["total_manpower_days"])
    row("峰值人数", rpo["peak_manpower"], rpn["peak_manpower"])
    row("峰值来源", rpo["peak_manpower_source"], rpn["peak_manpower_source"])
    row("曲线峰值", rpo["curve_peak_manpower"], rpn["curve_peak_manpower"])
    row("申报峰值", rpo["declared_peak_manpower"], rpn["declared_peak_manpower"])
    row("资源曲线工期", (so.get("schedule_versions") or {}).get("resource_ok_days"),
        (sn.get("schedule_versions") or {}).get("resource_ok_days"))

    print("\n--- 机械峰值（resource_plan.equipment_peak）---")
    eqo, eqn = rpo["equipment_peak"] or {}, rpn["equipment_peak"] or {}
    for k in sorted(set(eqo) | set(eqn)):
        mark = "  " if eqo.get(k) == eqn.get(k) else "≠ "
        print("  %s%-28s 改前=%-8s 改后=%s" % (mark, k, eqo.get(k), eqn.get(k)))
    print("  机械种类数：改前 %d → 改后 %d" % (len(eqo), len(eqn)))

    print("\n--- 配员峰值（machine_crew_peak）---")
    co, cn = rpo["machine_crew_peak"] or {}, rpn["machine_crew_peak"] or {}
    for k in sorted(set(co) | set(cn)):
        mark = "  " if co.get(k) == cn.get(k) else "≠ "
        print("  %s%-28s 改前=%-8s 改后=%s" % (mark, k, co.get(k), cn.get(k)))

    dict_diff("任务级机械名清单（机械名 → 任务数/单任务最大 per_day）",
              so["machine_names"], sn["machine_names"])
    dict_diff("配员名清单", so["crew_names"], sn["crew_names"])
    dict_diff("工种名清单", so.get("trade_names") or {}, sn.get("trade_names") or {})

    print("\n--- 设备对账（meta.equipment_binding）---")
    print("  改前：%s" % json.dumps(so["equipment_binding"], ensure_ascii=False))
    print("  改后：%s" % json.dumps(sn["equipment_binding"], ensure_ascii=False))

    print("\n--- 无资源原因分布 ---")
    dict_diff("无资源原因", so["unbound_reasons"], sn["unbound_reasons"])

    print("\n--- ALC 墙板任务（%d 条 → %d 条）---" % (len(so["alcs"]), len(sn["alcs"])))
    by_id_o = {a["task_id"]: a for a in so["alcs"]}
    by_id_n = {a["task_id"]: a for a in sn["alcs"]}
    shown = 0
    for tid in sorted(set(by_id_o) | set(by_id_n)):
        a, b = by_id_o.get(tid), by_id_n.get(tid)
        ra = (a or {}).get("resources")
        rb = (b or {}).get("resources")
        if ra == rb and (a or {}).get("assumed") == (b or {}).get("assumed"):
            continue
        print("  ≠ %-14s 改前资源=%s" % (tid, ra))
        print("    %-14s 改后资源=%s" % ("", rb))
        if (b or {}).get("assumed"):
            print("    %-14s 假定=%s" % ("", str((b or {}).get("assumed"))[:160]))
        shown += 1
        if shown >= 6:
            print("  …（其余同理，改后共 %d 条带资源）"
                  % sum(1 for a in sn["alcs"] if a["resources"]))
            break

    print("\n--- 定额覆盖率 / 可信度 ---")
    print("  改前 coverage=%s" % json.dumps(so["norm_coverage"], ensure_ascii=False)[:400])
    print("  改后 coverage=%s" % json.dumps(sn["norm_coverage"], ensure_ascii=False)[:400])

    print("\n--- token 用量 ---")
    print("  改前 %s" % so["usage"])
    print("  改后 %s" % sn["usage"])

    if args.json:
        Path(args.json).write_text(json.dumps({"old": so, "new": sn}, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print("\n完整对比落盘：%s" % args.json)


if __name__ == "__main__":
    main()
