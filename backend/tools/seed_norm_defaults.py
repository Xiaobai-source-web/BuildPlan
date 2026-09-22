"""初始化 L4_Norm_Default —— 把 KB 里散着的定额收敛成"每条 L4 一行默认值"。

用法（在仓库根目录）::

    python backend/tools/seed_norm_defaults.py --dry-run     # 只报告，不写库
    python backend/tools/seed_norm_defaults.py               # 写入（全部 pending）

写入后**仍然是 pending**，一条都不会改变工期。人工审定用::

    python backend/tools/approve_norm_default.py --list
    python backend/tools/approve_norm_default.py --approve FORM_NEW_FOUND
    python backend/tools/approve_norm_default.py --approve-all --confidence verified

## 取值规则（每条 L4 × 计量单位 一行）

1. **候选行**：`Norm_Labor_Table` 里该 `activity_id` 的全部行。
2. **单位**：取该活动在 `L4_Activity_Dictionary.unit` 的计量单位（没有则取候选行里
   `quantity_unit` 出现最多的那个）。
3. **默认值**：候选中位数（`labor_norm_value` 排序取中间）。
   —— 不取首行/最小行：上一版"兜底取首行"正是铝模被按"IE 垫层/带形/木模板
   0.025 工日/m²"算的原因。中位数是"最不坏"的无信息选择，且**必须人工复核**。
4. **条件组合**：取中位数那一行的 `condition_combination`，只作溯源展示。
5. **默认班组**：`Workface_Capacity_Rule.crew_base`（有则填）。它是"一个施工段
   的基准班组"，供人工参考；不进工期计算（工期口径仍由 scheduler 的工作面容量决定）。
6. **confidence**：
     - `L4_Activity_Dictionary.status == 'verified'` → verified
     - `== 'parsed'`                                  → parsed
     - 其它 / 没有定额行                              → estimated
7. **没有定额行的 L4**：**不造行**。列进报告的"缺失清单"，等人工补或确认。
   —— 宁可在排程里退回目标工期，也不拿一个编出来的数去顶。

脚本幂等：已存在的 (activity_id, quantity_unit, norm_kind) 不覆盖（除非 --force，
且 --force 也只会把 review_state 重置为 pending）。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pipeline import kb                       # noqa: E402
from pipeline import norm_defaults as nd      # noqa: E402


def _activity_status():
    """{activity_id: (work_type_id, activity_name, unit, status)}"""
    out = {}
    for row in kb._query_all(
            "SELECT activity_id, work_type_id, activity_name, unit, status "
            "FROM L4_Activity_Dictionary"):
        aid, wt, name, unit, status = (list(row) + [None] * 5)[:5]
        out[str(aid)] = (str(wt or ""), str(name or ""), str(unit or ""),
                         str(status or ""))
    return out


def _labor_rows():
    """{activity_id: [ (norm_value, norm_unit, condition_combination, quantity_unit) ]}"""
    out = {}
    for row in kb._query_all(
            "SELECT activity_id, labor_norm_value, labor_norm_unit, "
            "condition_combination, quantity_unit FROM Norm_Labor_Table"):
        aid, val, unit, cond, qunit = (list(row) + [None] * 5)[:5]
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        out.setdefault(str(aid), []).append(
            (v, str(unit or ""), str(cond or ""), str(qunit or "")))
    return out


def _crew_base():
    """{activity_id: crew_base}（工作面容量 v2 的基准班组）。"""
    out = {}
    try:
        for row in kb._query_all(
                "SELECT activity_id, crew_base FROM Workface_Capacity_Rule"):
            aid, cb = (list(row) + [None] * 2)[:2]
            try:
                out[str(aid)] = int(float(cb))
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return out


def _dominant_unit(rows, declared_unit):
    """计量单位：优先字典声明的，其次候选行里出现最多的。"""
    if declared_unit:
        return declared_unit
    counts = {}
    for _v, _u, _c, qunit in rows:
        counts[qunit] = counts.get(qunit, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: (kv[1], kv[0] == ""))[0]


def _confidence(status, has_rows):
    if not has_rows:
        return nd.CONF_ESTIMATED
    if status == "verified":
        return nd.CONF_VERIFIED
    if status == "parsed":
        return nd.CONF_PARSED
    return nd.CONF_ESTIMATED


def _pick_row(rows, unit):
    """在中位数那一行上取条件组合（中位数即默认值）。"""
    vals = sorted(r[0] for r in rows)
    median = statistics.median(vals)
    for v, u, cond, qunit in rows:            # 取离中位数最近的一行做溯源
        if v == median:
            return median, u, cond, qunit
    best = min(rows, key=lambda r: abs(r[0] - median))
    return best[0], best[1], best[2], best[3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    ap.add_argument("--force", action="store_true",
                    help="已存在的行也重写（review_state 重置为 pending）")
    ap.add_argument("--json", default="", help="把报告写到该路径（JSON）")
    args = ap.parse_args()

    if not nd.ensure_table():
        print("!! 无法建表 L4_Norm_Default（KB 不可写？）")
        return 1

    acts = _activity_status()
    norms = _labor_rows()
    crews = _crew_base()

    conn = kb._connect()
    inserted = updated = 0
    report = {"rows": [], "missing_norm": [], "by_confidence": {}}

    for aid in sorted(acts):
        _wt, name, declared_unit, status = acts[aid]
        rows = norms.get(aid) or []
        conf = _confidence(status, bool(rows))
        report["by_confidence"][conf] = report["by_confidence"].get(conf, 0) + 1

        if not rows:
            report["missing_norm"].append(
                {"activity_id": aid, "activity_name": name, "status": status})
            continue

        unit = _dominant_unit(rows, declared_unit)
        cand = [r for r in rows if r[3] == unit] or rows
        value, norm_unit, cond, _q = _pick_row(cand, unit)
        crew = crews.get(aid)
        reason = ("KB 无该 L4 的定额行" if not rows else
                  "自动收敛：%d 行候选取中位数，来源 %s（status=%s），待人工审定"
                  % (len(cand), "KB", status))
        raw = {
            "activity_id": aid,
            "quantity_unit": unit,
            "norm_kind": nd.KIND_LABOR,
            "condition_key": (cond or "")[:400],
            "norm_value": float(value),
            "norm_unit": norm_unit,
            "source_code": "KB_Norm_Labor_Table",
            "source_kind": "kb_%s" % status,
            "confidence": conf,
            "default_crew": crew,
            "review_state": nd.STATE_PENDING,
            "notes": reason,
        }
        report["rows"].append({
            "activity_id": aid, "activity_name": name,
            "quantity_unit": unit, "norm_value": float(value),
            "norm_unit": norm_unit, "confidence": conf,
            "candidates": len(cand), "row_condition": (cond or "")[:120],
            "default_crew": crew,
        })

        if args.dry_run:
            continue
        exists = conn.execute(
            "SELECT 1 FROM %s WHERE activity_id=? AND quantity_unit=? AND norm_kind=?"
            % nd.TABLE, (aid, unit, nd.KIND_LABOR)).fetchone()
        if exists and not args.force:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO %s (activity_id, quantity_unit, norm_kind, "
            "condition_key, norm_value, norm_unit, source_code, source_kind, "
            "confidence, default_crew, review_state, reviewed_by, reviewed_at, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)" % nd.TABLE,
            (raw["activity_id"], raw["quantity_unit"], raw["norm_kind"],
             raw["condition_key"], raw["norm_value"], raw["norm_unit"],
             raw["source_code"], raw["source_kind"], raw["confidence"],
             raw["default_crew"], raw["review_state"], "", "", raw["notes"]))
        if exists:
            updated += 1
        else:
            inserted += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    print("== L4_Norm_Default 初始化 ==")
    print("  L4 活动总数        : %d" % len(acts))
    print("  新建行 / 覆盖行    : %d / %d" % (inserted, updated))
    print("  分档               : %s" % json.dumps(report["by_confidence"],
                                                  ensure_ascii=False))
    print("  无定额行（列出缺口）: %d 条 -> 见报告 missing_norm"
          % len(report["missing_norm"]))
    print("  全部 review_state  : pending（**不放行**，一条都不改工期）")
    if report["by_confidence"]:
        print("  下一步             : python backend/tools/approve_norm_default.py --list")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print("  报告               : %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
