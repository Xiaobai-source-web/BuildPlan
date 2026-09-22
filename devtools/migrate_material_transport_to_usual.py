"""KB 迁移：把「材料运输与加工工程」(material_transport) 从 REQUIRED 降级为 USUAL。

为什么改（三条都是库内可验证的事实，不是口味问题）：

  1. `Building_Type_L3_Mapping` 在 **10 个建筑类型**（含 residential）下都把它标成
     REQUIRED（必含），可它名下 **116 个 L4 活动全是「XX运输」**
     （TRANS_NEW_001 刨花板运输 / TRANS_NEW_002 各种笆片运输 / … / TRANS_NEW_068 砂运输）。
     也就是说：**标了"必须做"，却给不出一条可注入、可算量的施工工序**。
     `backend/pipeline/nodes/wbs_gen.py:22` 自己写着「不注入，由 LLM 按词库生成」。

  2. 工程实践里材料运输的消耗**已经含在各分项定额内**（`Norm_Labor_Table` 就是这么给的：
     每条 labor_norm 都是"完成该分项所需的综合工日"）。把"材料运输"单列成 WBS 工序，
     既没有独立的工程量口径，也没有独立的工期意义。

  3. 后果是**每次运行都误报**：`missing_kb_essentials()`（wbs_agent.py:159）拿 REQUIRED
     清单对账，`material_transport` 永远对不上（树里不可能有 116 条"XX运输"），
     于是"补齐缺失必含工程类型"这个修复选项**每次都弹**，而交回「施工准备」相重做
     也补不出任何东西 —— 用户看到的是一个永远不会消失、又永远修不好的 HIGH。

做法：**只改 applicability_level，不改 L4、不改其它 L3**，并在 notes 留痕。
幂等（只改当前仍为 REQUIRED 的行，重复跑不会重复追加留痕）。
落地后由 `devtools/verify_kb_invariants.py` 的 ⑦ 号不变量守住。

用法：
    python devtools/migrate_material_transport_to_usual.py            # 应用
    python devtools/migrate_material_transport_to_usual.py --check     # 只检查（退出码即结论）
"""

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "BuildPlan_KB", "kb.db")

WORK_TYPE_ID = "material_transport"
NOTE = "[迁移 material_transport→USUAL：名下 116 个 L4 全为「XX运输」，无可注入工序；" \
       "材料运输消耗已含在各分项定额内]"


def status(cur):
    """返回 {applicability_level: 行数}。"""
    rows = cur.execute(
        "SELECT applicability_level, COUNT(*) FROM Building_Type_L3_Mapping "
        "WHERE work_type_id = ? GROUP BY applicability_level", (WORK_TYPE_ID,)).fetchall()
    return {str(lv): int(n) for lv, n in rows}


def check(cur):
    """返回 (ok, 说明, 仍为 REQUIRED 的建筑类型列表)。"""
    st = status(cur)
    req = [r[0] for r in cur.execute(
        "SELECT building_type_id FROM Building_Type_L3_Mapping "
        "WHERE work_type_id = ? AND applicability_level = 'REQUIRED' "
        "ORDER BY building_type_id", (WORK_TYPE_ID,)).fetchall()]
    if req:
        return False, "仍为 REQUIRED 的建筑类型 %d 个：%s" % (len(req), "、".join(req)), req
    return True, "已是 USUAL（分布 %s）" % st, []


def apply(cur):
    before = status(cur)
    cur.execute(
        "UPDATE Building_Type_L3_Mapping "
        "SET applicability_level = 'USUAL', notes = COALESCE(notes, '') || ? "
        "WHERE work_type_id = ? AND applicability_level = 'REQUIRED'", (" " + NOTE, WORK_TYPE_ID))
    changed = cur.rowcount
    after = status(cur)
    return before, changed, after


def main(argv=None):
    ap = argparse.ArgumentParser(description="material_transport: REQUIRED → USUAL")
    ap.add_argument("--check", action="store_true", help="只检查，不写库")
    ap.add_argument("--db", default=DB_PATH)
    a = ap.parse_args(argv)

    if not os.path.exists(a.db):
        print("[FAIL] 找不到知识库：%s" % a.db)
        return 1

    con = sqlite3.connect(a.db)
    try:
        cur = con.cursor()
        before = status(cur)
        print("迁移前 applicability_level 分布：%s" % (before or "（无该 work_type 的行）"))
        if not before:
            print("[FAIL] Building_Type_L3_Mapping 里没有 %s —— 库结构不对，未做任何改动"
                  % WORK_TYPE_ID)
            return 1

        if a.check:
            ok, why, _ = check(cur)
            print(("[OK]  " if ok else "[FAIL]") + " 检查模式：" + why)
            return 0 if ok else 1

        _b, changed, after = apply(cur)
        con.commit()
        print("已更新 %d 行（只动仍为 REQUIRED 的行）" % changed)
        print("迁移后 applicability_level 分布：%s" % after)

        ok, why, _ = check(cur)
        print(("[OK]  " if ok else "[FAIL]") + " 复核：" + why)
        if not ok:
            return 1
        if changed == 0:
            print("[OK]  幂等：无需改动（此前已迁移过）")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
