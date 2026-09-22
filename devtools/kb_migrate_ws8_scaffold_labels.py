#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WS8 知识库迁移：把「依据只有 SCAFFOLD_V1 占位」的主控机械行**如实标注**。

背景（用户 2026-09-20 裁定：**保留占位，但必须全面如实标注**）
----------------------------------------------------------------
WS6 本轮给 4 个预制构件活动（CONC_PC_SLAB / CONC_PC_BALCONY / CONC_PC_BAYWIN /
CONC_PC_STAIR）加了主控机械行，机械均为「塔式起重机」。这些行的**唯一**依据是
`Norm_Equipment_Table` 里的占位台班行（`source_code='SCAFFOLD_V1'` /
`source_type='scaffold_placeholder'` / `status='needs_review'`），不是真规范。

但 `Activity_Main_Machine` 里这 4 行却写成 `source_type='regional_quota'` /
`confidence='HIGH'` —— **假标签**（缺陷 ①）。本脚本按裁定把它们改成
`source_type='scaffold_placeholder'` / `confidence='LOW'`，即「如实标注」。

审计范围：**全部 70 行** Activity_Main_Machine（不只这 4 行）
------------------------------------------------------------
逐行判据：该行的 `activity_id` + `machine_name` 在 `Norm_Equipment_Table` 里
能不能找到**任何非 SCAFFOLD 的台班行**（同一 activity_id、机械名出现在
`machine_combination_json` 数组里，且 source_code 不以 SCAFFOLD 开头、
source_type 不是 scaffold_placeholder）。找不到 = 无真规范依据 → 一律按上法改标。

不做的事（红线）
----------------
  · 不改 `condition_text`（主键组成部分）、不改 `machine_name`、不删任何行；
  · 只写显式传入的 `--db`，绝不修改 `backend/pipeline/**` 或 `backend/plans/**`。

用法（默认 dry-run，不写库）
----------------------------
  python devtools/kb_migrate_ws8_scaffold_labels.py --db BuildPlan_KB\\kb.db            # dry-run
  python devtools/kb_migrate_ws8_scaffold_labels.py --db BuildPlan_KB\\kb.db --apply    # 备份后提交
  python devtools/kb_migrate_ws8_scaffold_labels.py --db BuildPlan_KB\\kb.db --verify   # 只读校验

安全约定（照抄 devtools/kb_migrate_ws6.py / kb_migrate_ws6_fix.py）：
  `--db` 必填且**没有默认值**；默认 dry-run 走 BEGIN/ROLLBACK；`--apply` 先备份
  （`<db>.bak_<时间戳>_pre_scaffold_labels`，同名已存在则跳过）再 COMMIT；**幂等**。
"""
from __future__ import print_function

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

# --------------------------------------------------------------------------- 常量

SCAFFOLD_CODE = "SCAFFOLD_V1"
NEW_SOURCE_TYPE = "scaffold_placeholder"
NEW_CONFIDENCE = "LOW"
BACKUP_SUFFIX = "_pre_scaffold_labels"


def log(msg):
    print(msg)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def json_list(value):
    """把 `machine_combination_json` 解析成字符串列表（坏值 → 空表，不抛）。"""
    try:
        got = json.loads(value or "[]")
    except Exception:
        return []
    if not isinstance(got, list):
        return []
    return [str(x) for x in got]


def is_scaffold_row(source_code, source_type):
    """该台班行是不是占位（而不是真规范）。"""
    code = str(source_code or "").strip().upper()
    stype = str(source_type or "").strip().lower()
    return code.startswith("SCAFFOLD") or stype == NEW_SOURCE_TYPE


def audit(cur):
    """逐行审计 Activity_Main_Machine（全部行）→ ``(rows, summary)``（只读）。"""
    pools = {}
    for nid, aid, mcomb, scode, stype in cur.execute(
            "SELECT norm_id, activity_id, machine_combination_json, source_code, "
            "source_type FROM Norm_Equipment_Table"):
        pools.setdefault(aid, []).append(
            (nid, json_list(mcomb), str(scode or ""), str(stype or "")))

    rows = []
    for rid, aid, ct, mn, stype, conf in cur.execute(
            "SELECT rowid, activity_id, IFNULL(condition_text,''), machine_name, "
            "IFNULL(source_type,''), IFNULL(confidence,'') FROM Activity_Main_Machine "
            "ORDER BY activity_id, condition_text"):
        cands = pools.get(aid, [])
        real = [nid for (nid, machines, sc, st) in cands
                if not is_scaffold_row(sc, st) and mn in machines]
        scaff = [nid for (nid, machines, sc, st) in cands
                 if is_scaffold_row(sc, st) and mn in machines]
        rows.append(dict(rowid=rid, activity_id=aid, condition_text=ct,
                         machine_name=mn, source_type=stype, confidence=conf,
                         real_norms=real, scaffold_norms=scaff))
    summary = dict(total=len(rows),
                   with_basis=sum(1 for r in rows if r["real_norms"]),
                   without_basis=sum(1 for r in rows if not r["real_norms"]),
                   already_marked=sum(
                       1 for r in rows
                       if not r["real_norms"]
                       and r["source_type"] == NEW_SOURCE_TYPE
                       and r["confidence"] == NEW_CONFIDENCE))
    return rows, summary


def migrate(cur, rows):
    """把无真规范依据的行改标（幂等）。返回逐行改动明细。"""
    changes = []
    for r in rows:
        rec = dict(r)
        if r["real_norms"]:
            rec["action"] = "KEEP（有真规范依据，不动）"
        elif (r["source_type"] == NEW_SOURCE_TYPE
              and r["confidence"] == NEW_CONFIDENCE):
            rec["action"] = "SKIP（已是如实标注，幂等）"
        else:
            cur.execute(
                "UPDATE Activity_Main_Machine SET source_type=?, confidence=? "
                "WHERE rowid=?", (NEW_SOURCE_TYPE, NEW_CONFIDENCE, r["rowid"]))
            rec["action"] = "UPDATE"
        changes.append(rec)
    return changes


def print_audit(rows, summary):
    log("")
    log("-" * 78)
    log(u"70 行审计（判据：该 activity_id + machine_name 在 Norm_Equipment_Table 里"
        u"有无非 SCAFFOLD 台班行）")
    log("-" * 78)
    log(u"  Activity_Main_Machine 总行数            : %d" % summary["total"])
    log(u"  —— 有真规范依据（保留原标签）           : %d" % summary["with_basis"])
    log(u"  —— 无真规范依据（必须如实标注）         : %d" % summary["without_basis"])
    log(u"     其中已如实标注（幂等）              : %d" % summary["already_marked"])
    if summary["without_basis"] == 4 and summary["with_basis"] == summary["total"] - 4:
        log(u"  结论：**只有 4 行缺真规范依据** —— 与 WS7 指控的行数一致。")
    log(u"")
    log(u"  无真规范依据的行明细：")
    for r in rows:
        if r["real_norms"]:
            continue
        log(u"    rowid=%-4d %-22s ct=%-12s %-12s  现标签 %s/%s  占位依据=%s"
            % (r["rowid"], r["activity_id"], repr(r["condition_text"]),
               r["machine_name"], r["source_type"], r["confidence"],
               ",".join(r["scaffold_norms"]) or "(无)"))


def print_changes(changes):
    log("")
    log("-" * 78)
    log(u"逐行改动（改前 → 改后；只列无真规范依据的行）")
    log("-" * 78)
    n_upd = n_skip = 0
    for c in changes:
        if c["real_norms"]:
            continue
        if c["action"] == "UPDATE":
            n_upd += 1
            log(u"  UPDATE rowid=%-4d %-22s ct=%-12s %-12s  %s/%s  ->  %s/%s"
                % (c["rowid"], c["activity_id"], repr(c["condition_text"]),
                   c["machine_name"], c["source_type"], c["confidence"],
                   NEW_SOURCE_TYPE, NEW_CONFIDENCE))
        else:
            n_skip += 1
            log(u"  SKIP   rowid=%-4d %-22s ct=%-12s %-12s  已是 %s/%s（幂等）"
                % (c["rowid"], c["activity_id"], repr(c["condition_text"]),
                   c["machine_name"], NEW_SOURCE_TYPE, NEW_CONFIDENCE))
    log(u"  小计：本次改标 %d 行 / 已标注跳过 %d 行" % (n_upd, n_skip))


def verify(cur):
    """只读校验：没有一行「声称规范来源却缺真规范依据」。返回坏行列表。"""
    rows, summary = audit(cur)
    bad = [r for r in rows
           if not r["real_norms"]
           and not (r["source_type"] == NEW_SOURCE_TYPE
                    and r["confidence"] == NEW_CONFIDENCE)]
    # 反向核对：有真规范依据的行不该被误标成占位（过度标注也是失真）
    over = [r for r in rows
            if r["real_norms"] and r["source_type"] == NEW_SOURCE_TYPE]
    dist = {}
    for r in rows:
        dist["%s/%s" % (r["source_type"], r["confidence"])] = \
            dist.get("%s/%s" % (r["source_type"], r["confidence"]), 0) + 1
    log(u"Activity_Main_Machine 共 %d 行；有真规范依据 %d 行；无真规范依据 %d 行"
        % (summary["total"], summary["with_basis"], summary["without_basis"]))
    log(u"source_type/confidence 分布：%s" % json.dumps(dist, ensure_ascii=False,
                                                        sort_keys=True))
    log(u"声称规范来源却缺真规范依据的行数（必须为 0）：%d" % len(bad))
    for r in bad:
        log(u"   !! rowid=%d %s %s 现标签 %s/%s"
            % (r["rowid"], r["activity_id"], r["machine_name"],
               r["source_type"], r["confidence"]))
    log(u"有真规范依据却被标成占位的行数（必须为 0，防过度标注）：%d" % len(over))
    for r in over:
        log(u"   !! rowid=%d %s %s" % (r["rowid"], r["activity_id"], r["machine_name"]))
    return bad + over


def main():
    ap = argparse.ArgumentParser(
        description=u"WS8：占位主控机械行如实标注（默认 dry-run，不写库）")
    ap.add_argument("--db", required=True,
                    help=u"目标 kb.db（必填，只写这一份；无默认值）")
    ap.add_argument("--apply", action="store_true",
                    help=u"真正提交（默认 dry-run 回滚）")
    ap.add_argument("--verify", action="store_true",
                    help=u"只读校验，不做任何改动")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        log("ERROR: 找不到 --db %s" % args.db)
        return 2

    # --verify：只读，先走
    if args.verify:
        con = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
        try:
            log("=" * 78)
            log(u"WS8 只读校验  db=%s" % args.db)
            log("=" * 78)
            bad = verify(con.cursor())
        finally:
            con.close()
        log(u"")
        log(u"校验结果：%s" % (u"PASS" if not bad else u"FAIL（%d 行）" % len(bad)))
        return 0 if not bad else 1

    mode = u"APPLY（提交）" if args.apply else u"DRY-RUN（回滚，不写库）"
    log("=" * 78)
    log(u"WS8 占位主控机械行如实标注  db=%s  模式=%s" % (args.db, mode))
    log("=" * 78)

    con = sqlite3.connect(args.db)
    con.isolation_level = None
    cur = con.cursor()

    rows, summary = audit(cur)
    print_audit(rows, summary)

    if args.apply:
        bak = args.db + ".bak_%s%s" % (time.strftime("%Y%m%d_%H%M%S"), BACKUP_SUFFIX)
        if os.path.exists(bak):
            log(u"备份已存在，跳过备份：%s" % bak)
        else:
            shutil.copy2(args.db, bak)
            log(u"已备份 → %s" % bak)

    cur.execute("BEGIN")
    try:
        changes = migrate(cur, rows)
        print_changes(changes)
        log("")
        verify(cur)
        if args.apply:
            cur.execute("COMMIT")
            log("")
            log(u"已提交（--apply）。")
        else:
            cur.execute("ROLLBACK")
            log("")
            log(u"已回滚（dry-run）：库未被修改。加 --apply 才会写入。")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        con.close()

    log("")
    log(u"db sha256 = %s" % sha256_of(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
