# -*- coding: utf-8 -*-
"""知识库替换：把 WS6 在副本上迁移好的库换进真库（父代理专用）。

用法：
    python devtools/kb_swap_migrated.py                 # dry-run，只体检
    python devtools/kb_swap_migrated.py --apply         # 备份后替换

设计原则：
  · 默认 dry-run，不写任何东西。
  · 替换前先备份真库（已存在同名备份则不覆盖）。
  · 体检不通过就**拒绝替换**，退出码 1。
  · 只认「副本不丢表、数据表行数不少于真库」——WS6 只该加列/加行/改 notes，不该删数据。
    若某表行数变少，必须显式 --allow-shrink 才放行（并打印是哪张表少了多少）。
"""
import argparse
import io
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FROM = os.path.join(ROOT, "BuildPlan_KB", "kb.db.migrated")
DEFAULT_TO = os.path.join(ROOT, "BuildPlan_KB", "kb.db")

MEASURE_SCOPE_TABLES = [
    "Norm_Labor_Table",
    "Norm_Equipment_Table",
    "L4_Norm_Default",
    "L4_Activity_Dictionary",
]

# 这些表在迁移中只允许"行数不变或增加"；meta 类表允许任意
# （H4/H5 2026-09-21：`Norm_Adjustment` / `Norm_Adjustment_Target` 已删除，从白名单移除）
ALLOW_SHRINK_TABLES = {"sqlite_sequence", "data_quality_log"}


def tables_of(con):
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def counts(con, tabs):
    out = {}
    for t in tabs:
        try:
            out[t] = con.execute("SELECT COUNT(*) FROM [%s]" % t).fetchone()[0]
        except Exception as e:
            out[t] = "ERR:%s" % e
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=DEFAULT_FROM)
    ap.add_argument("--to", dest="dst", default=DEFAULT_TO)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--allow-shrink", action="store_true")
    args = ap.parse_args()

    problems = []
    notes = []

    if not os.path.exists(args.src):
        print("源库不存在：%s" % args.src)
        return 1
    if not os.path.exists(args.dst):
        print("目标库不存在：%s" % args.dst)
        return 1

    print("源（迁移副本）：%s  %.1f MB" % (args.src, os.path.getsize(args.src) / 1e6))
    print("目标（真库）  ：%s  %.1f MB" % (args.dst, os.path.getsize(args.dst) / 1e6))

    try:
        con_s = sqlite3.connect("file:%s?mode=ro" % args.src.replace("\\", "/"), uri=True)
        con_s.execute("SELECT COUNT(*) FROM sqlite_master")
    except Exception as e:
        print("源库打不开或不是有效 sqlite：%s" % e)
        return 1
    con_d = sqlite3.connect("file:%s?mode=ro" % args.dst.replace("\\", "/"), uri=True)

    t_s, t_d = tables_of(con_s), tables_of(con_d)

    # 1) 不丢表
    lost = sorted(t_d - t_s)
    if lost:
        problems.append("副本丢失了真库里的表：%s" % lost)
    added = sorted(t_s - t_d)
    if added:
        notes.append("副本新增表：%s" % added)

    common = sorted(t_d & t_s)
    c_s, c_d = counts(con_s, common), counts(con_d, common)

    # 2) 行数对照
    print("\n%-34s %8s %8s %s" % ("表", "真库", "副本", "差"))
    shrink = []
    for t in common:
        a, b = c_d.get(t), c_s.get(t)
        if isinstance(a, int) and isinstance(b, int):
            d = b - a
            if d != 0 or t in ("L4_Norm_Default", "Norm_Equipment_Table",
                               "Norm_Labor_Table", "L4_Activity_Dictionary",
                               "Workface_Capacity_Rule", "sources"):
                print("%-34s %8d %8d %+d" % (t, a, b, d))
            if d < 0 and t not in ALLOW_SHRINK_TABLES:
                shrink.append((t, a, b, d))
        else:
            print("%-34s %8s %8s  (查不了)" % (t, a, b))
    if shrink:
        msg = "副本行数变少的表：%s" % ", ".join("%s %d→%d" % s for s in shrink)
        if args.allow_shrink:
            notes.append("已 --allow-shrink 放行：" + msg)
        else:
            problems.append(msg + "（如确认是有意删行，加 --allow-shrink）")

    # 3) measure_scope 是否落地
    print("\n=== measure_scope 落地检查 ===")
    for t in MEASURE_SCOPE_TABLES:
        if t not in t_s:
            problems.append("副本缺表：%s" % t)
            continue
        cols = {r[1] for r in con_s.execute("PRAGMA table_info([%s])" % t)}
        if "measure_scope" not in cols:
            problems.append("%s 仍无 measure_scope 列" % t)
            print("  FAIL  %s 无列" % t)
        else:
            n_filled = con_s.execute(
                "SELECT COUNT(*) FROM [%s] WHERE IFNULL(measure_scope,'')<>''" % t
            ).fetchone()[0]
            n_all = c_s.get(t, 0)
            print("  OK    %s 有列，已填 %d/%s" % (t, n_filled, n_all))

    # 4) 台班索引
    if "L4_Norm_Default" in t_s:
        try:
            dist = dict(con_s.execute(
                "SELECT norm_kind, COUNT(*) FROM L4_Norm_Default GROUP BY norm_kind"))
            print("\nL4_Norm_Default.norm_kind 分布：%s" % dist)
            if not dist.get("machine"):
                notes.append("注意：L4_Norm_Default 仍无 machine 索引行（C6 未做）")
        except Exception as e:
            notes.append("norm_kind 查不了：%s" % e)

    con_s.close()
    con_d.close()

    print("\n" + "=" * 72)
    for n in notes:
        print("  备注  %s" % n)
    for p in problems:
        print("  问题  %s" % p)
    print("=" * 72)

    if problems:
        print("体检不通过 → 拒绝替换（真库未改动）")
        return 1

    if not args.apply:
        print("dry-run：体检通过，未替换。加 --apply 执行。")
        return 0

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = args.dst + ".bak_%s_pre_final" % stamp
    if os.path.exists(bak):
        print("备份已存在，跳过备份：%s" % bak)
    else:
        shutil.copy2(args.dst, bak)
        print("已备份真库 → %s" % bak)

    tmp = args.dst + ".swap_tmp"
    shutil.copy2(args.src, tmp)
    os.replace(tmp, args.dst)
    print("已替换真库：%s（来自 %s）" % (args.dst, args.src))

    # 替换后复验
    con = sqlite3.connect(args.dst)
    t = tables_of(con)
    ok = all(x in t for x in MEASURE_SCOPE_TABLES)
    print("替换后复验：表数 %d，measure_scope 四表齐全 = %s" % (len(t), ok))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
