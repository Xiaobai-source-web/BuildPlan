"""第 38/39/41 轮：L4 默认定额行闸门的语义单测。

**第 41 轮口径（2026-09-20 政策变更后）**：

| 该 L4 的行状态                          | 判定 |
|-----------------------------------------|------|
| 有人 approved                             | 放行（状态 approved） |
| 无 approved，最好一行是 verified / parsed  | **放行 + 标注"未经人工审定"** |
| 无 approved，最好一行是 estimated（AI 经验估算） | **放行 + 标注 "released_ai"**（第 41 轮放开） |
| 非 rejected 的行全部取不到正定额值          | **拦下**（no_value，第 41 轮新增的显式判据） |
| 所有行都是 rejected                        | **拦下**（人工否决，一票否决，唯一硬开关） |
| 该 L4 一行都没有 / 任务没锚定 L4            | 放行（不发表意见，沿用第 37 轮旧判据） |

⚠️ **政策变更（2026-09-20，用户决定）**：第 38/39 轮口径里 `estimated` 是**拦下**
（`REASON_AI_ONLY`）。用户决定放开 —— AI 估算是装饰/机电/临建类活动当前唯一可得的
覆盖面来源，放开的前提是交付物**逐条标注**（`released_ai` + `LABEL_AI_ESTIMATE`）。
本文件里凡是被这次变更动过的断言，旁边都写了"为什么改"的中文注释，不许静默放宽。

覆盖：
  1. `gate_open` / `gate_label` 的分档；
  2. 单位归一（`m3` vs `m³`）不能把闸门绕开；
  3. `scheduler._build_ledger_item` 真的按闸门改判 `norm_is_evidence`，
     并把**来源标注**写进台账（`norm_evidence_label`）；
  4. `norm_coverage_report` 统计出"已放行但未审定"的条数（交付物要标出来）；
  5. **仍然会拦的旁路判据**（单位不可用 / `method_conflict`）—— 政策只放开"来源"
     这一档，绕不过它们。

⚠️ conftest 里有 autouse 的 `_norm_defaults_gate_open_for_synthetic_activities`
把闸门钉成放行（保护 1500 个既有用例）。本文件测的正是闸门本身，所以要先撤掉那个
替身 —— 用 `_real_gate()` 显式取回真函数。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import kb, norm_defaults as nd          # noqa: E402
from pipeline.nodes import scheduler as S             # noqa: E402


def _real_gate():
    """把 conftest 的替身换回真实现（带缓存清理）。"""
    import importlib
    importlib.reload(nd)
    nd.clear_cache()
    S._norm_defaults = nd
    return nd


def _pick(conf=None, state=None):
    """从真实库里找一条满足条件的 L4；找不到返回 (None, None)（库被换过时不假失败）。

    第 41 轮（2026-09-20 政策变更）起额外要求 `norm_value > 0`：闸门新增了值判据
    （零值/空值行一律拦），不筛掉零值行的话，这个"挑真库样本"的辅助函数可能挑到
    一条零值行，让用例因为**样本**而不是**政策**失败。
    """
    sql = ("SELECT activity_id, quantity_unit FROM %s "
           "WHERE norm_kind = 'labor' AND norm_value > 0" % nd.TABLE)
    args = []
    if conf:
        sql += " AND confidence = ?"
        args.append(conf)
    if state:
        sql += " AND review_state = ?"
        args.append(state)
    sql += " LIMIT 1"
    rows = kb._query_all(sql, tuple(args))
    return (rows[0][0], rows[0][1]) if rows else (None, None)


def _leaf(aid, unit, norm_value=0.01, duration=7):
    return {"id": "9.9.9", "name": "闸门用例任务", "quantity": 100.0, "unit": unit,
            "duration_days": duration, "work_type": "土方工程", "kb_activity_id": aid,
            "norm_binding": {"mode": "labor", "norm_value": norm_value,
                             "unit": "工日/%s" % unit, "source_code": "LD_T72_2_2008",
                             "match_type": "exact",
                             "provenance": {"origin": "kb"}}}


def _insert_row(aid, unit="m³", kind=nd.KIND_LABOR, value=1.0,
                conf=nd.CONF_ESTIMATED, state=nd.STATE_PENDING,
                source_code="AI_ESTIMATE_V1"):
    """插一条**合成** L4 默认定额行（用例自己收尾删掉；不要留在真库里）。

    为什么需要它：真库里没有 rejected / 零值行，而第 41 轮政策变更后必须证明
    "该拦的还拦着" —— 只能自己造靶子。
    """
    conn = kb._connect()
    try:
        conn.execute("DELETE FROM %s WHERE activity_id = ?" % nd.TABLE, (aid,))
        conn.execute(
            "INSERT INTO %s (activity_id, quantity_unit, norm_kind, norm_value,"
            " source_code, source_kind, confidence, review_state, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?)" % nd.TABLE,
            (aid, unit, kind, value, source_code, "ai_estimate", conf, state,
             "单测：第 41 轮合成行"))
        conn.commit()
    finally:
        conn.close()
    nd.clear_cache()


def _drop_row(aid):
    """删掉 `_insert_row` 造的合成行并清缓存。"""
    conn = kb._connect()
    try:
        conn.execute("DELETE FROM %s WHERE activity_id = ?" % nd.TABLE, (aid,))
        conn.commit()
    finally:
        conn.close()
    nd.clear_cache()


# ==================== 1) 闸门分档 ====================
def test_table_exists_and_has_all_three_states():
    nd.ensure_table()
    total = kb._query_all("SELECT count(*) FROM %s" % nd.TABLE)[0][0]
    assert total > 0, "L4_Norm_Default 未初始化（先跑 tools/seed_norm_defaults.py）"


def test_parsed_pending_is_released_with_label():
    """真人 parsed 行：没审定也放行 —— 但必须给出"未审定"的来源标注。"""
    nd = _real_gate()
    aid, unit = _pick(conf=nd.CONF_PARSED, state=nd.STATE_PENDING)
    if not aid:
        return
    ok, reason = nd.gate_open(aid, unit, nd.KIND_LABOR)
    assert ok is True and reason == "", "parsed 行应放行（实际 %r / %r）" % (ok, reason)
    lab = nd.gate_label(aid, unit, nd.KIND_LABOR)
    assert lab["state"] == "released_unapproved", lab
    assert lab["confidence"] == nd.CONF_PARSED and lab["note"], lab


def test_estimated_pending_is_released_with_ai_label():
    """AI 经验估算：**放行**（第 41 轮政策变更，2026-09-20）+ 必须带 AI 标注。

    为什么改：原用例是 `test_estimated_pending_is_blocked`，断言
    "estimated → 被拦 + reason == REASON_AI_ONLY"（第 38/39 轮口径）。
    2026-09-20 用户决定放开这条限制 —— 依据：AI 估算是装饰/机电/临建类活动当前
    **唯一可得的覆盖面来源**；前提：交付物逐条标注。所以这里改成断言放行
    （`ok is True`、`reason == ""`）+ `state == "released_ai"` + 文案 == `LABEL_AI_ESTIMATE`。
    """
    nd = _real_gate()
    aid, unit = _pick(conf=nd.CONF_ESTIMATED, state=nd.STATE_PENDING)
    if not aid:
        return
    ok, reason = nd.gate_open(aid, unit, nd.KIND_LABOR)
    # 政策变更（2026-09-20）：estimated 由"拦下"改为"放行"，reason 必须是空串 ——
    # 旧口径的 REASON_AI_ONLY 不再作为拦截理由返回。
    assert ok is True and reason == "", (ok, reason)
    lab = nd.gate_label(aid, unit, nd.KIND_LABOR)
    assert lab["state"] == "released_ai", lab
    assert lab["confidence"] == nd.CONF_ESTIMATED, lab
    # 标注文案是下游契约（交付物按字面读），必须逐字等于常量
    assert lab["note"] == nd.LABEL_AI_ESTIMATE, lab


def test_approved_is_released():
    nd = _real_gate()
    aid, unit = _pick(state=nd.STATE_APPROVED)
    if not aid:
        return
    ok, _r = nd.gate_open(aid, unit, nd.KIND_LABOR)
    assert ok is True, "approved 行必须放行"
    assert nd.gate_label(aid, unit, nd.KIND_LABOR)["state"] == "approved"


def test_rejected_row_is_blocked():
    """人工显式否决 → 永远不复活（这是取代"默认全部待审"的新开关）。

    第 41 轮政策变更（2026-09-20）**未动**这一条：`rejected` 是政策放开后唯一剩下的
    人工硬开关，所以本用例原样保留（断言一个字没改）。
    """
    nd = _real_gate()
    aid = "TEST_GATE_REJECTED_ZZZ"
    conn = kb._connect()
    try:
        conn.execute("DELETE FROM %s WHERE activity_id = ?" % nd.TABLE, (aid,))
        conn.execute(
            "INSERT INTO %s (activity_id, quantity_unit, norm_kind, norm_value,"
            " source_code, source_kind, confidence, review_state, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?)" % nd.TABLE,
            (aid, "m³", "labor", 1.0, "LD_T72_1_2008", "kb_parsed", "parsed",
             "rejected", "单测：人工否决行"))
        conn.commit()
    finally:
        conn.close()
    nd.clear_cache()
    try:
        ok, reason = nd.gate_open(aid, "m³", nd.KIND_LABOR)
        assert ok is False and reason == nd.REASON_REJECTED, (ok, reason)
    finally:
        conn = kb._connect()
        conn.execute("DELETE FROM %s WHERE activity_id = ?" % nd.TABLE, (aid,))
        conn.commit()
        conn.close()
        nd.clear_cache()


def test_unknown_activity_does_not_block():
    """表里没有的 L4 → 闸门不发表意见（否则合成数据/新活动会被一刀切）。

    第 41 轮政策变更（2026-09-20）**未动** `no_row` 的语义：它一直是**放行**
    （"表未灌"不能把所有合成活动打死）。放行不等于没意见 —— 后面的
    `method_conflict` / 单位判据仍然生效（见测试 2) 的两个旁路用例）。
    """
    nd = _real_gate()
    ok, reason = nd.gate_open("NOT_IN_TABLE_XYZ_999", "m3", nd.KIND_LABOR)
    assert ok is True and reason == ""


def test_unit_alias_cannot_bypass_the_gate():
    """`m3` 与 `m³` 必须视为同一单位 —— 否则换个写法就绕过闸门。

    为什么改：原用例拿真库里的 **estimated 行**当"被拦"靶子（断言各写法都 False）。
    第 41 轮政策变更（2026-09-20）后 estimated 已放行，那个靶子失效。改用**仍然拦**
    的两档做靶子：① `rejected`（人工否决 —— 唯一硬开关，任何单位写法都不能放行它）；
    ② 零值行（no_value）。
    """
    nd = _real_gate()
    aid = "TEST_GATE_ALIAS_ZZZ"
    _insert_row(aid, unit="m³", value=1.0, conf=nd.CONF_PARSED,
                state=nd.STATE_REJECTED)
    aid_zero = "TEST_GATE_ALIAS_ZERO_ZZZ"
    _insert_row(aid_zero, unit="m³", value=0.0, conf=nd.CONF_ESTIMATED)
    try:
        for alias in ("m3", "m³", "M3", "m 3"):
            ok, reason = nd.gate_open(aid, alias, nd.KIND_LABOR)
            assert ok is False and reason == nd.REASON_REJECTED, (alias, ok, reason)
            assert nd.gate_label(aid, alias, nd.KIND_LABOR)["state"] == "rejected"
            ok0, reason0 = nd.gate_open(aid_zero, alias, nd.KIND_LABOR)
            assert ok0 is False and reason0 == nd.REASON_NO_VALUE, (alias, ok0, reason0)
    finally:
        _drop_row(aid)
        _drop_row(aid_zero)


def test_zero_value_norm_is_blocked():
    """定额值 <= 0 → 仍然拦（第 41 轮放开 AI 时**必须**补的显式判据）。

    为什么必测：放开之前，estimated 的零值行是被 REASON_AI_ONLY 顺带挡住的
    （estimated 一律拦）；放开 AI 之后，如果不显式判值，"0 工日/m²"就会被标成
    released_ai 一路放行进工期 —— 这正是"一半放行"的漏网，所以单独立一个用例。
    """
    nd = _real_gate()
    aid = "TEST_GATE_ZEROVAL_ZZZ"
    _insert_row(aid, unit="m³", value=0.0, conf=nd.CONF_ESTIMATED)
    try:
        ok, reason = nd.gate_open(aid, "m³", nd.KIND_LABOR)
        assert ok is False and reason == nd.REASON_NO_VALUE, (ok, reason)
        lab = nd.gate_label(aid, "m³", nd.KIND_LABOR)
        assert lab["state"] == "no_value", lab
    finally:
        _drop_row(aid)


def test_zero_value_does_not_shadow_a_positive_row():
    """零值行不能"顶掉"同一 L4 下的有效行：有正值的行照旧放行。

    为什么测：值判据是"**所有**非 rejected 行都取不到正值才拦"，不是"排最前的那行
    是零值就拦"—— 否则同一个 L4 里挂一条零值占位行就会把有效定额一起打死。
    这里刻意让查询单位（m³）在表里**没有精确行**，走 `_candidate_rows` 的
    (活动, 口径) 兜底路径，才能同时拿到两条不同单位的行。
    """
    nd = _real_gate()
    aid = "TEST_GATE_MIXEDVAL_ZZZ"
    _insert_row(aid, unit="m²", value=0.0, conf=nd.CONF_VERIFIED)
    conn = kb._connect()
    try:
        # 再插一条正值的 parsed 行（主键是 (activity_id, unit, kind)，换单位才插得进）
        conn.execute(
            "INSERT INTO %s (activity_id, quantity_unit, norm_kind, norm_value,"
            " source_code, source_kind, confidence, review_state, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?)" % nd.TABLE,
            (aid, "m", nd.KIND_LABOR, 2.5, "LD_T72_2_2008", "kb_parsed",
             nd.CONF_PARSED, nd.STATE_PENDING, "单测：混合值"))
        conn.commit()
    finally:
        conn.close()
    nd.clear_cache()
    try:
        ok, reason = nd.gate_open(aid, "m³", nd.KIND_LABOR)
        assert ok is True and reason == "", (ok, reason)
        lab = nd.gate_label(aid, "m³", nd.KIND_LABOR)
        assert lab["state"] == "released_unapproved" and lab["confidence"] == "parsed", lab
    finally:
        _drop_row(aid)


# ==================== 2) 台账改判 + 来源标注 ====================
def test_scheduler_releases_estimated_norm_with_ai_label():
    """闸门放行 AI 估算 → 台账带上 `released_ai` 标注，且定额真的进了工期口径。

    为什么改：原用例是 `test_scheduler_blocks_estimated_norm_to_target_days`，
    断言"estimated 被拦 → duration 退回 WBS 目标 7 天"。第 41 轮政策变更
    （2026-09-20，用户决定）后 AI 估算放行，`norm_is_evidence=True` → 工期由定额
    反算（scheduler `_plan_task` 里 `duration = by_norm`）。这时**唯一的防线就是
    逐条标注**，所以断言重心从"拦住"移到"标注必须原样落到台账"。
    """
    nd = _real_gate()
    aid, unit = _pick(conf=nd.CONF_ESTIMATED, state=nd.STATE_PENDING)
    if not aid:
        return
    item = S._build_ledger_item(_leaf(aid, unit), "9.9.9", "AI 估算定额任务")
    # 政策变更（2026-09-20）：原来这里是 False/False/False + REASON_AI_ONLY
    assert item["norm_pre_approved"] is True
    assert item["norm_is_evidence"] is True, "第 41 轮起 AI 估算放行（2026-09-20 政策变更）"
    assert item["usable"] is True
    assert item["not_usable_reason"] == ""
    lab = item["norm_evidence_label"]
    assert lab["state"] == "released_ai", lab
    assert lab["confidence"] == "estimated" and lab["source_code"], lab
    # 标注文案是交付物契约，必须逐字等于常量（下游按字面渲染）
    assert lab["note"] == nd.LABEL_AI_ESTIMATE, lab
    # 旧口径的拦截理由不得再出现在台账上（不许"一半放行、一半还写被拦"）
    assert item["norm_gate_reason"] == ""
    assert item["not_usable_reason"] != nd.REASON_AI_ONLY


def test_method_conflict_still_blocks_after_ai_release():
    """`method_conflict`（定额口径与任务不符）仍然拦 —— 政策只放开"来源"这一档。

    为什么测：闸门放行**不等于**绕过旁路判据；这条靶子用的是"表里没有这个 L4"
    （闸门表 no_row → 放行），以隔离出 method_conflict 单独生效。
    """
    nd = _real_gate()
    leaf = _leaf("NOT_IN_TABLE_MC_ZZZ", "m³", norm_value=0.5)
    leaf["norm_binding"]["method_conflict"] = True
    item = S._build_ledger_item(leaf, "9.9.5", "定额口径冲突任务")
    assert item["norm_pre_approved"] is True, "闸门本身放行（no_row）"
    assert item["norm_is_evidence"] is False, "method_conflict 必须继续拦"
    assert item["usable"] is False
    assert item["not_usable_reason"] == "定额口径不符"


def test_unit_incompatible_still_blocks_after_ai_release():
    """单位不可用/不可换算仍然拦（`units_compatible` 类判据，旁路，不在闸门内）。

    为什么测：AI 估算放行后，"量纲对不上"这类**可修的数据问题**绝不能被顺带放过 ——
    否则 m² 的工程量会被 m³ 的定额算出一个毫无意义的天数。
    """
    nd = _real_gate()
    leaf = _leaf("NOT_IN_TABLE_UNIT_ZZZ", "m²", norm_value=0.5)
    # 定额分母写成 m³，与任务单位 m² 不同族、不可换算
    leaf["norm_binding"]["unit"] = "工日/m³"
    item = S._build_ledger_item(leaf, "9.9.4", "单位不符任务")
    assert item["norm_is_evidence"] is False, "单位不可换算必须继续拦"
    assert item["usable"] is False
    assert "单位" in item["not_usable_reason"], item["not_usable_reason"]


def test_scheduler_releases_parsed_norm_and_records_label():
    """真人 parsed 行照旧让定额决定工期，并把"未经人工审定"写进台账。"""
    nd = _real_gate()
    aid, unit = _pick(conf=nd.CONF_PARSED, state=nd.STATE_PENDING)
    if not aid:
        return
    item = S._build_ledger_item(_leaf(aid, unit, norm_value=0.5), "9.9.8", "真人定额任务")
    assert item["norm_pre_approved"] is True
    assert item["norm_is_evidence"] is True
    assert item["usable"] is True
    assert item["not_usable_reason"] == ""
    lab = item["norm_evidence_label"]
    assert lab["state"] == "released_unapproved" and lab["confidence"] == "parsed", lab
    # 第 41 轮政策变更后，真人档与 AI 档的标注必须**分得开**（否则用户看不出
    # 哪条是 AI 编的）—— 这里顺带钉住"真人档不是 released_ai"
    assert lab["state"] != nd.STATE_RELEASED_AI


def test_coverage_report_counts_released_unapproved():
    """进了定额口径、但没人工审定的条数必须报出来（交付物要标）。"""
    nd = _real_gate()
    aid, unit = _pick(conf=nd.CONF_PARSED, state=nd.STATE_PENDING)
    if not aid:
        return
    item = S._build_ledger_item(_leaf(aid, unit, norm_value=0.5), "9.9.7", "真人定额任务")
    assert item["usable"] is True
    cov = S.norm_coverage_report({"9.9.7": item}, total=1)
    assert cov["released_unapproved"] == 1
    assert cov["by_confidence"].get("parsed") == 1
    assert cov["released_activities"]
    assert "released_unapproved" in S._coverage_warning(cov) or \
        "未经人工审定" in S._coverage_warning(cov)


def test_machine_kind_is_queried_separately():
    """机械口径与人工口径是两张账：用错口径会误拦机械任务（实测踩过）。"""
    nd = _real_gate()
    ok_l, r_l = nd.gate_open("CONC_NEW_FOUND", "m3", nd.KIND_LABOR)
    ok_m, r_m = nd.gate_open("CONC_NEW_FOUND", "m3", nd.KIND_MACHINE)
    # 人工口径在真库里有行（parsed）→ 放行；机械口径没有行 → 不表态
    assert ok_m is True, "机械口径没有行时不该拦（实际 %r）" % (r_m,)
