# -*- coding: utf-8 -*-
"""第 37 轮 · WS2：KB 数据层迁移 v2（幂等；默认只做 dry-run，加 --apply 才写库）。

## 这个脚本修什么

`devtools/fix_norm_basis.py` 当年把 `Norm_Labor_Table.productivity_value` 按
`quantity_basis / labor_norm_value` 重写过。那条公式是**错的**，因为
`labor_norm_value` 落库时**已经归一**（`raw_value / quantity_basis == labor_norm_value`，
实测 3877/3877 行成立）。正确产能只有一个真值：

    productivity_value = 1 / labor_norm_value        # 工日 → 单位/工日

于是 `quantity_basis != 1` 的 1030 行全部错：895 行变成 `basis/norm`（偏大 basis 倍，
最大 1000 倍）、135 行变成 `1/(basis*norm)`（偏小 basis 倍）。这是"铝模 1 人/层"与
"4260 m³ 基坑土方 118 天"的成因之一。

本脚本做的六件事（每一步都写 `data_quality_log`）：

1. 备份 `BuildPlan_KB/kb.db` → `kb.db.bak_<YYYYmmdd_HHMMSS>`（`shutil.copy2`）。
2. `Norm_Labor_Table.quantity_basis` → `raw_quantity_basis`（已是新名则跳过；SQLite
   `RENAME COLUMN` 报错时退回"建新表 + 拷数据 + 换名"）。
3. 产能回归唯一真值 `productivity_value = 1/labor_norm_value`（Python 浮点，不用 SQL），
   `productivity_unit` 归一成 `<规范 quantity_unit>/工日`，`conversion_notes` **追加**
   而不覆盖（重复运行靠标记串幂等）。
4. `Norm_Equipment_Table.quantity_unit` 用 `kb_units.normalize_unit` 归一；
   `machine_shift_unit_json` 里的转义写法 `\\u53f0\\u73ed` 统一成字面 `"台班"`
   （数组长度与 `machine_shift_norm_json` 保持一致，数值一律不动）。
5. 建并灌 `Workface_Capacity_Rule_v2`（478 行）与 `Production_Method_Baseline`
   （earthwork / pile_foundation / concrete / masonry）。
6. 结束打印：备份文件名、各步改动行数、新建行数。

⚠️ 第 5 步在**已合表**的库上是空转/重建中间态：工作面容量现在的唯一运行时表是
`Workface_Capacity_Rule`（v3 结构，由 `devtools/migrate_v3_workface.py` 从
`Workface_Capacity_Rule_legacy_v1` + `_legacy_v2` 合成），本脚本不再建 v2 表。
本脚本保留是为了它**其它四步**（列改名 / 产能回归 / 机械定额单位 / PMB）仍然有用；
全新克隆的库要跑齐两步：先本脚本、再 `migrate_v3_workface.py`。

用法：
    python devtools/migrate_v2_kb.py            # dry-run：只打印将改什么
    python devtools/migrate_v2_kb.py --apply    # 真正写库（先自动备份）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "BuildPlan_KB", "kb.db")

sys.path.insert(0, os.path.join(ROOT, "backend"))
from pipeline import kb_units as U  # noqa: E402

# 追加到 conversion_notes 的标记；同时也是幂等判据（第 2 次运行不再追加）
FIX_TAG = "[第37轮修正：norm_value 已归一，产能 = 1/labor_norm_value；此前按 basis/norm 写库，偏大 basis 倍]"

MODEL_VERSION = "wfcap-v2-2026.11"

# 契约 §4 的经验带：精确 quantity_unit → (q_ref, crew_step_q, crew_step_n)
# 单位不在表内 → q_ref 取旧表该 unit 组 max_labor 的中位数，step_q=1、step_n=0。
Q_REF_BANDS = {
    "m³": (200.0, 50.0, 1),
    "t": (22.0, 10.0, 1),
    "m²": (1000.0, 500.0, 1),
    "m": (200.0, 50.0, 1),
    "根": (30.0, 20.0, 1),
}

# 契约 §4 的族标定说明（写进 notes，便于人工复核）
FAMILY_NOTE = {
    "volume": "m³", "mass": "t", "area": "m²", "length": "m", "count:根": "根",
}

# Production_Method_Baseline：土方量级基线来自契约 §5-WS3②；
# 其余 L3 由 L4_Activity_Dictionary.recommended_production_mode 的多数派 + 机械活动反查得出。
BASELINE_ROWS = [
    {
        "work_type_l3": "earthwork",
        "default_mode": "machine",
        "qty_threshold_high": 500.0,
        "qty_threshold_low": 50.0,
        "machine_activity_hint": "GD_A11_机械挖土方、淤泥流砂",
        "notes": ("量级基线来自改造契约 §5-WS3②（>500 m³ 强制机械、<50 m³ 人工）；"
                  "machine_activity_hint 取自 L4_Activity_Dictionary 中 work_type_id='earthwork' "
                  "且 recommended_production_mode='equipment_driven' 的「机械挖土方、淤泥流砂」"
                  "（实测该 activity_id 存在，机械/机械 口径）"),
        "source_type": "contract_rule",
        "confidence": "MEDIUM",
    },
    {
        "work_type_l3": "pile_foundation",
        "default_mode": "machine",
        "qty_threshold_high": 300.0,
        "qty_threshold_low": 30.0,
        "machine_activity_hint": "GD_A13_打管桩",
        "notes": ("来源：L4_Activity_Dictionary 中 work_type_id='pile_foundation' 的 33 条活动"
                  "**全部**为 equipment_driven（实测），故 default_mode='machine'；"
                  "阈值 300/30 为本轮 AI 估算（经验带，未标定），confidence=LOW"),
        "source_type": "ai_estimate",
        "confidence": "LOW",
    },
    {
        "work_type_l3": "concrete",
        "default_mode": "machine",
        "qty_threshold_high": 1000.0,
        "qty_threshold_low": 100.0,
        "machine_activity_hint": "CONC_NEW_FOUND",
        "notes": ("来源：L4_Activity_Dictionary 中 work_type_id='concrete' 的 13 条活动中"
                  "11 条 equipment_driven、2 条 labor_driven，多数派为机械；"
                  "阈值 1000/100 为本轮 AI 估算（经验带，未标定），confidence=LOW"),
        "source_type": "ai_estimate",
        "confidence": "LOW",
    },
    {
        "work_type_l3": "masonry",
        "default_mode": "labor",
        "qty_threshold_high": None,
        "qty_threshold_low": None,
        "machine_activity_hint": None,
        "notes": ("来源：L4_Activity_Dictionary 中 work_type_id='masonry' 的 55 条活动"
                  "**全部**为 labor_driven（实测），无 equipment_driven 活动可改绑，"
                  "故 default_mode='labor' 且 machine_activity_hint 留空、阈值不设"),
        "source_type": "kb_dict_majority",
        "confidence": "MEDIUM",
    },
]

DDL_V2 = """
CREATE TABLE IF NOT EXISTS Workface_Capacity_Rule_v2 (
  rule_id TEXT PRIMARY KEY, activity_id TEXT, work_type_l3 TEXT,
  quantity_unit TEXT,
  q_ref REAL,
  crew_base INTEGER,
  crew_step_q REAL, crew_step_n INTEGER,
  crew_min INTEGER, crew_max INTEGER,
  segments_factor INTEGER DEFAULT 1,
  machine_q_ref REAL, machine_base INTEGER, machine_step_q REAL,
  machine_step_n INTEGER, machine_min INTEGER, machine_max INTEGER,
  model_version TEXT, q_ref_source TEXT, source_type TEXT, confidence TEXT,
  notes TEXT, created_at TEXT)
"""

DDL_PMB = """
CREATE TABLE IF NOT EXISTS Production_Method_Baseline (
  work_type_l3 TEXT PRIMARY KEY, default_mode TEXT,
  qty_threshold_high REAL, qty_threshold_low REAL,
  machine_activity_hint TEXT, notes TEXT, source_type TEXT, confidence TEXT)
"""


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def table_columns(cur, table):
    """表的所有列名；表不存在返回 []。"""
    try:
        return [r[1] for r in cur.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def table_exists(cur, table):
    return bool(cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def dql_columns(cur):
    return table_columns(cur, "data_quality_log")


def log_quality(cur, table_name, record_id, issue_type, severity, description, resolution):
    """写一条 data_quality_log；表/列不存在就静默跳过（返回 False）。"""
    cols = dql_columns(cur)
    if not cols:
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    want = {
        "table_name": table_name, "record_id": record_id, "issue_type": issue_type,
        "severity": severity, "description": description, "resolution": resolution,
        "resolved_by": "devtools/migrate_v2_kb.py", "resolved_at": now, "created_at": now,
    }
    use = {k: v for k, v in want.items() if k in cols}
    if "table_name" not in use:
        return False
    names = list(use.keys())
    sql = "INSERT INTO data_quality_log (%s) VALUES (%s)" % (
        ", ".join(names), ", ".join("?" for _ in names))
    cur.execute(sql, [use[n] for n in names])
    return True


def parse_json_list(text):
    if text is None:
        return None, ""
    try:
        val = json.loads(text)
    except (ValueError, TypeError):
        return None, "json 解析失败"
    if not isinstance(val, list):
        return None, "不是 JSON 数组"
    return val, ""


# ---------------------------------------------------------------------------
# 步骤 2：列改名
# ---------------------------------------------------------------------------

def rename_basis_column(cur, apply_changes, stats):
    """quantity_basis → raw_quantity_basis（幂等）。返回说明文本。"""
    cols = table_columns(cur, "Norm_Labor_Table")
    if not cols:
        stats["rename"] = "跳过：Norm_Labor_Table 不存在"
        return stats["rename"]
    if "raw_quantity_basis" in cols:
        stats["rename"] = "列 raw_quantity_basis 已存在，跳过改名（幂等）"
        return stats["rename"]
    if "quantity_basis" not in cols:
        stats["rename"] = "跳过：既没有 quantity_basis 也没有 raw_quantity_basis"
        return stats["rename"]

    if not apply_changes:
        stats["rename"] = "将执行 ALTER TABLE Norm_Labor_Table RENAME COLUMN quantity_basis TO raw_quantity_basis"
        return stats["rename"]

    try:
        cur.execute("ALTER TABLE Norm_Labor_Table "
                    "RENAME COLUMN quantity_basis TO raw_quantity_basis")
        stats["rename"] = "已改名 quantity_basis → raw_quantity_basis（ALTER ... RENAME COLUMN）"
        return stats["rename"]
    except sqlite3.Error as exc:
        stats["rename"] = "ALTER RENAME COLUMN 失败（%s），改用建新表 + 拷数据 + 替换" % exc

    # 兜底：建新表 + 拷数据 + 替换（尽量用原 DDL 保真）
    ddl = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='Norm_Labor_Table'").fetchone()
    if not ddl or not ddl[0]:
        raise RuntimeError("拿不到 Norm_Labor_Table 的 DDL，无法重建")
    new_ddl = ddl[0].replace("Norm_Labor_Table", "Norm_Labor_Table__tmp", 1)
    new_ddl = new_ddl.replace("quantity_basis", "raw_quantity_basis")
    cur.execute(new_ddl)
    old_cols = table_columns(cur, "Norm_Labor_Table")
    new_cols = table_columns(cur, "Norm_Labor_Table__tmp")
    cols_sql = ", ".join('"%s"' % c for c in new_cols)
    src_sql = ", ".join('"%s"' % ("quantity_basis" if c == "raw_quantity_basis" else c)
                        for c in new_cols)
    cur.execute("INSERT INTO Norm_Labor_Table__tmp (%s) SELECT %s FROM Norm_Labor_Table"
                % (cols_sql, src_sql))
    cur.execute("DROP TABLE Norm_Labor_Table")
    cur.execute("ALTER TABLE Norm_Labor_Table__tmp RENAME TO Norm_Labor_Table")
    stats["rename"] += "（旧列 %d 列 → 新列 %d 列）" % (len(old_cols), len(new_cols))
    return stats["rename"]


# ---------------------------------------------------------------------------
# 步骤 2b：只读兼容别名列 quantity_basis
# ---------------------------------------------------------------------------

def add_basis_alias_column(cur, apply_changes, stats):
    """补一个与 `raw_quantity_basis` 同值的 `quantity_basis` 列（**只读兼容别名**）。

    为什么补：契约把列改名成 `raw_quantity_basis`（新语义：仅溯源），但仓库里
    `backend/tests/test_norm_basis.py`（契约列为**严禁修改**）的 SQL 直接写
    `quantity_basis`。改名后那条 SQL 会 `no such column` 直接报错，测试连断言
    都跑不到。补一个同值别名列后，该测试能正常执行到断言（它断言的旧产能口径
    与第 37 轮相反，仍然会失败，但那是**口径冲突**，必须由契约层决定怎么处理，
    不该伪装成"列名不存在"）。

    别名列不参与任何计算：`kb.py` 只把 `raw_quantity_basis` 当口径源，
    `verify_kb_invariants.py` 校验两列逐行相等。

    ⚠ 下游警告（请转达 WS4）：`quantity_basis` 这个别名**只为本仓库那条
    严禁修改的测试而留**，语义仍是"仅溯源"。**禁止**在流水线里拿它当乘数
    （那正是第 37 轮修掉的 bug）；第 37 轮后的口径只有一条：
    `productivity_value = 1 / labor_norm_value`。
    """
    cols = table_columns(cur, "Norm_Labor_Table")
    if not cols:
        stats["basis_alias"] = "跳过：Norm_Labor_Table 不存在"
        return
    if "raw_quantity_basis" not in cols:
        stats["basis_alias"] = "跳过：raw_quantity_basis 列不存在（改名未执行）"
        return
    rows = cur.execute(
        "SELECT COUNT(*) FROM Norm_Labor_Table WHERE "
        "IFNULL(raw_quantity_basis, -999) IS NOT IFNULL(quantity_basis, -999)"
    ).fetchone()[0] if "quantity_basis" in cols else None
    if "quantity_basis" in cols:
        stats["basis_alias"] = ("别名列 quantity_basis 已存在；与 raw_quantity_basis "
                                "不一致 %d 行" % rows)
        stats["basis_alias_mismatch"] = rows
        if apply_changes and rows:
            cur.execute("UPDATE Norm_Labor_Table SET quantity_basis = raw_quantity_basis")
            stats["basis_alias"] += "（已同步为同值）"
            stats["basis_alias_mismatch"] = 0
        return
    stats["basis_alias"] = ("将补只读兼容别名列 quantity_basis，并填成与 "
                            "raw_quantity_basis 同值（%d 行）"
                            % cur.execute("SELECT COUNT(*) FROM Norm_Labor_Table").fetchone()[0])
    if not apply_changes:
        return
    cur.execute("ALTER TABLE Norm_Labor_Table ADD COLUMN quantity_basis REAL")
    cur.execute("UPDATE Norm_Labor_Table SET quantity_basis = raw_quantity_basis")
    stats["basis_alias"] = "已补别名列 quantity_basis（与 raw_quantity_basis 同值）"
    stats["basis_alias_mismatch"] = 0


# ---------------------------------------------------------------------------
# 步骤 3：产能修正
# ---------------------------------------------------------------------------

def fix_productivity(cur, apply_changes, stats):
    basis_col = "raw_quantity_basis" if "raw_quantity_basis" in table_columns(
        cur, "Norm_Labor_Table") else "quantity_basis"
    rows = cur.execute(
        "SELECT norm_id, activity_id, labor_norm_value, %s, quantity_unit, "
        "productivity_value, productivity_unit, conversion_notes "
        "FROM Norm_Labor_Table" % basis_col).fetchall()

    value_fixes, unit_fixes, skipped, already_tagged = [], [], [], 0
    for nid, aid, nv, _b, qu, pv, pu, cn in rows:
        correct = U.productivity_of(nv)
        if correct is None:
            skipped.append((nid, aid, nv))
            continue
        want_unit = "%s/工日" % U.normalize_unit(qu)
        need_value = pv is None or abs(float(pv) - correct) > 1e-9
        need_unit = (pu or "") != want_unit
        need_note = FIX_TAG not in (cn or "")
        if not (need_value or need_unit or need_note):
            already_tagged += 1
            continue
        if need_value:
            value_fixes.append((correct, want_unit, nid))
        if need_unit:
            unit_fixes.append((want_unit, nid))

    stats["labor_total"] = len(rows)
    stats["prod_value_fixed"] = len(value_fixes)
    stats["prod_unit_fixed"] = len(unit_fixes)
    stats["prod_skipped"] = len(skipped)
    stats["prod_already"] = already_tagged
    stats["prod_samples"] = [
        (r[0], r[1], r[2], r[3], r[4]) for r in rows[:0]]
    # 抽 6 条"改前 → 改后"样例（优先 basis≠1 的）
    samples = []
    for nid, aid, nv, b, qu, pv, pu, cn in rows:
        if b and b != 1 and pv is not None and U.productivity_of(nv) is not None:
            samples.append((nid, aid, nv, b, pv, U.productivity_of(nv)))
        if len(samples) >= 6:
            break
    stats["prod_samples"] = samples

    if not apply_changes:
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for nid, aid, nv, b, qu, pv, pu, cn in rows:
        correct = U.productivity_of(nv)
        if correct is None:
            continue
        want_unit = "%s/工日" % U.normalize_unit(qu)
        new_note = cn if FIX_TAG in (cn or "") else ((cn or "") + " " + FIX_TAG).strip()
        cur.execute(
            "UPDATE Norm_Labor_Table SET productivity_value = ?, productivity_unit = ?, "
            "conversion_notes = ?, updated_at = ? WHERE norm_id = ?",
            (correct, want_unit, new_note, now, nid))


# ---------------------------------------------------------------------------
# 步骤 4：机械定额单位
# ---------------------------------------------------------------------------

def fix_equipment_units(cur, apply_changes, stats):
    rows = cur.execute(
        "SELECT norm_id, activity_id, quantity_unit, machine_shift_norm_json, "
        "machine_shift_unit_json FROM Norm_Equipment_Table").fetchall()
    unit_fixes, json_fixes, skipped, mismatch, repaired = [], [], [], [], []
    new_rows = []
    for nid, aid, qu, nj, uj in rows:
        nqu = U.normalize_unit(qu)
        if nqu != (qu or ""):
            unit_fixes.append((nid, qu, nqu))
        norms, err_n = parse_json_list(nj)
        units, err_u = parse_json_list(uj)
        if err_n or err_u:
            skipped.append((nid, err_n or err_u))
            continue
        if len(norms) != len(units):
            mismatch.append((nid, len(norms), len(units)))
        # 数值一律不动；只把单位写法统一成字面「台班」的规范 JSON
        fixed_units = [U.normalize_unit(x) for x in units]
        canon = json.dumps(fixed_units, ensure_ascii=False)
        if canon != (uj or ""):
            json_fixes.append((nid, uj, canon))
            new_rows.append((canon, nid))
        else:
            repaired.append(nid)

    stats["equip_total"] = len(rows)
    stats["equip_unit_fixed"] = len(unit_fixes)
    stats["equip_json_fixed"] = len(json_fixes)
    stats["equip_json_already"] = len(repaired)
    stats["equip_json_skipped"] = len(skipped)
    stats["equip_len_mismatch"] = len(mismatch)
    stats["equip_unit_samples"] = unit_fixes[:6]
    stats["equip_json_samples"] = [(n, a, b) for n, a, b in json_fixes[:4]]

    if not apply_changes:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for nid, old, new in unit_fixes:
        cur.execute("UPDATE Norm_Equipment_Table SET quantity_unit = ?, updated_at = ? "
                    "WHERE norm_id = ?", (new, now, nid))
    for new_json, nid in new_rows:
        cur.execute("UPDATE Norm_Equipment_Table SET machine_shift_unit_json = ?, "
                    "updated_at = ? WHERE norm_id = ?", (new_json, now, nid))


# ---------------------------------------------------------------------------
# 步骤 4b：L4 字典单位写法
# ---------------------------------------------------------------------------

def fix_l4_units(cur, apply_changes, stats):
    """L4_Activity_Dictionary.unit 归一（'m3'/'m2' → 'm³'/'m²'）。

    为什么归这一列：`workface_capacity` v2 的 quantity_unit 就是 **L4.unit**，
    而人工定额的 quantity_unit 已是规范写法（3877/3877）。两列写法不一致时，
    "人工/机械判定 + 单位换算"那套 check_unit_pair 虽能靠 normalize 兜住，
    但 KB 里同一物理量出现两种写法时，任何直接 `==` 的比较（含产物 JSON）
    都会静默失配；数据层应只有一种写法。数值/语义一律不动。
    """
    cols = table_columns(cur, "L4_Activity_Dictionary")
    if not cols:
        stats["l4_unit_total"] = 0
        stats["l4_unit_fixed"] = 0
        stats["l4_unit_samples"] = []
        stats["l4_unit_note"] = "跳过：L4_Activity_Dictionary 不存在"
        return
    rows = cur.execute(
        "SELECT activity_id, unit FROM L4_Activity_Dictionary").fetchall()
    fixes = []
    for aid, unit in rows:
        nunit = U.normalize_unit(unit)
        if nunit != (unit or ""):
            fixes.append((aid, unit, nunit))
    stats["l4_unit_total"] = len(rows)
    stats["l4_unit_fixed"] = len(fixes)
    stats["l4_unit_samples"] = fixes[:6]
    stats["l4_unit_note"] = "L4.unit 已全规范" if not fixes else \
        "L4.unit 需归一 %d 行（写法 'm3'/'m2'）" % len(fixes)
    if not apply_changes or not fixes:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for aid, _old, new in fixes:
        cur.execute("UPDATE L4_Activity_Dictionary SET unit = ?, updated_at = ? "
                    "WHERE activity_id = ?", (new, now, aid))


# ---------------------------------------------------------------------------
# 步骤 5：新表
# ---------------------------------------------------------------------------

L3_UNIT_RE = None


def build_v2_rows(cur, stats):
    """构造 Workface_Capacity_Rule_v2 的 478 行。"""
    l4 = {}
    for aid, l3, unit in cur.execute(
            "SELECT activity_id, work_type_id, unit FROM L4_Activity_Dictionary"):
        l4[aid] = (l3, U.normalize_unit(unit))

    old = cur.execute(
        "SELECT rule_id, activity_id, max_labor, max_machine FROM Workface_Capacity_Rule "
        "ORDER BY activity_id").fetchall()
    stats["old_wfcap_rows"] = len(old)
    # 旧表只有 70/478 行给了 max_machine（取值 1/2/3）——机械侧的唯一合法来源
    stats["legacy_machine_rows"] = sum(
        1 for _r, _a, _ml, mm in old if mm is not None and mm > 0)

    # ① quantity_unit：L4.unit 归一；取不到 → 同 L3 众数继承
    l3_units = {}
    for aid, (l3, unit) in l4.items():
        if unit:
            l3_units.setdefault(l3, {})
            l3_units[l3][unit] = l3_units[l3].get(unit, 0) + 1
    l3_mode = {l3: max(c.items(), key=lambda kv: (kv[1], kv[0]))[0]
               for l3, c in l3_units.items()}

    resolved = {}
    unit_from_l4 = 0
    unit_from_l3 = 0
    unit_missing = 0
    for _rid, aid, _ml, _mm in old:
        l3, unit = l4.get(aid, (None, ""))
        if unit:
            resolved[aid] = (l3, unit, "l4")
            unit_from_l4 += 1
        elif l3 in l3_mode:
            resolved[aid] = (l3, l3_mode[l3], "l3_mode")
            unit_from_l3 += 1
        else:
            resolved[aid] = (l3, "", "missing")
            unit_missing += 1
    stats["v2_unit_from_l4"] = unit_from_l4
    stats["v2_unit_from_l3_mode"] = unit_from_l3
    stats["v2_unit_missing"] = unit_missing

    # ② 旧表按"解析出的 quantity_unit"分组统计（契约 §4：max_labor 同族中位/极值）
    groups = {}
    for _rid, aid, ml, _mm in old:
        u = resolved[aid][1] or ""
        groups.setdefault(u, []).append(ml if ml is not None else 0)

    def med(vals):
        s = sorted(vals)
        n = len(s)
        if not n:
            return 0
        return s[n // 2] if n % 2 else int(round((s[n // 2 - 1] + s[n // 2]) / 2.0))

    fam = {u: {"n": len(v), "median": med(v), "min": min(v), "max": max(v)}
           for u, v in groups.items()}

    rows_out = []
    for idx, (rid, aid, _ml, _mm) in enumerate(old, 1):
        l3, unit, src = resolved[aid]
        st = fam.get(unit, {"n": 0, "median": 0, "min": 0, "max": 0})
        crew_base = int(st["median"])
        crew_max = int(st["max"])
        crew_min = max(2, int(st["min"]))
        band = Q_REF_BANDS.get(unit)
        if band is None:
            q_ref = float(crew_base) if crew_base else 1.0
            step_q, step_n = 1.0, 0
            q_src = ("落入「其它族」分支：q_ref 取旧表该单位组 max_labor 的中位 %s、"
                     "step_q=1、step_n=0（契约 §4 经验带未覆盖该单位）" % crew_base)
        else:
            q_ref, step_q, step_n = band
            q_src = ("q_ref=%g、每 %g %s ±1 人（契约 §4 经验带）"
                     % (q_ref, step_q, unit))
        notes = ("crew_base 来自旧表同族（quantity_unit=%s，%d 行）max_labor 中位；"
                 "crew_max=同族最大 %d、crew_min=max(2, 同族最小 %d)；%s"
                 % (unit, st["n"], crew_max, st["min"], q_src))
        if band is None:
            # step_n=0 是**有意为之**，不是缺数据：这类单位（项/樘/块/座/台/扇/卷/…，
            # 共 95 行）的"一项验收""一樘门"容量与工程量无关，容量退化为常量 crew_base。
            notes += ("；注：unit 不在契约 §4 经验带内，crew_step_n=0 为有意为之"
                      "（容量与工程量无关，恒为 crew_base=%d）" % crew_base)
        if src == "l3_mode":
            notes += "；unit_from=l3_mode（L4.unit 缺，继承同 L3 众数）"
        if src == "missing":
            notes += "；unit_from=missing（L4.unit 与同 L3 众数都取不到）"

        # ---- 机械侧六列：只认旧表 Workface_Capacity_Rule.max_machine（70/478 行有值），
        # 其余 408 行**保留 NULL 并在 notes 写明"无机械容量数据"，绝不编造机械台数** ----
        legacy_mm = int(_mm) if _mm is not None and int(_mm) > 0 else None
        if legacy_mm is None:
            m_q_ref = m_base = m_step_q = m_step_n = m_min = m_max = None
            notes += ("；机械侧：旧表 max_machine 为空（全表 478 行仅 %d 行有值，覆盖率 "
                      "%d/%d），保留 NULL = 无机械容量数据，不编造台数"
                      % (stats["legacy_machine_rows"], stats["legacy_machine_rows"],
                         stats["old_wfcap_rows"]))
        else:
            m_max = legacy_mm
            m_base, m_min = 1, 1
            if unit in Q_REF_BANDS:
                _q, m_step_q, m_step_n = Q_REF_BANDS[unit]
                m_q_ref = _q
                m_step_desc = ("每 %g %s ±1 台（沿用契约 §4 同族经验带），夹在 [%d, %d]"
                               % (m_step_q, unit, m_min, m_max))
            else:
                m_q_ref, m_step_q, m_step_n = float(m_base), 1.0, 0
                m_step_desc = ("unit 不在经验带内，machine_step_n=0 为有意为之"
                               "（机械台数恒为 machine_base=%d）" % m_base)
            notes += ("；机械侧：machine_max=%d 来自旧表 max_machine（旧表 %d/%d 行有值）；"
                      "machine_base=1、machine_min=1、machine_q_ref=%g；%s"
                      % (m_max, stats["legacy_machine_rows"], stats["old_wfcap_rows"],
                         m_q_ref, m_step_desc))
        rule_id = "WFC2_%04d" % idx
        rows_out.append((
            rule_id, aid, l3, unit, q_ref, crew_base, step_q, step_n,
            crew_min, crew_max, 1, m_q_ref, m_base, m_step_q, m_step_n,
            m_min, m_max,
            MODEL_VERSION, "契约 §4 经验带 + 旧表同族中位（脚本 ai 标定）",
            "ai_estimate", "LOW", notes, None))
    stats["v2_rows"] = len(rows_out)
    stats["v2_machine_rows"] = sum(1 for r in rows_out if r[16] is not None)
    stats["v2_bands"] = {u: v for u, v in fam.items()}
    stats["v2_of_unit"] = {u: sum(1 for r in rows_out if r[3] == u) for u in fam}
    return rows_out


def apply_new_tables(cur, v2_rows, apply_changes, stats):
    if not apply_changes:
        stats["new_tables"] = "将重建 Workface_Capacity_Rule_v2（%d 行）与 Production_Method_Baseline（%d 行）" % (
            len(v2_rows), len(BASELINE_ROWS))
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # v2 表完全由本脚本生成 → 重建以保证幂等（旧表 Workface_Capacity_Rule 只读保留）
    cur.execute("DROP TABLE IF EXISTS Workface_Capacity_Rule_v2")
    cur.execute(DDL_V2)
    cur.executemany(
        "INSERT INTO Workface_Capacity_Rule_v2 (rule_id, activity_id, work_type_l3, "
        "quantity_unit, q_ref, crew_base, crew_step_q, crew_step_n, crew_min, crew_max, "
        "segments_factor, machine_q_ref, machine_base, machine_step_q, machine_step_n, "
        "machine_min, machine_max, model_version, q_ref_source, source_type, confidence, "
        "notes, created_at) VALUES (%s)" % ", ".join("?" * 23),
        [tuple(list(r[:-1]) + [now]) for r in v2_rows])

    cur.execute("DROP TABLE IF EXISTS Production_Method_Baseline")
    cur.execute(DDL_PMB)
    cur.executemany(
        "INSERT INTO Production_Method_Baseline (work_type_l3, default_mode, "
        "qty_threshold_high, qty_threshold_low, machine_activity_hint, notes, source_type, "
        "confidence) VALUES (?,?,?,?,?,?,?,?)",
        [(b["work_type_l3"], b["default_mode"], b["qty_threshold_high"],
          b["qty_threshold_low"], b["machine_activity_hint"], b["notes"],
          b["source_type"], b["confidence"]) for b in BASELINE_ROWS])
    stats["new_tables"] = "已重建 v2 表 %d 行、Production_Method_Baseline %d 行" % (
        len(v2_rows), len(BASELINE_ROWS))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="第 37 轮 KB 数据层迁移 v2（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正写库（先自动备份）")
    ap.add_argument("--dry-run", action="store_true",
                    help="显式声明只预览（默认行为，仅为可读性保留）")
    ap.add_argument("--db", default=DB_PATH, help="kb.db 路径（默认仓库内的 BuildPlan_KB/kb.db）")
    args = ap.parse_args(argv)

    apply_changes = bool(args.apply)
    db_path = args.db
    if not os.path.exists(db_path):
        sys.exit("找不到 KB：%s" % db_path)

    stats = {}
    print("=" * 78)
    print("第 37 轮 WS2 · KB 数据层迁移 v2   模式 = %s" % ("APPLY（写库）" if apply_changes else "DRY-RUN（不写库）"))
    print("DB = %s" % db_path)
    print("sqlite3 = %s，python = %s" % (sqlite3.sqlite_version, sys.version.split()[0]))
    print("=" * 78)

    # ---- 备份（只在 --apply 时）----
    bak_name = "-"
    if apply_changes:
        bak_name = "kb.db.bak_%s" % datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(db_path, os.path.join(os.path.dirname(db_path), bak_name))
        print("[1] 备份 -> %s" % bak_name)
    else:
        print("[1] 备份：dry-run 不备份（--apply 时执行 shutil.copy2 → kb.db.bak_<YYYYmmdd_HHMMSS>）")
    stats["backup"] = bak_name

    con = sqlite3.connect(db_path)
    con.row_factory = None
    cur = con.cursor()
    try:
        # [2] 列改名
        rename_basis_column(cur, apply_changes, stats)
        print("[2] %s" % stats["rename"])

        # 校验列名（dry-run 时改名未执行，用原列名也能读）
        cols = table_columns(cur, "Norm_Labor_Table")
        print("    列名检查：raw_quantity_basis=%s quantity_basis=%s"
              % ("raw_quantity_basis" in cols, "quantity_basis" in cols))

        # [2b] 只读兼容别名列（test_norm_basis.py 的 SQL 写死 quantity_basis）
        add_basis_alias_column(cur, apply_changes, stats)
        print("[2b] %s" % stats["basis_alias"])

        # [3] 产能
        fix_productivity(cur, apply_changes, stats)
        print("[3] Norm_Labor_Table 共 %d 行；productivity_value 需改 %d 行、"
              "productivity_unit 需改 %d 行、norm_value<=0 跳过 %d 行、已达标 %d 行"
              % (stats["labor_total"], stats["prod_value_fixed"], stats["prod_unit_fixed"],
                 stats["prod_skipped"], stats["prod_already"]))
        for s in stats["prod_samples"]:
            print("      样例 %-10s %-14s norm=%-10s basis=%-7s 原产能=%-14s → 正确=%-14s"
                  % (s[0], s[1], round(s[2], 8), s[3], round(s[4], 4), round(s[5], 4)))

        # [4] 机械定额单位
        fix_equipment_units(cur, apply_changes, stats)
        print("[4] Norm_Equipment_Table 共 %d 行；quantity_unit 需归一 %d 行、"
              "machine_shift_unit_json 需去转义 %d 行、已规范 %d 行、解析失败跳过 %d 行、"
              "单位数组长度不一致 %d 行"
              % (stats["equip_total"], stats["equip_unit_fixed"], stats["equip_json_fixed"],
                 stats["equip_json_already"], stats["equip_json_skipped"],
                 stats["equip_len_mismatch"]))
        for u in stats["equip_unit_samples"]:
            print("      单位 %-10s %r → %r" % (u[0], u[1], u[2]))
        for j in stats["equip_json_samples"]:
            print("      JSON %-10s %s → %s" % (j[0], j[1], j[2]))

        # [4b] L4 字典单位写法（v2 的 quantity_unit 来源列）
        fix_l4_units(cur, apply_changes, stats)
        print("[4b] L4_Activity_Dictionary 共 %d 行；unit 需归一 %d 行（%s）"
              % (stats["l4_unit_total"], stats["l4_unit_fixed"], stats["l4_unit_note"]))
        for s in stats["l4_unit_samples"]:
            print("      unit %-40s %r → %r" % (s[0], s[1], s[2]))

        # [5] 新表
        v2_rows = build_v2_rows(cur, stats)
        apply_new_tables(cur, v2_rows, apply_changes, stats)
        print("[5] %s" % stats["new_tables"])
        print("      旧表 Workface_Capacity_Rule = %d 行（只读保留）" % stats["old_wfcap_rows"])
        print("      unit 来源：L4.unit=%d 行、同 L3 众数继承=%d 行、取不到=%d 行"
              % (stats["v2_unit_from_l4"], stats["v2_unit_from_l3_mode"],
                 stats["v2_unit_missing"]))
        print("      机械侧：旧表 max_machine>0 共 %d/%d 行 → machine_max/machine_base/"
              "machine_min 有值；其余 %d 行保留 NULL（无机械容量数据）"
              % (stats["legacy_machine_rows"], stats["old_wfcap_rows"],
                 stats["old_wfcap_rows"] - stats["legacy_machine_rows"]))
        print("      crew_step_n=0（常量容量，有意为之）= %d 行"
              % sum(1 for r in v2_rows if r[7] == 0))
        print("      族标定（旧表 max_labor）：")
        for u, f in sorted(stats["v2_bands"].items(), key=lambda kv: -kv[1]["n"]):
            print("        %-8s n=%-4d 中位=%-4d 最小=%-3d 最大=%-3d"
                  % (u, f["n"], f["median"], f["min"], f["max"]))

        # [6] data_quality_log
        if apply_changes:
            logged = 0
            logged += log_quality(
                cur, "Norm_Labor_Table", "*", "conversion_error", "high",
                "productivity_value 曾被 devtools/fix_norm_basis.py 按 basis/norm 写入；"
                "labor_norm_value 实为已归一值（raw_value/basis），正确产能 = 1/labor_norm_value",
                "迁移脚本按 1/labor_norm_value 重算 %d 行；productivity_unit 归一 %d 行；"
                "conversion_notes 追加第 37 轮标记" % (stats["prod_value_fixed"],
                                                      stats["prod_unit_fixed"]))
            logged += log_quality(
                cur, "Norm_Labor_Table", "*", "unit_ambiguous", "low",
                "quantity_basis 曾被下游当乘数用，且 productivity_unit 残留 '10m³/工日' 这类批量前缀",
                "列改名为 raw_quantity_basis（仅溯源）；productivity_unit 统一为 "
                "『<规范 quantity_unit>/工日』")
            logged += log_quality(
                cur, "Norm_Equipment_Table", "*", "unit_ambiguous", "low",
                "quantity_unit 存在 'm3'/'m2' 写法；machine_shift_unit_json 存在转义写法 "
                "'\\u53f0\\u73ed' 与字面 '台班' 并存",
                "quantity_unit 归一 %d 行；machine_shift_unit_json 去转义 %d 行（数组长度与 "
                "machine_shift_norm_json 保持一致，数值未动）"
                % (stats["equip_unit_fixed"], stats["equip_json_fixed"]))
            logged += log_quality(
                cur, "L4_Activity_Dictionary", "*", "unit_ambiguous", "low",
                "unit 残留 'm3'/'m2' 写法（%d 行），与 Norm_Labor_Table.quantity_unit 的"
                "规范写法 'm³'/'m²' 不一致，任何直接字符串比较都会静默失配"
                % stats["l4_unit_fixed"],
                "unit 归一为 'm³'/'m²'（数值/语义未动）；v2 表的 quantity_unit 即取自此列，"
                "归一后再生成 v2 行")
            logged += log_quality(
                cur, "Workface_Capacity_Rule_v2", "*", "other", "low",
                "旧表 Workface_Capacity_Rule 全表 ai_estimate/LOW 且只有单一上限，"
                "无法表达『量 → 班组人数』曲线",
                "新建 v2 表 %d 行：crew_base 取旧表同单位组 max_labor 中位、"
                "crew_max/crew_min 取同组极值（crew_min 下限 2）、q_ref/step 取契约 §4 经验带；"
                "机械侧六列只在旧表 max_machine>0 的 %d/%d 行有值"
                "（machine_max=旧表值、machine_base=machine_min=1、step 按单位族），"
                "其余 %d 行整组 NULL 并在 notes 写明「无机械容量数据」；"
                "unit 不在经验带内的 %d 行 crew_step_n=0（容量与工程量无关的常量，有意为之）；"
                "model_version=%s"
                % (stats["v2_rows"], stats["legacy_machine_rows"], stats["old_wfcap_rows"],
                   stats["old_wfcap_rows"] - stats["legacy_machine_rows"],
                   sum(1 for r in v2_rows if r[7] == 0), MODEL_VERSION))
            logged += log_quality(
                cur, "Production_Method_Baseline", "*", "other", "low",
                "任务量级与生产方式（人工/机械）此前只能靠 LLM 猜，导致人工定额被绑到机械土方",
                "新建 %d 行基线：earthwork（契约 §5-WS3② 500/50 m³）、pile_foundation、"
                "concrete、masonry（后三者来自 L4 字典多数派，confidence 已标注）"
                % len(BASELINE_ROWS))
            con.commit()
            print("[6] data_quality_log 写入 %d 条（表存在则写，不存在则跳过）" % logged)
        else:
            print("[6] data_quality_log：将写 6 条（apply 时执行）；现有列 = %s"
                  % ",".join(dql_columns(cur)))

        if not apply_changes:
            con.rollback()
            print()
            print("[dry-run] 未写库。加 --apply 执行上述改动（会先备份）。")
        else:
            con.commit()
            print()
            print("=" * 78)
            print("完成。备份文件名 = %s" % bak_name)
            print("改动统计：productivity_value %d 行、productivity_unit %d 行、"
                  "设备 quantity_unit %d 行、machine_shift_unit_json %d 行、"
                  "L4.unit %d 行、v2 表新建 %d 行、Production_Method_Baseline 新建 %d 行"
                  % (stats["prod_value_fixed"], stats["prod_unit_fixed"],
                     stats["equip_unit_fixed"], stats["equip_json_fixed"],
                     stats["l4_unit_fixed"],
                     stats["v2_rows"], len(BASELINE_ROWS)))
            print("=" * 78)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
