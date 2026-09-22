"""把「塔吊」「施工电梯」两台垂直运输机械的**配员（crew）**加进知识库 —— 用户直接指令。

背景与边界（务必读完再改）
--------------------------
用户指令原文：**「你直接添加塔吊和施工电梯到机械表中，并记得添加 crew。」**

本脚本**只做一件事**：往 `Equipment_Crew_Mapping` 插 2 行配员记录，
让 `kb.crew_for_machine('塔吊')` / `kb.crew_for_machine('施工电梯')` 真的返回 crew
（而不是 None），从而 `crew_bind` 节点不再对这两台机械报"无配员数据"。

**不做**的事（每一步都查过库，理由写在第 2 步交付报告里）：

* 不往 `Activity_Main_Machine` 加主控机械行。实测 `Norm_Equipment_Table`
  243 行的 `machine_combination_json` 里**一台塔吊都没有**（0 行）——
  加了主控机械行，`norm_bind._pick_machine_row()` 必然返回 None，
  直接命中 `_downgrade_missing_machine()` → `usable=False`，
  会把本来能用的**混凝土输送泵车**定额挤掉（CONC_NEW_* 现在走
  `NE_CONC_002 泵车 0.055 台班/10m³`），属于**把计划算崩**，不是补数据。
* 不往 `Norm_Equipment_Table` 编台班定额行。库里的 `source_code` 只有
  真实规范来源（GD_2018_* / LD_T72_*）与 `AI_ESTIMATE_V1` 占位两类；
  编一行"塔吊 0.05 台班/台"就是**编数据冒充规范**，铁律禁止。
* 不往 `Workface_Capacity_Rule` 编容量行。缺容量时现有代码用
  "不封顶的默认值 + warning"，加一行 AI 造的 `machine_max` 只会让
  塔吊投入被凭空封顶。

诚实标注（铁律）
----------------
这 2 行**没有任何规范原文出处**：

* `source_type = 'user_directive'`（是用户要求加的，不是规范，也不是 AI 编的）
* `confidence  = 'LOW'`
* 依据写在 `notes` 里：塔吊照同为垂直运输机械的 `ECM_0002
  卷扬机架(单笼5t内)`（`LD_T72`/`HIGH`，司机1名+信号工1名）的口径；
  施工电梯的规格串 `单笼5t内` 是**真实规范字段借用**，不是编造。
* `legacy_id` 留 NULL；配员表 DDL 的 CHECK 只约束 confidence，不约束
  source_type（已用 `sqlite_master` 复核），因此 `user_directive` 合法。

幂等
----
按主键 `mapping_id` 判断已存在：第 2 次 `--apply` 报告"0 行变更"。
脚本每次写库前都先备份 `kb.db.bak_<时间戳>`。

用法::

    # 只读事实 + 试算（**默认**，不写库）
    python backend/tools/add_vertical_transport_equipment.py

    # 写库（先备份 kb.db.bak_<ts>，再插入，再 SELECT 回读）
    python backend/tools/add_vertical_transport_equipment.py --apply
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

# Windows 控制台默认 GBK，中文/符号会直接把脚本打断。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pipeline import config                                        # noqa: E402

TABLE = "Equipment_Crew_Mapping"
_DB_OVERRIDE = ""

# 真实规范来源（grep 过 sources 表）：只有这两个是"有原文出处"的机械配员来源。
REAL_SOURCE_TYPES = ("LD_T72", "guangdong_2018")


def _db_path():
    return _DB_OVERRIDE or str(config.KB_DB_PATH)


# ==================== 要写入的行（唯一事实来源） ====================
def _rows():
    """→ [(mapping_id, machine_name, machine_spec, crew_size, composition, notes)]

    `machine_spec` 一律**不编**：塔吊留空（库内无塔吊规格原文可借）；
    施工电梯借 `LD_T72` 里真实存在的 `单笼5t内` 档位串（与 ECM_0002
    `卷扬机架(单笼5t内)` 同源写法），并在 notes 里说明是借用。
    """
    return [
        {
            "mapping_id": "ECM_USER_塔吊",
            "machine_name": "塔吊",
            "machine_spec": "",
            "default_crew_size": 2,
            "crew_composition": "司机1名+信号工1名",
            "source_type": "user_directive",
            "confidence": "LOW",
            "notes": ("【用户指令】用户明确要求把塔吊加入机械表并配 crew"
                      "（2026-09 直接指令）。依据：同为垂直运输机械的现有行 "
                      "ECM_0002 卷扬机架(单笼5t内)（source_type=LD_T72 / "
                      "confidence=HIGH）= 司机1名+信号工1名，塔吊照此口径。"
                      "**无规范依据**（库内不存在塔吊台班定额，"
                      "Norm_Equipment_Table 243 行零命中），"
                      "machine_spec 留空以免编造起重量档位；"
                      "导入真实规范（垂直运输/施工机械台班定额）后应清退或替换。"),
        },
        {
            "mapping_id": "ECM_USER_施工电梯",
            "machine_name": "施工电梯",
            "machine_spec": "单笼5t内",
            "default_crew_size": 1,
            "crew_composition": "司机1名",
            "source_type": "user_directive",
            "confidence": "LOW",
            "notes": ("【用户指令】用户明确要求把施工电梯加入机械表并配 crew"
                      "（2026-09 直接指令）。依据：施工电梯为单人操纵的垂直运输"
                      "设备，配 1 名司机；machine_spec 借用 LD/T 72 中真实存在的"
                      "「单笼5t内」档位串（与 ECM_0002 卷扬机架同源写法），"
                      "非编造数值。**无规范依据**（库内无施工电梯台班定额），"
                      "待人工确认；导入真实规范后应清退或替换。"),
        },
    ]


def _cols():
    return ("mapping_id", "machine_name", "machine_spec", "default_crew_size",
            "crew_composition", "source_type", "confidence", "notes")


# ==================== 只读事实 ====================
def _read_all(db_path):
    """读全表（只读）；缺表/缺库 → None。"""
    if not os.path.exists(db_path):
        return None
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT %s FROM %s ORDER BY mapping_id"
                          % (", ".join(_cols()), TABLE))
        return [dict(zip(_cols(), r)) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        print("!! 读表失败（表缺失？）：%s" % exc)
        return None
    finally:
        con.close()


def _sources(db_path):
    """sources 表：确认 AI_ESTIMATE_V1 占位行存在（本脚本不写其它表）。"""
    try:
        con = sqlite3.connect(db_path)
        try:
            return [(r[0], r[1]) for r in con.execute(
                "SELECT source_code, source_type FROM sources ORDER BY source_code")]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def _facts(rows, db_path):
    """A 查证：现状分布（证明"塔吊/施工电梯此前 0 行"）。"""
    print("-- A 只读事实（改前现状）--")
    print("表 %s：%d 行" % (TABLE, len(rows)))
    print("现有 source_type 分布：%s"
          % _counts(rows, "source_type"))
    print("现有 confidence 分布：%s" % _counts(rows, "confidence"))
    print("现有 %s 的机械名（%d 个）：%s"
          % (TABLE, len(rows), "、".join(sorted(r["machine_name"] for r in rows))))
    hits = [r["mapping_id"] for r in rows
            if r["machine_name"] in ("塔吊", "施工电梯", "塔式起重机", "人货梯")]
    print("其中塔吊/塔式起重机/施工电梯/人货梯 = %d 行 %s"
          % (len(hits), hits or "（无）"))
    srcs = dict(_sources(db_path))
    print("sources 表 AI_ESTIMATE_V1 占位行：%s"
          % ("存在（%s）" % srcs.get("AI_ESTIMATE_V1") if "AI_ESTIMATE_V1" in srcs
             else "**缺失**"))
    print("")


def _counts(rows, key):
    out = {}
    for r in rows:
        out[r.get(key)] = out.get(r.get(key), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (kv[0] is None, str(kv[0]))))


# ==================== 试算 ====================
def _plan(rows):
    """→ (要插入的行, 已存在的行)。按 mapping_id 主键判重（幂等的全部依据）。"""
    have = {r["mapping_id"] for r in rows}
    todo, skip = [], []
    for want in _rows():
        (skip if want["mapping_id"] in have else todo).append(want)
    return todo, skip


def _print_plan(todo, skip, existing):
    print("-- B 试算（%s）--" % "dry-run，不写库")
    if not todo:
        print("会插入的行：0（这 2 个 mapping_id 已存在 → 幂等，无变更）")
    else:
        print("会插入的行：%d" % len(todo))
        for w in todo:
            print("   INSERT %s：machine_name=%s / machine_spec=%r / "
                  "default_crew_size=%s / crew_composition=%s"
                  % (w["mapping_id"], w["machine_name"], w["machine_spec"],
                     w["default_crew_size"], w["crew_composition"]))
            print("          source_type=%s / confidence=%s / legacy_id=NULL"
                  % (w["source_type"], w["confidence"]))
            print("          notes=%s" % w["notes"])
    if skip:
        print("已存在、会跳过：%s" % [w["mapping_id"] for w in skip])
    print("不改动任何已存在的行：本脚本只有 INSERT，没有 UPDATE/DELETE"
          "（现有 %d 行逐行不动）" % len(existing))
    # 诚实性自检：绝不许出现"像规范"的来源
    bad = [w["mapping_id"] for w in todo
           if str(w["source_type"]) in REAL_SOURCE_TYPES
           or str(w["source_type"]).startswith(("GD_", "LD_T72"))]
    print("诚实性自检：会把来源写成规范来源的行 = %d %s"
          % (len(bad), "✔" if not bad else "**违规，禁止写库** " + str(bad)))
    print("")


# ==================== 写库 ====================
def _apply(db_path, todo):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = "%s.bak_%s" % (db_path, stamp)
    shutil.copy2(db_path, bak)
    print("已备份：%s" % bak)
    n = 0
    con = sqlite3.connect(db_path)
    try:
        sql = ("INSERT INTO %s (%s) VALUES (%s)"
               % (TABLE, ", ".join(_cols()), ", ".join("?" * len(_cols()))))
        for w in todo:
            con.execute(sql, tuple(w[c] for c in _cols()))
            n += 1
        con.commit()
    finally:
        con.close()
    print("已插入 %d 行（只 INSERT，未改任何既有行）" % n)
    return n, bak


def _readback(db_path, ids):
    """SELECT 回读作为证据。"""
    print("-- C 回读证据（SELECT）--")
    con = sqlite3.connect(db_path)
    try:
        for mid in ids:
            rows = con.execute(
                "SELECT %s FROM %s WHERE mapping_id = ?" % (", ".join(_cols()), TABLE),
                (mid,)).fetchall()
            for r in rows:
                for c, v in zip(_cols(), r):
                    print("   %-18s %-18s = %s" % (mid, c, v))
                print("   " + "-" * 70)
        total = con.execute("SELECT COUNT(*) FROM %s" % TABLE).fetchone()[0]
        print("   表 %s 总行数（回读）= %d" % (TABLE, total))
    finally:
        con.close()
    print("")


# ==================== crew 通道验收 ====================
def _verify_crew():
    """调 kb.crew_for_machine() —— 证明 crew 真的取得到（不是 None）。"""
    print("-- D crew 通道验收（kb.crew_for_machine）--")
    try:
        from pipeline import kb
        from pipeline.nodes.crew_bind import parse_crew_composition
    except Exception as exc:
        print("!! 导入 kb/crew_bind 失败：%s" % exc)
        return 1
    bad = 0
    for name in ("塔吊", "施工电梯"):
        got = kb.crew_for_machine(name)
        print("   crew_for_machine(%r) = %s" % (name, got))
        if got is None:
            print("   **失败：返回 None**")
            bad += 1
            continue
        crew = parse_crew_composition(got.get("composition"))
        print("        composition=%r 解析为 %s（人数合计=%s）"
              % (got.get("composition"), crew, sum(crew.values())))
        if not crew:
            print("   **失败：composition 解析不出工种**")
            bad += 1
    # 模糊匹配通道（KB 机械名常带规格后缀）
    for probe in ("塔吊QTZ80", "施工电梯SC200/200"):
        print("   模糊匹配 crew_for_machine(%r) = %s"
              % (probe, kb.crew_for_machine(probe)))
    print("   → %s" % ("全部通过 ✔" if not bad else "有 %d 项失败" % bad))
    print("")
    return bad


def main():
    ap = argparse.ArgumentParser(
        description="把塔吊/施工电梯的配员（crew）加进 Equipment_Crew_Mapping")
    ap.add_argument("--apply", action="store_true",
                    help="写库（默认只试算；写前自动备份 kb.db.bak_<ts>）")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="只试算不写库（默认行为，显式传也无副作用）")
    ap.add_argument("--db", default="", help="覆盖 kb.db 路径（默认 config.KB_DB_PATH）")
    args = ap.parse_args()

    global _DB_OVERRIDE
    _DB_OVERRIDE = args.db or ""
    db_path = _db_path()

    print("== 加入垂直运输机械配员（塔吊 / 施工电梯）==")
    print("库：%s" % db_path)
    if not os.path.exists(db_path):
        print("!! 找不到 kb.db —— 缺数据就如实报缺，不猜、不写。")
        return 2
    rows = _read_all(db_path)
    if rows is None:
        print("!! 表不可读（迁移未跑？）—— 不写、不猜。")
        return 2
    _facts(rows, db_path)

    todo, skip = _plan(rows)
    _print_plan(todo, skip, rows)

    if not args.apply:
        print("（dry-run：未写库。确认无误后加 --apply）")
        ids = [w["mapping_id"] for w in todo] or [w["mapping_id"] for w in _rows()]
        _readback(db_path, [i for i in ids if any(r["mapping_id"] == i for r in rows)])
        _verify_crew()
        return 0

    if todo:
        _apply(db_path, todo)
    else:
        print("本库已存在这 2 个 mapping_id → 未写库、未备份（幂等：0 行变更）")
    after = _read_all(db_path) or []
    want_ids = [w["mapping_id"] for w in _rows()]
    _readback(db_path, want_ids)

    # 幂等复核：再算一遍
    todo2, _skip2 = _plan(after)
    print("-- E 幂等复核 --")
    print("表行数 %d → %d（应 +%d）" % (len(rows), len(after), len(todo)))
    print("重算一遍仍会插入 %d 行 → %s"
          % (len(todo2), "幂等 ✔（再跑 --apply 就是 0 行变更）"
             if not todo2 else "**不幂等**，请检查"))
    src = _counts([r for r in after if r["mapping_id"] in want_ids], "source_type")
    conf = _counts([r for r in after if r["mapping_id"] in want_ids], "confidence")
    print("回读新行来源：source_type=%s ；confidence=%s"
          "（应为 user_directive / LOW，**不许美化**）" % (src, conf))
    print("")
    return _verify_crew()


if __name__ == "__main__":
    raise SystemExit(main())
