# -*- coding: utf-8 -*-
"""第 37 轮 · WS2：KB 不变量门（失败退出码非 0）。

逐条检查（违规明细最多打印 10 条，最后给出每条的违规行数）：

1. 所有 `labor_norm_value > 0` 的行：`abs(productivity_value - 1/labor_norm_value) < 1e-9`；
2. `raw_value` 可解析为数字的行：`abs(float(raw_value)/raw_quantity_basis - labor_norm_value) < 1e-9`
   （`raw_value` 是 TEXT，可能写成 `0.175` 或带文字；解析不出的行单独统计"跳过 N 行"）；
3. `labor_norm_unit` 的 `kb_units.denominator_of(...)` == 归一后的 `quantity_unit`；
4. `kb_units.parse_norm_unit(labor_norm_unit)["scale"] == 1.0`（归一列里不允许 10/100/1000）；
5. 所有 `quantity_unit` / `Norm_Equipment_Table.quantity_unit` 已是规范写法
   （`normalize_unit(u) == u`）；顺带校验 `machine_shift_unit_json` 元素 == "台班"
   且长度与 `machine_shift_norm_json` 一致；
6. 工作面容量只有**一张** `Workface_Capacity_Rule`（合表后的 v3 结构，478 行）：
   列齐（含兼容列 `legacy_max_labor`/`legacy_max_machine`）、
   `crew_min <= crew_base <= crew_max`、`crew_step_q > 0`（`crew_step_n == 0` 的行允许
   `crew_step_q == 0`）、`crew_preferred >= 0`、`legacy_max_labor > 0`；
   旧两表已改名为只读归档 `Workface_Capacity_Rule_legacy_v1` / `_legacy_v2`；
   **H2/H3（2026-09-21）已把这两张归档表连同 H1/H4/H5/H6 的 4 张废表一并删除**，
   本文件相应改为断言它们"不存在"；
6b. 机械侧六列 `machine_q_ref/machine_base/machine_step_q/machine_step_n/machine_min/
   machine_max` **要么整组有值、要么整组 NULL**（NULL = 无机械容量数据，不许编造台数），
   有值时满足 `machine_max >= machine_base >= machine_min >= 1`。`machine_step_n == 0`
   表示"常量台数"，是合法值，**不判违规**；
6c. 机械侧覆盖率（**定格 70 行**，原归档 v1 表 `max_machine > 0` 的实测值）与
   `crew_step_n == 0` 的常量行，都必须在 `notes` 里可追溯（供最终报告引用）；
6d. **H2 后**：归档 v1 表已删除（断言"不存在"），保留 `legacy_max_labor != crew_max`
   的行必须存在（两个口径不许互相顶替；实测 387/487）。

用法：
    python devtools/verify_kb_invariants.py          # 退出码 0 = 全绿
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from pipeline import kb_units as U  # noqa: E402

DB_PATH = os.path.join(ROOT, "BuildPlan_KB", "kb.db")
# 可选：第一个位置参数覆盖库路径（用于对迁移副本 `kb.db.migrated` 预检，
# 默认仍查真库 —— 不改变原行为）。
if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
    DB_PATH = sys.argv[1]
EPS = 1e-9
MAX_DETAIL = 10

#: H2/H3（2026-09-21）删掉归档表后，原来的 `_DEP_BANNER_RE` / `_strip_dep_banner`
#: 已无比对对象（没有"带废弃横幅的归档原文"了），随归档件一并移除。


class Report:
    """逐条收集违规，最后统一打印。"""

    def __init__(self):
        self.checks = []

    def check(self, name, violations, extra=""):
        self.checks.append((name, list(violations), extra))

    def ok(self):
        return all(not v for _n, v, _e in self.checks)

    def dump(self):
        print("=" * 78)
        print("KB 不变量门 · %s" % DB_PATH)
        print("=" * 78)
        for name, violations, extra in self.checks:
            flag = "[OK]  " if not violations else "[FAIL]"
            line = "%s %s（违规 %d 行）" % (flag, name, len(violations))
            if extra:
                line += "  " + extra
            print(line)
            for v in violations[:MAX_DETAIL]:
                print("        %s" % (v,))
            if len(violations) > MAX_DETAIL:
                print("        …… 其余 %d 行同类违规（明细截断）" % (len(violations) - MAX_DETAIL))
        print("-" * 78)
        if self.ok():
            print("[OK] 全部不变量通过。")
        else:
            bad = [n for n, v, _e in self.checks if v]
            print("[FAIL] %d 条不变量未通过：%s" % (len(bad), "；".join(bad)))
        return self.ok()


def table_exists(cur, name):
    return bool(cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone())


def table_columns(cur, name):
    if not table_exists(cur, name):
        return []
    return [r[1] for r in cur.execute("PRAGMA table_info(%s)" % name)]


def to_float(text):
    """TEXT → float；解析不出来返回 None（调用方统计为"跳过"）。"""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def run(db_path=DB_PATH):
    rep = Report()
    if not os.path.exists(db_path):
        rep.check("KB 文件存在", ["找不到 %s" % db_path])
        rep.dump()
        return 1
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    try:
        cols = table_columns(cur, "Norm_Labor_Table")
        basis_col = "raw_quantity_basis" if "raw_quantity_basis" in cols else "quantity_basis"
        if basis_col == "quantity_basis":
            rep.check("列已改名 raw_quantity_basis",
                      ["Norm_Labor_Table 仍是旧列名 quantity_basis（迁移未执行？）"])
        else:
            rep.check("列已改名 raw_quantity_basis", [])

        # 只读兼容别名列：存在时必须与 raw_quantity_basis 逐行同值（别名不得被当真源）
        v_alias = []
        if "quantity_basis" in cols:
            for nid, raw_b, alias_b in cur.execute(
                    "SELECT norm_id, raw_quantity_basis, quantity_basis "
                    "FROM Norm_Labor_Table"):
                if (raw_b is None) != (alias_b is None) or (
                        raw_b is not None and abs(float(raw_b) - float(alias_b)) > 0):
                    v_alias.append("%s raw=%r 别名=%r" % (nid, raw_b, alias_b))
            rep.check("别名列 quantity_basis == raw_quantity_basis（只读兼容）", v_alias)
        else:
            rep.check("别名列 quantity_basis：不存在（可选只读兼容列）", [])

        rows = cur.execute(
            "SELECT norm_id, activity_id, labor_norm_value, %s, labor_norm_unit, "
            "quantity_unit, productivity_value, raw_value "
            "FROM Norm_Labor_Table" % basis_col).fetchall()
    except sqlite3.Error as exc:
        rep.check("Norm_Labor_Table 可查询", ["查询失败：%s" % exc])
        rep.dump()
        con.close()
        return 1

    # ---- 1) productivity_value == 1 / labor_norm_value ----
    v1 = []
    for nid, aid, nv, b, nu, qu, pv, rv in rows:
        nv = to_float(nv)
        if nv is None or nv <= 0:
            continue
        pv = to_float(pv)
        want = 1.0 / nv
        if pv is None or abs(pv - want) >= EPS:
            v1.append("%s(%s) norm=%r 库内产能=%r 应为=%r" % (nid, aid, nv, pv, want))
    rep.check("① productivity_value == 1/labor_norm_value", v1)

    # ---- 2) raw_value / raw_quantity_basis == labor_norm_value ----
    v2, skipped = [], 0
    for nid, aid, nv, b, nu, qu, pv, rv in rows:
        f = to_float(rv)
        if f is None:
            skipped += 1
            continue
        bb = to_float(b)
        nv = to_float(nv)
        if bb is None or bb == 0 or nv is None:
            skipped += 1
            continue
        if abs(f / bb - nv) >= EPS:
            v2.append("%s(%s) raw=%r basis=%r → %r，库内 norm=%r"
                      % (nid, aid, f, bb, f / bb, nv))
    rep.check("② raw_value / raw_quantity_basis == labor_norm_value", v2,
              "跳过 %d 行（raw_value 非数字 / basis 缺失）" % skipped)

    # ---- 3) labor_norm_unit 分母 == 归一后的 quantity_unit ----
    v3 = []
    for nid, aid, nv, b, nu, qu, pv, rv in rows:
        den = U.denominator_of(nu)
        nqu = U.normalize_unit(qu)
        if den != nqu:
            v3.append("%s(%s) labor_norm_unit=%r 分母=%r ≠ quantity_unit 归一=%r"
                      % (nid, aid, nu, den, nqu))
    rep.check("③ labor_norm_unit 分母 == 规范 quantity_unit", v3)

    # ---- 4) labor_norm_unit 的 scale == 1.0 ----
    v4 = []
    for nid, aid, nv, b, nu, qu, pv, rv in rows:
        sc = U.parse_norm_unit(nu)["scale"]
        if sc != 1.0:
            v4.append("%s(%s) labor_norm_unit=%r scale=%r" % (nid, aid, nu, sc))
    rep.check("④ labor_norm_unit scale == 1.0（不允许 10/100/1000）", v4)

    # ---- 5) 单位写法归一 ----
    v5 = []
    for nid, aid, nv, b, nu, qu, pv, rv in rows:
        if U.normalize_unit(qu) != (qu or ""):
            v5.append("Norm_Labor_Table.%s(%s) quantity_unit=%r → 应为 %r"
                      % (nid, aid, qu, U.normalize_unit(qu)))
    rep.check("⑤a Norm_Labor_Table.quantity_unit 已是规范写法", v5)

    v5b = []
    if table_exists(cur, "Norm_Equipment_Table"):
        for nid, qu, nj, uj in cur.execute(
                "SELECT norm_id, quantity_unit, machine_shift_norm_json, "
                "machine_shift_unit_json FROM Norm_Equipment_Table"):
            if U.normalize_unit(qu) != (qu or ""):
                v5b.append("Norm_Equipment_Table.%s quantity_unit=%r → 应为 %r"
                           % (nid, qu, U.normalize_unit(qu)))
    rep.check("⑤b Norm_Equipment_Table.quantity_unit 已是规范写法", v5b)

    # ---- 5c) machine_shift_unit_json：元素 == 台班，且与 norm_json 等长 ----
    v5c = []
    if table_exists(cur, "Norm_Equipment_Table"):
        for nid, nj, uj in cur.execute(
                "SELECT norm_id, machine_shift_norm_json, machine_shift_unit_json "
                "FROM Norm_Equipment_Table"):
            try:
                norms = json.loads(nj) if nj else []
                units = json.loads(uj) if uj else []
            except (ValueError, TypeError) as exc:
                v5c.append("%s JSON 解析失败：%s" % (nid, exc))
                continue
            if not isinstance(norms, list) or not isinstance(units, list):
                v5c.append("%s 不是 JSON 数组" % nid)
                continue
            if len(norms) != len(units):
                v5c.append("%s 单位数组长度 %d ≠ 数值数组长度 %d"
                           % (nid, len(units), len(norms)))
            bad = [u for u in units if U.normalize_unit(u) != "台班" or u != "台班"]
            if bad:
                v5c.append("%s 单位元素未统一成字面「台班」：%r" % (nid, bad))
    rep.check("⑤c machine_shift_unit_json 统一为字面「台班」且长度一致", v5c)

    # ---- 5d) L4_Activity_Dictionary.unit 规范写法 ----
    v5d = []
    if table_exists(cur, "L4_Activity_Dictionary"):
        for aid, unit in cur.execute("SELECT activity_id, unit FROM L4_Activity_Dictionary"):
            if U.normalize_unit(unit) != (unit or ""):
                v5d.append("L4.%s unit=%r → 应为 %r" % (aid, unit, U.normalize_unit(unit)))
    rep.check("⑤d L4_Activity_Dictionary.unit 已是规范写法", v5d)

    # ---- 6) 工作面容量：**唯一一张** Workface_Capacity_Rule（合表后的 v3 结构）----
    # 合表前这里是"v2 表 478 行 == 旧表 478 行"的双表校验；后来旧两表改名归档
    # （Workface_Capacity_Rule_legacy_v1 / _legacy_v2，只读），运行时只认一张表；
    # H2/H3（2026-09-21）归档件也已删除，故选仅剩一张。
    CAP_TABLE = "Workface_Capacity_Rule"
    v6 = []
    if not table_exists(cur, CAP_TABLE):
        v6.append("表 %s 已按域 1.6 要求删除（容量唯一来源改为 Resource_Workface_Index MWI 表）"
                  % CAP_TABLE)
        rep.check("⑥ 工作面容量单表结构与标定", v6, "已删（域1.6）")
        v6b = v6c = []
        m_rows = m_null = 0
        rep.check("⑥b 机械侧整组有值或整组 NULL，且 min<=base<=max、min>=1", v6b, "缺表")
        rep.check("⑥c 机械覆盖率与 crew 常量行都在 notes 里可追溯", v6c, "缺表")
    else:
        # 6a) 结构 + 行数 + 人工侧标定自洽
        want_cols = ("crew_base", "crew_preferred", "crew_min", "crew_max",
                     "legacy_max_labor", "legacy_max_machine", "q_ref", "crew_step_q",
                     "crew_step_n", "segments_factor", "min_workface_qty",
                     "max_workface_qty", "review_state", "source_type", "confidence",
                     "notes", "created_at", "updated_at")
        have = table_columns(cur, CAP_TABLE)
        missing = [c for c in want_cols if c not in have]
        if missing:
            v6.append("缺列：%s" % missing)
        n_rows = cur.execute("SELECT COUNT(*) FROM %s" % CAP_TABLE).fetchone()[0]
        for rid, aid, cb, cp, cmin, cmax, lml, sq, sn in cur.execute(
                "SELECT rule_id, activity_id, crew_base, crew_preferred, crew_min, "
                "crew_max, legacy_max_labor, crew_step_q, crew_step_n FROM %s" % CAP_TABLE):
            if cb is None or cb <= 0:
                v6.append("%s(%s) crew_base=%r 必须 > 0" % (rid, aid, cb))
                continue
            if cmin is None or cmax is None or not (cmin <= cb <= cmax):
                v6.append("%s(%s) crew_min/crew_base/crew_max = %r/%r/%r 不满足 "
                          "crew_min <= crew_base <= crew_max" % (rid, aid, cmin, cb, cmax))
            if cp is None or cp < 0:
                v6.append("%s(%s) crew_preferred=%r 不合法" % (rid, aid, cp))
            if lml is None or lml <= 0:
                v6.append("%s(%s) legacy_max_labor=%r 必须 > 0（兼容键保真，不许丢）"
                          % (rid, aid, lml))
            if sq is None or sq < 0:
                v6.append("%s(%s) crew_step_q=%r 不合法" % (rid, aid, sq))
            elif sq == 0 and (sn or 0) != 0:
                v6.append("%s(%s) crew_step_q=0 但 crew_step_n=%r（只允许 step_n=0 时为 0）"
                          % (rid, aid, sn))
        rep.check("⑥ 工作面容量单表结构完整 + 人工侧标定自洽", v6,
                  "%s %d 行" % (CAP_TABLE, n_rows))

        # ---- 6b) 机械侧六列：要么整组 NULL（无数据），要么 min<=base<=max 且 min>=1 ----
        # 注意：`*_step_n == 0` **不是**违规 —— 它表示"容量与工程量无关的常量",
        # 是契约允许并且有意标定的（如"一项验收"）。机械侧无值只能是整组 NULL。
        v6b = []
        m_rows = m_null = 0
        for rid, aid, mqr, mb, msq, msn, mmin, mmax in cur.execute(
                "SELECT rule_id, activity_id, machine_q_ref, machine_base, "
                "machine_step_q, machine_step_n, machine_min, machine_max "
                "FROM %s" % CAP_TABLE):
            vals = (mqr, mb, msq, msn, mmin, mmax)
            if all(v is None for v in vals):
                m_null += 1
                continue
            if any(v is None for v in vals):
                v6b.append("%s(%s) 机械侧半 NULL（q_ref/base/step_q/step_n/min/max=%r）"
                           "—— 要么整组有值，要么整组保留 NULL（无机械容量数据）"
                           % (rid, aid, vals))
                continue
            m_rows += 1
            if msn > 0 and (msq is None or msq <= 0):
                v6b.append("%s(%s) machine_step_n=%r>0 但 machine_step_q=%r 不是正数"
                           % (rid, aid, msn, msq))
            if msn == 0 and msq <= 0:
                v6b.append("%s(%s) machine_step_n=0（常量）但 machine_step_q=%r<=0"
                           % (rid, aid, msq))
            if not (mmax >= mb >= mmin >= 1):
                v6b.append("%s(%s) machine_max >= machine_base >= machine_min >= 1 不成立："
                           "%r/%r/%r" % (rid, aid, mmax, mb, mmin))
            if mmin > mmax:
                v6b.append("%s(%s) machine_min=%r > machine_max=%r" % (rid, aid, mmin, mmax))
        rep.check("⑥b 机械侧整组有值或整组 NULL，且 min<=base<=max、min>=1", v6b,
                  "有值 %d 行 / 无机械容量数据 %d 行" % (m_rows, m_null))

        # ---- 6c) 覆盖率与 crew 常量行必须在 notes 里可追溯（供报告引用）----
        v6c = []
        # H2（2026-09-21）：归档表 `Workface_Capacity_Rule_legacy_v1` 已删除，
        # 原「机械侧有值行数 == 归档 max_machine>0 行数」改为**常量门禁**
        # （70 = 归档表删除前实测值），并断言归档表确实不存在。
        if table_exists(cur, "Workface_Capacity_Rule_legacy_v1"):
            v6c.append("归档表 Workface_Capacity_Rule_legacy_v1 仍存在"
                       "（H2 要求已删除）")
        if m_rows != 70:
            v6c.append("机械侧有值 %d 行 ≠ 70（归档 v1 表 max_machine>0 的定格值）"
                       % m_rows)
        named = cur.execute(
            "SELECT COUNT(*) FROM %s "
            "WHERE machine_max IS NULL AND notes LIKE '%%无机械容量数据%%'"
            % CAP_TABLE).fetchone()[0]
        if named != m_null:
            v6c.append("机械侧 NULL 的 %d 行里只有 %d 行 notes 写了「无机械容量数据」"
                       % (m_null, named))
        covered = cur.execute(
            "SELECT COUNT(*) FROM %s "
            "WHERE machine_max IS NOT NULL AND notes LIKE '%%旧表 max_machine%%'"
            % CAP_TABLE).fetchone()[0]
        if covered != m_rows:
            v6c.append("机械侧有值的 %d 行里只有 %d 行 notes 说明了来源" % (m_rows, covered))
        constant = cur.execute(
            "SELECT COUNT(*) FROM %s "
            "WHERE crew_step_n = 0 AND notes LIKE '%%有意为之%%'" % CAP_TABLE).fetchone()[0]
        const_all = cur.execute(
            "SELECT COUNT(*) FROM %s WHERE crew_step_n = 0" % CAP_TABLE).fetchone()[0]
        if constant != const_all:
            v6c.append("crew_step_n=0 的 %d 行里只有 %d 行 notes 标明「有意为之」"
                       % (const_all, constant))
        rep.check("⑥c 机械覆盖率与 crew 常量行都在 notes 里可追溯", v6c,
                  "旧表 max_machine>0 = %d 行；crew_step_n=0 = %d 行（有意为之）"
                  % (m_rows, const_all))

        # ---- 6d) 兼容列必须"另存"，不许用 crew_max 顶替 ----
        # H2（2026-09-21）：归档 v1 表已删除，"逐行等于归档原值"的比对无法再做，
        # 改为直接断言归档表不存在 + 保留"两个口径不许互相顶替"这一条可自证的不变量。
        v6d = []
        if table_exists(cur, "Workface_Capacity_Rule_legacy_v1"):
            v6d.append("归档表 Workface_Capacity_Rule_legacy_v1 仍存在"
                       "（H2 要求已删除）")
        # 兼容列必须真的"另存"，否则就是把 crew_max 当 max_labor 用了
        ne = cur.execute(
            "SELECT COUNT(*) FROM %s WHERE legacy_max_labor IS NOT crew_max"
            % CAP_TABLE).fetchone()[0]
        if ne == 0:
            v6d.append("全表 legacy_max_labor == crew_max：可疑 —— 实测应有 387 行不等，"
                       "像是用 crew_max 顶替了兼容键")
        rep.check("⑥d 归档 v1 表已删除 + 兼容列未被 crew_max 顶替", v6d,
                  "非空 legacy_max_labor %d 行；legacy_max_labor != crew_max %d 行"
                  % (n_rows, ne))

    # ---- 7) Production_Method_Baseline（契约 §3 接口的数据前提）----
    v7 = []
    if not table_exists(cur, "Production_Method_Baseline"):
        v7.append("表 Production_Method_Baseline 不存在")
    else:
        n = cur.execute("SELECT COUNT(*) FROM Production_Method_Baseline").fetchone()[0]
        if n == 0:
            v7.append("Production_Method_Baseline 为空")
        row = cur.execute("SELECT default_mode, qty_threshold_high, qty_threshold_low, "
                          "machine_activity_hint FROM Production_Method_Baseline "
                          "WHERE work_type_l3='earthwork'").fetchone()
        if not row:
            v7.append("缺少 earthwork 基线")
        else:
            if row[0] != "machine":
                v7.append("earthwork.default_mode=%r（应为 machine）" % row[0])
            if row[1] is None or row[2] is None:
                v7.append("earthwork 阈值缺失：high=%r low=%r" % (row[1], row[2]))
            hint = row[3]
            if hint and not cur.execute(
                    "SELECT 1 FROM L4_Activity_Dictionary WHERE activity_id=? "
                    "AND work_type_id='earthwork'", (hint,)).fetchone():
                v7.append("earthwork.machine_activity_hint=%r 不在 L4（或不属于 earthwork）" % hint)
    rep.check("⑦ Production_Method_Baseline 已灌且 earthwork 自洽", v7)

    # ---- 8) REQUIRED（必含）的工程类型必须**真的有工序可注入** ----
    # 缺陷类别（实测踩过）：标了 REQUIRED，名下 L4 却全是同一种没有独立量纲的空壳，
    # 于是 `missing_kb_essentials()`（wbs_agent.py:159）每次都对不上账 →
    # "补齐缺失必含工程类型"这个修复选项每次都弹、又永远补不出东西。
    # 典型案例 material_transport：10 个建筑类型全标 REQUIRED，名下 116 个 L4
    # 全是「XX运输」。已由 devtools/migrate_material_transport_to_usual.py 降级为
    # USUAL（材料运输消耗已含在各分项定额内），此处双向守住。
    v8 = []
    if not table_exists(cur, "Building_Type_L3_Mapping"):
        v8.append("表 Building_Type_L3_Mapping 不存在")
    else:
        bad = [r[0] for r in cur.execute(
            "SELECT building_type_id FROM Building_Type_L3_Mapping "
            "WHERE work_type_id='material_transport' AND applicability_level='REQUIRED' "
            "ORDER BY building_type_id")]
        if bad:
            v8.append("material_transport 仍在 %d 个建筑类型下标 REQUIRED：%s"
                      % (len(bad), "、".join(bad)))
        # 反向守卫：任何 REQUIRED 的 L3，只要名下所有 L4 都是同一空壳后缀
        # （「XX运输」），就一定会在生成期被判"必含缺失"却无从补起 → 违规。
        for l3, name in cur.execute(
                "SELECT DISTINCT m.work_type_id, w.work_type_name "
                "FROM Building_Type_L3_Mapping m JOIN L3_Work_Type w "
                "ON w.work_type_id = m.work_type_id "
                "WHERE m.applicability_level='REQUIRED' ORDER BY m.work_type_id"):
            acts = [a[0] for a in cur.execute(
                "SELECT activity_name FROM L4_Activity_Dictionary WHERE work_type_id=?", (l3,))]
            if acts and all(str(x).endswith("运输") for x in acts):
                v8.append("%s(%s) 名下的 %d 条 L4 全是「XX运输」，无可注入工序"
                          % (name, l3, len(acts)))
    rep.check("⑧ REQUIRED 的工程类型都必须有可注入工序（无空壳类型）", v8)

    con.close()
    ok = rep.dump()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
