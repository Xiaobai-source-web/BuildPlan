# -*- coding: utf-8 -*-
"""WS6 报告生成器：B1（90 行 AI 定额审核表）+ C12（判废清单）+ C1–C11 结果汇总。

只读，不写库。用法：
  python devtools/kb_migrate_ws6_report.py --db BuildPlan_KB\\kb.db.migrated
  python devtools/kb_migrate_ws6_report.py --db ... --out devtools/_dev-notes/ws6_tables.md
"""
from __future__ import print_function

import argparse
import io
import json
import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 措施 / 验收类 work_type —— 规范劳动定额本来就不覆盖，AI 估算属合理兜底
MEASURE_WORK_TYPES = (
    "tech_prep", "site_prep", "material_prep", "equip_prep", "temp_util",
    "hidden_accept", "sub_accept", "div_accept", "final_accept",
    "demolition", "demobilization",
)


def q(cur, sql, args=()):
    return cur.execute(sql, args).fetchall()


def ones(cur, sql, args=()):
    return cur.execute(sql, args).fetchone()[0]


# ------------------------------------------------------------------ B1

def b1_rows(cur):
    """90 行 AI_ESTIMATE_V1 的逐行审核（只读）。"""
    rows = q(cur, """
        SELECT n.norm_id, n.activity_id, d.activity_name, d.work_type_id, d.unit,
               n.quantity_unit, n.labor_norm_value, n.labor_norm_unit, n.measure_scope,
               n.productivity_value
        FROM Norm_Labor_Table n
        LEFT JOIN L4_Activity_Dictionary d ON d.activity_id = n.activity_id
        WHERE n.source_code = 'AI_ESTIMATE_V1'
        ORDER BY n.norm_id""")
    out = []
    for (nid, aid, name, wtype, aunit, qunit, val, unit, scope, prod) in rows:
        has_regional = ones(cur, """
            SELECT COUNT(*) FROM Norm_Labor_Table
            WHERE activity_id = ? AND quantity_unit = ?
              AND source_type = 'regional_quota'""", (aid, qunit)) or 0
        if has_regional:
            verdict, why = u"是", (u"同单位已有 %d 条规范定额行（regional_quota），可替换" % has_regional)
        elif (wtype or "") in MEASURE_WORK_TYPES:
            verdict, why = u"否", u"属措施/验收类（work_type=%s），规范劳动定额不覆盖，AI 估算属合理兜底" % wtype
        else:
            verdict, why = u"是", u"实体工程（work_type=%s）应有规范定额来源，AI 值不可长期替代" % wtype
        out.append(dict(norm_id=nid, activity_id=aid, activity_name=name, work_type=wtype,
                        value=val, unit=unit, quantity_unit=qunit,
                        scope=scope or u"", verdict=verdict, why=why,
                        productivity=prod))
    return out


# ------------------------------------------------------------------ C12

#: C12 候选判废表 —— 只做「是否被非测试后端代码引用」的客观统计
C12_CANDIDATES = (
    "Norm_Adjustment", "Norm_Adjustment_Target", "data_quality_log",
    "Condition_Dictionary",
)


def scan_backend_refs(root):
    """扫 backend/（排除 __pycache__/_probe_tmp/tests），返回 表名 -> [命中文件]。"""
    hits = {t: [] for t in C12_CANDIDATES}
    base = os.path.join(root, "backend")
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "_probe_tmp", "tests")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                txt = io.open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            for t in C12_CANDIDATES:
                if t in txt:
                    hits[t].append(os.path.relpath(p, root))
    return hits


def c12_rows(cur, root):
    hits = scan_backend_refs(root)
    out = []
    for t in C12_CANDIDATES:
        try:
            n = ones(cur, "SELECT COUNT(*) FROM [%s]" % t)
        except Exception:
            n = None
        out.append(dict(table=t, rows=n,
                        backend_refs=hits.get(t) or [],
                        successor=u"无（如需保留语义请先指定承接表）"))
    return out


# ------------------------------------------------------------------ 汇总

def summary(cur):
    s = {}
    for t in ("Norm_Labor_Table", "Norm_Equipment_Table", "L4_Norm_Default",
              "L4_Activity_Dictionary"):
        cols = [r[1] for r in cur.execute("PRAGMA table_info([%s])" % t)]
        if "measure_scope" not in cols:
            continue
        total = ones(cur, "SELECT COUNT(*) FROM [%s]" % t)
        filled = ones(cur, "SELECT COUNT(*) FROM [%s] WHERE IFNULL(measure_scope,'')<>''" % t)
        dist = dict(q(cur, "SELECT IFNULL(measure_scope,'(空)'), COUNT(*) FROM [%s] "
                           "GROUP BY 1 ORDER BY 2 DESC" % t))
        s[t] = dict(total=total, filled=filled, dist=dist,
                    pct=round(100.0 * filled / total, 1) if total else 0.0)
    s["_norm_kind"] = dict(q(cur, "SELECT norm_kind, COUNT(*) FROM L4_Norm_Default GROUP BY 1"))
    s["_main_machine"] = dict(q(cur, "SELECT source_type, COUNT(*) FROM Activity_Main_Machine "
                                    "GROUP BY 1"))
    s["_equipment_driven"] = ones(
        cur, "SELECT COUNT(*) FROM L4_Activity_Dictionary "
             "WHERE recommended_production_mode='equipment_driven'")
    s["_eq_driven_with_rows"] = ones(cur, """
        SELECT COUNT(*) FROM L4_Activity_Dictionary d WHERE
        d.recommended_production_mode='equipment_driven'
        AND EXISTS (SELECT 1 FROM Norm_Equipment_Table e WHERE e.activity_id=d.activity_id)""")
    s["_scaffold_norms"] = ones(
        cur, "SELECT COUNT(*) FROM Norm_Labor_Table WHERE source_code='SCAFFOLD_V1'")
    s["_scaffold_equipment"] = ones(
        cur, "SELECT COUNT(*) FROM Norm_Equipment_Table WHERE source_code='SCAFFOLD_V1'")
    s["_new_activities"] = q(
        cur, "SELECT activity_id, activity_name, unit, measure_scope FROM "
             "L4_Activity_Dictionary WHERE activity_id LIKE 'MON_%' OR "
             "activity_id LIKE 'EMB_%' OR activity_id LIKE 'PAVE_%' ORDER BY activity_id")
    s["_legacy_marked"] = {}
    for t in ("Workface_Capacity_Rule_legacy_v1", "Workface_Capacity_Rule_legacy_v2"):
        total = ones(cur, "SELECT COUNT(*) FROM [%s]" % t)
        marked = ones(cur, "SELECT COUNT(*) FROM [%s] WHERE IFNULL(notes,'') LIKE '%%deprecated%%'"
                      % t)
        s["_legacy_marked"][t] = (marked, total)
    # H1（2026-09-21）：`Unit_Conversion` 表已删除，不再统计
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    cur = con.cursor()
    s = summary(cur)
    b1 = b1_rows(cur)
    c12 = c12_rows(cur, ROOT)

    L = []
    add = L.append

    add(u"## 汇总：measure_scope 填充")
    add(u"")
    add(u"| 表 | 总行 | 已填 | 填充率 | 分布 |")
    add(u"|---|---|---|---|---|")
    for t in ("L4_Activity_Dictionary", "Norm_Labor_Table", "Norm_Equipment_Table",
              "L4_Norm_Default"):
        d = s[t]
        add(u"| `%s` | %d | %d | %s%% | %s |" % (
            t, d["total"], d["filled"], d["pct"],
            ", ".join(u"%s:%d" % (k, v) for k, v in d["dist"].items())))
    add(u"")
    add(u"- `L4_Norm_Default.norm_kind`：%s" % json.dumps(s["_norm_kind"], ensure_ascii=False))
    add(u"- `Activity_Main_Machine.source_type`：%s" % json.dumps(s["_main_machine"], ensure_ascii=False))
    add(u"- equipment_driven %d 个 / 其中有台班定额行 %d 个"
        % (s["_equipment_driven"], s["_eq_driven_with_rows"]))
    add(u"- SCAFFOLD_V1 占位：Norm_Labor_Table %d 行 / Norm_Equipment_Table %d 行"
        % (s["_scaffold_norms"], s["_scaffold_equipment"]))
    for t, (m, tot) in s["_legacy_marked"].items():
        add(u"- `%s` 已标废弃 %d/%d" % (t, m, tot))
    add(u"")
    add(u"## 新增活动（C1/C2/C4）")
    add(u"")
    add(u"| activity_id | 名称 | 单位 | measure_scope |")
    add(u"|---|---|---|---|")
    for aid, nm, unit, scope in s["_new_activities"]:
        add(u"| `%s` | %s | %s | %s |" % (aid, nm, unit, scope or u"''"))
    add(u"")
    add(u"## B1：90 行 AI_ESTIMATE_V1 逐行审核（只读，未改任何数值）")
    add(u"")
    add(u"判据（客观、可复算）：① 同 activity 同 quantity_unit 已存在 `regional_quota` 规范行 → "
        u"需改值=**是**（有规范可替）；② 否则若 work_type 属措施/验收类（tech_prep/site_prep/"
        u"material_prep/equip_prep/temp_util/hidden_accept/sub_accept/div_accept/final_accept/"
        u"demolition/demobilization）→ 需改值=**否**（规范劳动定额本就不覆盖，AI 估算属合理兜底，"
        u"但必须保持 source_code=AI_ESTIMATE_V1 以便下游按「模型估算」标注）；③ 其余实体工程 → "
        u"需改值=**是**（应有规范来源）。")
    add(u"")
    add(u"| norm_id | activity_id | 现值 | 单位 | 推断口径 | 是否需改值 | 理由 |")
    add(u"|---|---|---|---|---|---|---|")
    for r in b1:
        add(u"| %s | `%s` | %s | %s | %s | %s | %s |" % (
            r["norm_id"], r["activity_id"], r["value"], r["unit"],
            r["scope"] or u"''", r["verdict"], r["why"]))
    yes = sum(1 for r in b1 if r["verdict"] == u"是")
    add(u"")
    add(u"**B1 结论：共 %d 行；需改值 %d 行，维持 %d 行。所有数值均未改动。**"
        % (len(b1), yes, len(b1) - yes))
    add(u"")
    add(u"## C12：判废候选清单（未删除任何表）")
    add(u"")
    add(u"| 表 | 行数 | 非测试后端代码引用 | 建议承接表 |")
    add(u"|---|---|---|---|")
    for r in c12:
        refs = u"、".join(u"`%s`" % x for x in r["backend_refs"]) if r["backend_refs"] else u"**无**"
        add(u"| `%s` | %s | %s | %s |" % (r["table"], r["rows"], refs, r["successor"]))
    add(u"")
    add(u"说明：空白引用即 `backend/`（排除 `__pycache__`/`_probe_tmp`/`tests`）下无任何 .py 引用；"
        u"这些表只被 `BuildPlan_KB/tools/query_*.py`、README、devtools 迁移脚本提及。"
        u"**本流未删除任何表**，等父代理确认承接关系后再清退。")

    text = u"\n".join(L)
    print(text)
    if a.out:
        with io.open(a.out, "w", encoding="utf-8") as f:
            f.write(text + u"\n")
        print(u"\n[written] %s" % a.out)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
