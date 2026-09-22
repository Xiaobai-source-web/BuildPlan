# -*- coding: utf-8 -*-
"""列出工作面容量表里"**旧上限被新口径顶上去过**"的行 —— 第 41 轮审计层（只读）。

要解决什么
----------
`Workface_Capacity_Rule` 里同时存着**三代**人数上限，谁都没有被复核过：

  · `legacy_max_labor` —— 旧 v1 表的上限（实测 4/5/6/8/10/12/14/15/16）。
    它**至今仍是**写进计划的 `leaf.workface_capacity.max_labor` 的那个值
    （`kb.py::_CAPACITY_COMPAT_FROM_COLUMN` 明写：取 `legacy_max_labor`，
    **不是** `crew_max` 的别名）；
  · `crew_max` —— v2 标定上限，全表只有 {3,4,5,6,8,12,15,16} 八档；
  · `effective_crew_max = max(crew_max, min(40, ceil(crew_base×2.5)))`
    —— 排程**实际**用的上限（`pipeline/nodes/scheduler.py`，只抬不降）。

实测（本仓库 `BuildPlan_KB/kb.db`，487 行）：**387 行** `legacy_max_labor < crew_max`，
其中 **385 行**再被公式抬高一次（如 base=8、crew_max=15 → 实际 20 人）；
`review_state` 全 `pending`、`confidence` 全 `LOW`、`source_type` 全 `ai_estimate`。
也就是说：一份计划的工期按"20 人一个施工段"排出来，交付物上印的却是"旧上限 12 人"，
中间那次抬高**没有任何一处提示**。

本工具做的事
------------
把那 387 行按 `(crew_base, crew_max, legacy_max_labor, effective_crew_max)` **分档**
列出来（实测 26 档），每档给出命中行数、工程类型、计量单位、样本 activity_id。
**只读**：SQLite 以 `mode=ro` 打开，脚本里没有一句 UPDATE/INSERT。

用法::

    python backend/tools/list_cmax_review.py                # 全部档位
    python backend/tools/list_cmax_review.py --stats        # 只印汇总事实
    python backend/tools/list_cmax_review.py --limit 8      # 只看前 8 档
    python backend/tools/list_cmax_review.py --json         # 机器可读（进 meta 用这个）
    python backend/tools/list_cmax_review.py --plan backend/plans/plan_sample3_after_fix.json
                                                            # 顺带跑完整三项审计（②③）

退出码：0 = 列出来了；2 = KB 读不到 / 没有容量表（**不是**"没问题"）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Windows 控制台默认 GBK，`m²` / `m³` 这类字符会直接把脚本打断。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pipeline import audit_scope as A                              # noqa: E402
from pipeline import config                                        # noqa: E402


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="列出工作面容量表里旧上限被新口径顶上去的行（只读，不改库）")
    ap.add_argument("--db", default=None,
                    help="KB 路径（默认 pipeline.config.KB_DB_PATH）")
    ap.add_argument("--limit", type=int, default=0, help="只印前 N 档（0 = 全部）")
    ap.add_argument("--min-rows", type=int, default=A.CMAX_FLOOR_ROWS,
                    help="某档至少这么多行才列出（默认 %d）" % A.CMAX_FLOOR_ROWS)
    ap.add_argument("--min-lift", type=float, default=A.CMAX_FLOOR_LIFT,
                    help="或抬高幅度 ≥ 这么多人（默认 %s）" % A.CMAX_FLOOR_LIFT)
    ap.add_argument("--stats", action="store_true", help="只印汇总事实，不印档位表")
    ap.add_argument("--json", action="store_true", help="输出 JSON（含档位清单）")
    ap.add_argument("--plan", default=None,
                    help="顺带对这份 plan.json 跑完整审计（②定额离散 / ③重复范围）")
    return ap.parse_args(argv)


def _fmt(value):
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _print_summary(res, db_path):
    print("KB：%s" % db_path)
    print("表：%s" % res["table"])
    if res["status"] != "ok":
        print("!! %s" % res.get("note"))
        return 2
    print("总行数：%d" % res["total_rows"])
    print("旧上限 < 新上限（legacy_max_labor < crew_max）：%d 行" % res["legacy_below_crew_max"])
    print("旧上限 > 新上限（反向分歧）：%d 行" % res["legacy_above_crew_max"])
    print("任一上限为空：%d 行" % res["null_ceiling_rows"])
    print("分档数（base / crew_max / legacy / 实际生效）：%d，通过阈值展示 %d 档"
          % (res["distinct_bands"], res.get("bands_shown", len(res["bands"]))))
    print("复核状态：%s" % _pairs(res["review_state"]))
    print("置信度：%s" % _pairs(res["confidence"]))
    print("口径：%s" % res["note"])
    return 0


def _pairs(items):
    if not items:
        return "（无）"
    return "；".join("%s=%d 行" % (it["value"], it["rows"]) for it in items)


def _print_bands(res, limit):
    bands = res["bands"]
    if limit:
        bands = bands[:limit]
    if not bands:
        print("（没有档位通过阈值 —— 注意：这**不代表**全表没有分歧，只看通过的档）")
        return
    print("")
    print("档位（按命中行数降序）：")
    print("  %-6s %-9s %-10s %-10s %-7s %-8s %s"
          % ("base", "crew_max", "旧max_labor", "实际生效", "抬高", "行数", "工程类型"))
    for b in bands:
        print("  %-6s %-9s %-10s %-10s %-7s %-8d %s"
              % (_fmt(b["crew_base"]), _fmt(b["crew_max"]), _fmt(b["legacy_max_labor"]),
                 _fmt(b["effective_crew_max"]), _fmt(b["lift_over_legacy"]), b["rows"],
                 "/".join("%s(%d)" % (x["value"], x["rows"]) for x in b["work_type_l3"])))
        print("        样本：%s" % "、".join(b["sample_activity_ids"]))
    tail = ("只印了前 %d 档（--limit %d）；共 %d 档。"
            % (limit, limit, res.get("bands_shown", len(res["bands"]))))
    if limit and limit < len(res["bands"]):
        print("        " + tail)


def _print_plan(plan_path):
    if not os.path.exists(plan_path):
        print("!! 计划文件不存在：%s" % plan_path)
        return 2
    with open(plan_path, encoding="utf-8") as fh:
        plan = json.load(fh)
    audit = A.scope_audit(plan)
    print("")
    print(A.scope_audit_summary(audit))
    print("")
    print("meta[\"scope_audit\"] 用 --json 取（%d 字节）"
          % len(json.dumps(audit, ensure_ascii=False)))
    return 0


def main(argv=None):
    args = _parse_args(argv)
    db_path = args.db or str(config.KB_DB_PATH)
    res = A.cmax_review_rows(db_path=db_path, min_rows=args.min_rows,
                             min_lift=args.min_lift)
    if args.json:
        payload = {"db": db_path, "cmax": res}
        if args.plan:
            with open(args.plan, encoding="utf-8") as fh:
                payload["scope_audit"] = A.scope_audit(json.load(fh), db_path=db_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        code = _print_summary(res, db_path)
        if code:
            return code
        if not args.stats:
            _print_bands(res, args.limit)
    if args.plan and not args.json:
        return _print_plan(args.plan) or 0
    return 0 if res["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
