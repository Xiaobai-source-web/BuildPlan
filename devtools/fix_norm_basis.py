# -*- coding: utf-8 -*-
"""【已改正】修正 Norm_Labor_Table.productivity_value = 1 / labor_norm_value。

═══════════════════════════════════════════════════════════════════════════════
本脚本的历史（必须读，否则会重犯同一个错）
───────────────────────────────────────────────────────────────────────────────
本脚本**当年就是那个 bug 的引入者**。它当时的路子是：

    productivity_value = quantity_basis / labor_norm_value      ← 错的

理由是它以为定额单位是「工日 / (quantity_basis × 单位)」，于是要把 basis 乘回去。
事实相反：**`labor_norm_value` 在入库时就已经归一过了**，它就是
「工日 / 1 个 quantity_unit」。可复核的留档不变式（3877/3877 行成立）：

    labor_norm_value == raw_value / raw_quantity_basis

书页上的「工日 / 10m²」被拆成两张皮：批量基数留在 `quantity_basis` 列，
归一后的定额留在 `labor_norm_value`。所以正确产能是**取倒数**：

    productivity_value = 1 / labor_norm_value        ← 本脚本现在写的值

当年那次错误修正让所有 `basis != 1` 的行**偏大 basis 倍**（basis ∈ {1,10,100,1000}），
即产能被放大 10~1000 倍、工日需求相应缩小同样倍数，并把
`quantity_basis` 这个纯溯源字段变成了下游的乘数。
第 37 轮由 `devtools/migrate_v2_kb.py` 全量重算（1085 行），
`devtools/verify_kb_invariants.py` 把这条不变式固化成门禁。

═══════════════════════════════════════════════════════════════════════════════
本脚本现在的口径（与迁移脚本同源，可重复执行）
───────────────────────────────────────────────────────────────────────────────
改三列，且都幂等：
  1. `productivity_value` = 1 / labor_norm_value（norm_value 为 NULL/<=0 → 跳过不动）
  2. `productivity_unit`  = "<归一 quantity_unit>/工日"（消除 '10m³/工日' 等批量前缀）
  3. `conversion_notes`   追加本轮标记（已有标记则不重复追加）

`raw_quantity_basis`（旧列名 `quantity_basis`，本脚本两种列名都能读）**一律不再参与计算**，
只作溯源；`raw_value / raw_quantity_basis == labor_norm_value` 是留档不变式。

用法：
    python devtools/fix_norm_basis.py                 # 只看会改什么（默认 dry-run）
    python devtools/fix_norm_basis.py --dry-run       # 同上，显式声明
    python devtools/fix_norm_basis.py --apply         # 真正写库（先 shutil.copy2 备份）
    python devtools/fix_norm_basis.py --db <path>     # 指定别的 kb.db
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from pipeline import kb_units as U  # noqa: E402

DB_DEFAULT = os.path.join(ROOT, "BuildPlan_KB", "kb.db")

# 幂等判据：notes 里已有该标记就不再追加。
# 与 devtools/migrate_v2_kb.py 的 FIX_TAG **逐字一致**，否则两个脚本会互相
# 认为"对方没打过标记"而反复重写 conversion_notes。
FIX_TAG = ("[第37轮修正：norm_value 已归一，产能 = 1/labor_norm_value；"
           "此前按 basis/norm 写库，偏大 basis 倍]")

EPS = 1e-9


def table_columns(cur, table):
    try:
        return [r[1] for r in cur.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def basis_column(cur):
    """读原始基数用的列名：第 37 轮后是 raw_quantity_basis，迁移前是 quantity_basis。"""
    cols = table_columns(cur, "Norm_Labor_Table")
    for name in ("raw_quantity_basis", "quantity_basis"):
        if name in cols:
            return name
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="修正 productivity_value = 1/labor_norm_value（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正写库（先自动备份）")
    ap.add_argument("--dry-run", action="store_true", help="只预览（默认行为）")
    ap.add_argument("--db", default=DB_DEFAULT, help="kb.db 路径")
    args = ap.parse_args(argv)

    db_path = args.db
    if not os.path.exists(db_path):
        sys.exit("找不到 KB：%s" % db_path)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    bcol = basis_column(cur)
    if bcol is None:
        sys.exit("Norm_Labor_Table 既没有 raw_quantity_basis 也没有 quantity_basis，无法溯源")
    cols = table_columns(cur, "Norm_Labor_Table")
    has_notes = "conversion_notes" in cols
    has_updated = "updated_at" in cols

    total = cur.execute("SELECT COUNT(*) FROM Norm_Labor_Table").fetchone()[0]
    rows = cur.execute(
        "SELECT norm_id, activity_id, condition_text, labor_norm_value, %s AS basis, "
        "quantity_unit, productivity_value, productivity_unit%s "
        "FROM Norm_Labor_Table" %
        (bcol, ", conversion_notes" if has_notes else ", NULL AS conversion_notes")).fetchall()

    value_fixes, unit_fixes, skipped, already, samples = [], [], [], 0, []
    for r in rows:
        nv = r["labor_norm_value"]
        correct = U.productivity_of(nv)          # 1 / norm_value（basis 只交叉校验）
        if correct is None:
            skipped.append((r["norm_id"], r["activity_id"], nv))
            continue
        want_unit = "%s/工日" % U.normalize_unit(r["quantity_unit"])
        pv = r["productivity_value"]
        try:
            need_value = pv is None or abs(float(pv) - correct) > EPS
        except (TypeError, ValueError):
            need_value = True
        need_unit = (r["productivity_unit"] or "") != want_unit
        need_note = has_notes and FIX_TAG not in (r["conversion_notes"] or "")
        if not (need_value or need_unit or need_note):
            already += 1
            continue
        if need_value:
            value_fixes.append((correct, r["norm_id"]))
        if need_unit:
            unit_fixes.append((want_unit, r["norm_id"]))
        if len(samples) < 6 and need_value:
            samples.append((r["activity_id"], (r["condition_text"] or "")[:24], nv,
                            r["basis"], pv, correct))

    print("=" * 78)
    print("fix_norm_basis（已改正口径）  模式 = %s" % ("APPLY（写库）" if args.apply else "DRY-RUN（不写库）"))
    print("DB = %s" % db_path)
    print("基数列 = %s（仅溯源，不参与产能计算）" % bcol)
    print("=" * 78)
    print("Norm_Labor_Table 共 %d 行" % total)
    print("  正确口径：productivity_value = 1 / labor_norm_value")
    print("  已达标（值+单位+标记都齐）  = %d" % already)
    print("  **需改 productivity_value** = %d" % len(value_fixes))
    print("  **需改 productivity_unit**  = %d" % len(unit_fixes))
    print("  norm_value 为 NULL/<=0 跳过 = %d" % len(skipped))
    for s in samples:
        print("  样例 %-34s norm=%-10s basis=%-7s 原值=%-14s → 正确=%-14s"
              % (s[0], round(s[2], 8) if s[2] is not None else None, s[3],
                 round(s[4], 6) if s[4] is not None else None, round(s[5], 6)))
    for s in skipped[:5]:
        print("  跳过 %s(%s) labor_norm_value=%r" % (s[0], s[1], s[2]))

    if not args.apply:
        con.rollback()
        print()
        print("[dry-run] 未写库。加 --apply 执行上述改动（会先备份）。")
        con.close()
        return 0

    fixes = sorted({nid for _v, nid in value_fixes} | {nid for _u, nid in unit_fixes})
    if not fixes:
        print()
        print("没有需要修正的行（幂等）。")
        con.close()
        return 0

    bak = os.path.join(os.path.dirname(db_path),
                       "kb.db.bak_%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(db_path, bak)
    print()
    print("已备份 -> %s" % os.path.basename(bak))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    upd = ["productivity_value = ?", "productivity_unit = ?"]
    if has_notes:
        upd.append("conversion_notes = CASE WHEN COALESCE(conversion_notes,'') LIKE ? "
                   "THEN conversion_notes ELSE (COALESCE(conversion_notes,'') || ' ' || ?) END")
    if has_updated:
        upd.append("updated_at = ?")
    sql = "UPDATE Norm_Labor_Table SET %s WHERE norm_id = ?" % ", ".join(upd)

    for r in rows:
        correct = U.productivity_of(r["labor_norm_value"])
        if correct is None:
            continue
        want_unit = "%s/工日" % U.normalize_unit(r["quantity_unit"])
        params = [correct, want_unit]
        if has_notes:
            params += ["%" + FIX_TAG + "%", FIX_TAG]
        if has_updated:
            params.append(now)
        params.append(r["norm_id"])
        cur.execute(sql, params)
    con.commit()

    # 复核：跟迁移脚本/不变量门用同一条判据
    left_value = cur.execute(
        "SELECT COUNT(*) FROM Norm_Labor_Table WHERE labor_norm_value IS NOT NULL "
        "AND labor_norm_value > 0 AND (productivity_value IS NULL "
        "OR ABS(productivity_value - 1.0/labor_norm_value) > 1e-9)").fetchone()[0]
    left_unit = 0
    for r in cur.execute("SELECT quantity_unit, productivity_unit FROM Norm_Labor_Table"):
        if (r[1] or "") != "%s/工日" % U.normalize_unit(r[0]):
            left_unit += 1
    print("已处理 %d 行；复核 productivity_value 不一致 = %d 行、"
          "productivity_unit 不一致 = %d 行" % (len(fixes), left_value, left_unit))
    print("提示：全量门禁请跑 python devtools/verify_kb_invariants.py（退出码 0 = 全绿）")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
