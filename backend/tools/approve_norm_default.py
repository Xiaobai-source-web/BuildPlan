"""人工审定 L4_Norm_Default —— 把默认行从 pending 改成 approved（或 rejected）。

第 39 轮起闸门**按来源分档**（见 `pipeline/norm_defaults.py`）：`verified` / `parsed`
（真人来源）**不需要审定**就能决定工期，但会在交付物里标成"未经人工审定"；
`estimated`（AI 经验估算）一律拦下；被 `rejected` 的行永远不复活。
所以这个脚本现在的职责是两件事：

  1. **否决**（`--reject`）—— 唯一能把某一行彻底关掉的开关（一票否决）；
  2. **确认**（`--approve`）—— 把"未经人工审定"的标注摘掉，交付物显示"已审定"。

用法::

    # 看现状（按置信度分组，列出待定的）
    python backend/tools/approve_norm_default.py --list
    python backend/tools/approve_norm_default.py --list --confidence verified
    python backend/tools/approve_norm_default.py --show FORM_NEW_FOUND

    # 批量放行：只放行 verified/parsed 档（estimated 档永远不批量放行）
    python backend/tools/approve_norm_default.py --approve --confidence verified,parsed --by 张三

    # 单条放行 / 否决 / 改值
    python backend/tools/approve_norm_default.py --approve FORM_NEW_FOUND
    python backend/tools/approve_norm_default.py --reject  FORM_ALU_INSTALL
    python backend/tools/approve_norm_default.py --set FORM_ALU_INSTALL --value 0.9 \
        --unit 工日/m2 --source INDUSTRY_REF_2024 --approve

    # 回滚成待定
    python backend/tools/approve_norm_default.py --reset FORM_NEW_FOUND

## 为什么 estimated 档不许批量放行

`estimated` = KB 里没有该 L4 的定额行，或 status 不在 verified/parsed
（AI 经验估算 / 待解析）。这些值**没有任何规范来源**，批量放行等于把
"AI 临场选值"换个地方重演一遍。要放行必须**逐条**指定并写清来源
（`--value` + `--source`），并在 `notes` 里留下依据。
"""

from __future__ import annotations

import argparse
import os
import sys
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

from pipeline import kb                       # noqa: E402
from pipeline import norm_defaults as nd      # noqa: E402

_COLS = ("activity_id", "quantity_unit", "norm_kind", "condition_key", "norm_value",
         "norm_unit", "source_code", "source_kind", "confidence", "default_crew",
         "review_state", "reviewed_by", "reviewed_at", "notes")


def _rows(where="", params=()):
    sql = "SELECT %s FROM %s" % (", ".join(_COLS), nd.TABLE)
    if where:
        sql += " WHERE " + where
    return [dict(zip(_COLS, r)) for r in kb._query_all(sql, params)]


def _list(args):
    where, params = "", ()
    if args.confidence:
        confs = [c.strip() for c in args.confidence.split(",") if c.strip()]
        where = "confidence IN (%s)" % ",".join("?" * len(confs))
        params = tuple(confs)
    if args.state:
        where = (where + " AND " if where else "") + "review_state = ?"
        params = params + (args.state,)
    rows = _rows(where, params)
    rows.sort(key=lambda r: (r["confidence"], r["activity_id"]))

    print("== L4_Norm_Default（%d 行）==" % len(rows))
    for r in rows[: args.limit]:
        mark = {"approved": "[OK]", "pending": "[  ]", "rejected": "[XX]"}.get(
            str(r["review_state"]), "[??]")
        print("  %s %-22s %-8s %-12s %-9s crew=%s  %s"
              % (mark, r["activity_id"], r["quantity_unit"],
                 ("%.4f" % float(r["norm_value"])) if r["norm_value"] is not None else "-",
                 r["confidence"], r["default_crew"], r["norm_unit"]))
    if len(rows) > args.limit:
        print("  … 还有 %d 行（用 --limit 调整）" % (len(rows) - args.limit))
    print("\n放行命令示例："
          "\n  python backend/tools/approve_norm_default.py --approve --confidence verified,parsed --by <你的名字>")


def _show(args):
    rows = _rows("activity_id = ?", (args.show,))
    if not rows:
        print("没有该 L4 的默认行：%s" % args.show)
        return
    for r in rows:
        print("---- %s / %s / %s" % (r["activity_id"], r["quantity_unit"], r["norm_kind"]))
        for k in _COLS:
            print("   %-14s %s" % (k, r[k]))


def _update(aid, **fields):
    if not fields:
        return 0
    sets = ", ".join("%s = ?" % k for k in fields)
    params = tuple(fields.values()) + (aid,)
    conn = kb._connect()
    try:
        cur = conn.execute("UPDATE %s SET %s WHERE activity_id = ?" % (nd.TABLE, sets),
                           params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show", default="")
    ap.add_argument("--confidence", default="")
    ap.add_argument("--state", default="")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--approve", nargs="?", const="", default=None)
    ap.add_argument("--reject", default="")
    ap.add_argument("--reset", default="")
    ap.add_argument("--set", default="")
    ap.add_argument("--value", type=float, default=None)
    ap.add_argument("--unit", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--by", default=os.environ.get("USERNAME") or os.environ.get("USER") or "manual")
    args = ap.parse_args()

    nd.ensure_table()

    if args.show:
        return _show(args) or 0
    # 有明确动作就不打印整表（`--approve` 不带值时是批量放行，不是"列个表"）。
    action = (args.approve is not None or bool(args.reject)
              or bool(args.reset) or bool(args.set))
    if args.list or not action:
        _list(args)
        if args.list or not action:
            return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---- 单条 / 批量放行 ----
    if args.approve is not None and args.approve != "":
        aid = args.approve
        n = _update(aid, review_state=nd.STATE_APPROVED, reviewed_by=args.by,
                    reviewed_at=now)
        nd.clear_cache()
        print("已放行 %s（%d 行）" % (aid, n))
        return 0

    if args.approve == "" and args.confidence:
        confs = [c.strip() for c in args.confidence.split(",") if c.strip()]
        bad = [c for c in confs if c == nd.CONF_ESTIMATED]
        if bad:
            print("!! 拒绝批量放行 estimated 档（无规范来源）。"
                  "请逐条用 --set <L4> --value <值> --source <来源> --approve")
            return 2
        conn = kb._connect()
        try:
            cur = conn.execute(
                "UPDATE %s SET review_state=?, reviewed_by=?, reviewed_at=? "
                "WHERE confidence IN (%s) AND review_state != ?"
                % (nd.TABLE, ",".join("?" * len(confs))),
                tuple([nd.STATE_APPROVED, args.by, now] + confs + [nd.STATE_APPROVED]))
            conn.commit()
            n = cur.rowcount
        finally:
            conn.close()
        nd.clear_cache()
        print("已批量放行 %d 行（confidence ∈ %s）" % (n, confs))
        return 0

    if args.reject:
        n = _update(args.reject, review_state=nd.STATE_REJECTED,
                    reviewed_by=args.by, reviewed_at=now)
        nd.clear_cache()
        print("已否决 %s（%d 行）" % (args.reject, n))
        return 0

    if args.reset:
        n = _update(args.reset, review_state=nd.STATE_PENDING,
                    reviewed_by="", reviewed_at="")
        nd.clear_cache()
        print("已重置为待定 %s（%d 行）" % (args.reset, n))
        return 0

    # ---- 改值（常用于补 KB 缺的 L4，如铝模）----
    if args.set:
        aid = args.set
        rows = _rows("activity_id = ?", (aid,))
        if not rows:
            print("!! 没有 %s 的默认行。先建行："
                  "\n   INSERT via --create（TODO）或先跑 seed 脚本" % aid)
            return 2
        fields = {}
        if args.value is not None:
            fields["norm_value"] = args.value
        if args.unit:
            fields["norm_unit"] = args.unit
        if args.source:
            fields["source_code"] = args.source
            fields["source_kind"] = "manual"
            fields["confidence"] = nd.CONF_VERIFIED
        if args.approve:
            fields["review_state"] = nd.STATE_APPROVED
            fields["reviewed_by"] = args.by
            fields["reviewed_at"] = now
        n = _update(aid, **fields)
        nd.clear_cache()
        print("已更新 %s（%d 行）：%s" % (aid, n, fields))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
