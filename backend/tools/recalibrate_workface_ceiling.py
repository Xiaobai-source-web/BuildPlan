"""重定工作面容量的**每施工段人数上限**（`Workface_Capacity_Rule.crew_max`）—— 第 39 轮。

要解决什么
----------
`crew_max` 是"一个施工段最多站几个人"的上限，全表 478 行只有
{3,4,5,6,8,12,15,16} 八个取值（3~16 人）—— 它是标定 v2 时照**旧表同族最大值**取的，
于是大工程量任务先撞上限：同一份计划（`terminal/plans/plan_run_1789827002.json`，322 条
任务）里 **49 条**的人工容量顶到 `crew_max`（混凝土运输 4260 m³ 也只有 15 人），
工程量再大也不加人。

重定口径（与 `pipeline/nodes/scheduler.py::effective_crew_max` **同一个函数**，不另写一份）::

    effective = max(crew_max, min(CREW_CEILING_CAP, ceil(crew_base × CREW_CEILING_BAND)))
              = max(crew_max, min(40, ceil(crew_base × 2.5)))

  · `CREW_CEILING_BAND = 2.5`：相对**典型班组** `crew_base` 的放宽带宽（AI 经验带，
    与 `crew_base` 同源，不是规范值）；
  · `CREW_CEILING_CAP = 40`：一个施工段站得下的人数上限（经验值，可改）——再往上加人
    不会缩短工期，只会互相干扰；
  · 取 `max` 保证**只抬不降**：原库已经比新口径大的行原样保留；`crew_base` 缺失不动。

用法::

    # 只读事实 + 试算（**默认**，不写库）
    python backend/tools/recalibrate_workface_ceiling.py
    python backend/tools/recalibrate_workface_ceiling.py --stats      # 只印 A3 事实
    python backend/tools/recalibrate_workface_ceiling.py --limit 40

    # 写回（先备份 kb.db.bak_<ts>，只改 crew_max + 追加 notes）
    python backend/tools/recalibrate_workface_ceiling.py --apply

不许美化来源
------------
这 478 行全是 `source_type='ai_estimate'` / `confidence='LOW'`：重定上限是**AI 经验标定**，
不是规范数据。脚本**只写 `crew_max` 与 `notes`**，`source_type` / `confidence` 原样保留
（`--apply` 的复核会把这两列原样打印出来，防止有人顺手"提升"置信度）。

幂等
----
写回后的 `crew_max` 再喂回 `effective_crew_max` 仍是同一个值 → 第二次 `--apply` 会报告
"会改 0 行"（脚本把这条复核打出来）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime

# Windows 控制台默认 GBK，`㎡³` 这类字符会直接把脚本打断。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pipeline import config                                        # noqa: E402
from pipeline.nodes import scheduler as S                          # noqa: E402

TABLE = "Workface_Capacity_Rule"          # 合表后的唯一容量表（v3 结构）
_NOTE_TAG = "第39轮重定上限"


def _db_path():
    return str(config.KB_DB_PATH)


def _rows(db_path):
    """读全表（只读）。缺表/缺库 → 空列表（调用方据此报"缺"，不编数）。"""
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "SELECT rule_id, activity_id, work_type_l3, quantity_unit, crew_base, "
            "crew_min, crew_max, segments_factor, machine_max, source_type, confidence, "
            "notes FROM %s ORDER BY activity_id" % TABLE)
        cols = ("rule_id", "activity_id", "work_type_l3", "quantity_unit", "crew_base",
                "crew_min", "crew_max", "segments_factor", "machine_max", "source_type",
                "confidence", "notes")
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        print("!! 读表失败（表缺失？）：%s" % exc)
        return []
    finally:
        con.close()


def _dist(rows, key):
    return dict(sorted(Counter(r.get(key) for r in rows).items(),
                       key=lambda kv: (kv[0] is None, kv[0])))


def _plan(rows):
    """→ (会改的行, 不受影响的行数)。每项 = (row, old, new)。"""
    todo = []
    for r in rows:
        old = r.get("crew_max")
        new = S.effective_crew_max(r.get("crew_base"), r.get("crew_min"), old)
        if new is None or old is None:
            continue
        if float(new) > float(old) + 1e-9:
            todo.append((r, old, new))
    return todo, len(rows) - len(todo)


def _stats(rows):
    """A3 只读事实：segments_factor 分布（全 1 → 公式行为不改）+ crew_max 分布 + 来源。"""
    print("-- 只读事实（第 39 轮 A 查证 / B 现状）--")
    sf = _dist(rows, "segments_factor")
    non_unit = sum(1 for r in rows
                   if r.get("segments_factor") not in (1, 1.0))
    print("segments_factor 取值分布：%s" % (sf or "（无行）"))
    print("  → 非 1 行数 = %d：%s" % (
        non_unit,
        "契约 §5-WS4 ⑤ 只在 ==1 时放大；全表皆 1 ⇒ 闸门现值\"全开\"，本次**不改公式行为**"
        if non_unit == 0 else "**有非 1 行**，必须逐行看语义（0 = 不按施工段并行）"))
    cm = _dist(rows, "crew_max")
    vals = [r["crew_max"] for r in rows if r.get("crew_max") is not None]
    print("crew_max 取值分布：%s" % (cm or "（无行）"))
    if vals:
        print("  → min/max = %s/%s，共 %d 行（这就是「一段最多几人」目前的上限）"
              % (min(vals), max(vals), len(rows)))
    print("crew_base 取值分布：%s" % (_dist(rows, "crew_base") or "（无行）"))
    print("source_type：%s ；confidence：%s（**写回时不许美化**）"
          % (_dist(rows, "source_type") or "（无）", _dist(rows, "confidence") or "（无）"))
    print("")


def _print_plan(todo, untouched, limit):
    print("-- 上限重定试算（%s）--" % "dry-run，不写库")
    print("口径：effective = max(crew_max, min(%s, ceil(crew_base × %s)))"
          % (S.CREW_CEILING_CAP, S.CREW_CEILING_BAND))
    if not todo:
        print("会改的行：0（上限已全部不低于新口径，或表里没有 crew_base）")
        return
    acts = {r.get("activity_id") for r, _o, _n in todo}
    print("会改的行：%d / %d ；受影响活动数（去重 activity_id）：%d ；不受影响：%d"
          % (len(todo), len(todo) + untouched, len(acts), untouched))
    # 抬升幅度最大的前 10
    top = sorted(todo, key=lambda t: -(float(t[2]) - float(t[1])))[:10]
    print("抬升幅度最大的前 10 条：")
    for r, old, new in top:
        print("   %-26s crew_base=%-4s crew_max %s → %s（+%s）  %s"
              % (r.get("activity_id"), r.get("crew_base"), old, new,
                 int(float(new) - float(old)), (r.get("work_type_l3") or "")))
    print("逐行明细（按 activity_id 排序，前 %d 行）：" % limit)
    for r, old, new in sorted(todo, key=lambda t: str(t[0].get("activity_id")))[:limit]:
        print("   %-26s %-10s crew_base=%-4s %s → %s"
              % (r.get("activity_id"), r.get("quantity_unit"), r.get("crew_base"),
                 old, new))
    if len(todo) > limit:
        print("   … 还有 %d 行（用 --limit 调整）" % (len(todo) - limit))
    print("按新上限分组：%s"
          % dict(sorted(Counter(int(t[2]) for t in todo).items())))
    print("")


def _apply(db_path, todo, rows):
    """写回：备份 → 逐行 UPDATE crew_max + notes 追加标签 → 复核幂等。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = "%s.bak_%s" % (db_path, stamp)
    shutil.copy2(db_path, bak)
    print("已备份：%s" % bak)
    now_tag = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(db_path)
    n = 0
    try:
        for r, old, new in todo:
            note = str(r.get("notes") or "").strip()
            add = "%s：%s→%s（%s）" % (_NOTE_TAG, old, new, now_tag)
            note = (note + "；" + add) if note else add
            con.execute(
                "UPDATE %s SET crew_max = ?, notes = ? WHERE rule_id = ?" % TABLE,
                (int(new), note, r.get("rule_id")))
            n += 1
        con.commit()
    finally:
        con.close()
    print("已写回 %d 行（只改 crew_max + notes；source_type/confidence/crew_preferred "
          "原样保留 —— crew_preferred 语义是\"无节拍要求时的默认配置\"，"
          "重定物理上限 crew_max 不改它）" % n)
    after = _rows(db_path)
    todo2, _u2 = _plan(after)
    print("复核：重算一遍仍会改 %d 行 → %s"
          % (len(todo2), "幂等 ✔" if not todo2 else "**不幂等**，请检查"))
    src = _dist(after, "source_type")
    conf = _dist(after, "confidence")
    print("复核来源：source_type=%s ；confidence=%s（应仍为 ai_estimate/LOW）"
          % (src, conf))
    return n


def main():
    ap = argparse.ArgumentParser(
        description="重定 Workface_Capacity_Rule.crew_max（每施工段人数上限，第 39 轮）")
    ap.add_argument("--apply", action="store_true",
                    help="写回库（默认只试算；写前自动备份 kb.db.bak_<ts>）")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="只试算不写库（默认行为，显式传也无副作用）")
    ap.add_argument("--stats", action="store_true", help="只印 A3 只读事实（分布）后退出")
    ap.add_argument("--limit", type=int, default=20, help="逐行明细打印行数（默认 20）")
    ap.add_argument("--db", default="", help="覆盖 kb.db 路径（默认 config.KB_DB_PATH）")
    args = ap.parse_args()

    db_path = args.db or _db_path()
    print("== 工作面容量上限重定（第 39 轮）==")
    print("库：%s" % db_path)
    if not os.path.exists(db_path):
        print("!! 找不到 kb.db —— 缺数据就如实报缺，不猜、不写。")
        return 2
    rows = _rows(db_path)
    print("表 %s：%d 行" % (TABLE, len(rows)))
    if not rows:
        print("!! 表为空或不可读（迁移未跑？）—— 不写、不猜。")
        return 2
    _stats(rows)
    if args.stats:
        return 0

    todo, untouched = _plan(rows)
    _print_plan(todo, untouched, max(1, args.limit))

    if not args.apply:
        print("（dry-run：未写库。确认无误后加 --apply）")
        return 0

    _apply(db_path, todo, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
