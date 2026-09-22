"""垂直运输机械（塔吊 / 施工电梯）配员登记 + 选行安全的回归测试。

覆盖点（对应用户指令「直接添加塔吊和施工电梯到机械表中，并记得添加 crew」）：
1) `kb.crew_for_machine('塔吊')` / `('施工电梯')` 真的返回 crew（不是 None），
   且 `crew_composition` 能解析成工种人数；
2) 模糊匹配（KB 机械名常带规格后缀，如「塔吊QTZ80」「施工电梯SC200/200」）；
3) `crew_bind` 节点端到端：带 `machine_name='塔吊'` 的叶子补上 machine_crew 且**不报警告**
   （改动前这里必然是 "无配员数据" 警告）；
4) **诚实性锁**：新增行的来源必须是 `user_directive` / `LOW`，
   **绝不**出现规范来源（GD_2018_* / LD_T72）或 HIGH 置信度 —— 防止有人顺手"美化"；
5) **占位诚实锁（判据 2026-09-20 变更）**：塔吊/施工电梯的台班占位行与主控机械行
   一律**如实标注**为 `SCAFFOLD_V1` / `scaffold_placeholder` / `LOW`，
   **禁止冒充规范来源**（详见两条锁的 docstring）。

只依赖随仓库附带的 BuildPlan_KB/kb.db，不联网、不调用 LLM。
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import kb
from pipeline.nodes import norm_bind as NB
from pipeline.nodes.crew_bind import CrewBindNode, parse_crew_composition

# 本子代理按用户直接指令写入的行（见 backend/tools/add_vertical_transport_equipment.py）
USER_DIRECTIVE_MACHINES = {
    "塔吊": {"crew_size": 2, "crew": {"司机": 1, "信号工": 1}},
    "施工电梯": {"crew_size": 1, "crew": {"司机": 1}},
}
REAL_SOURCE_TYPES = {"LD_T72", "guangdong_2018"}

# 垂直运输机械名的匹配键（两条「占位诚实锁」共用）
VERTICAL_TRANSPORT_MACHINE_KEYS = ("塔吊", "塔式起重机", "施工电梯", "人货梯")
# 占位行**禁止**冒充的来源标记（真规范 / 地区定额）
FORBIDDEN_NORM_SOURCE_MARKERS = ("GD_2018", "LD_T72", "regional_quota",
                                 "guangdong_2018")


# ==================== 1) crew 真的取得到 ====================
@pytest.mark.parametrize("machine", sorted(USER_DIRECTIVE_MACHINES))
def test_vertical_transport_machine_has_crew(machine):
    row = kb.crew_for_machine(machine)
    assert row is not None, "用户指令要求配 crew：%s 不应返回 None" % machine
    assert row["machine_name"] == machine
    assert row["crew_size"] == USER_DIRECTIVE_MACHINES[machine]["crew_size"]
    crew = parse_crew_composition(row["composition"])
    assert crew == USER_DIRECTIVE_MACHINES[machine]["crew"], row["composition"]
    assert sum(crew.values()) == row["crew_size"], "composition 人数与 default_crew_size 必须一致"


@pytest.mark.parametrize("probe,expected", [
    ("塔吊QTZ80", "塔吊"),
    ("施工电梯SC200/200", "施工电梯"),
])
def test_vertical_transport_fuzzy_match(probe, expected):
    """KB 机械名常带规格后缀（`crew_for_machine` 的 LIKE 兜底通道）。"""
    row = kb.crew_for_machine(probe)
    assert row is not None, probe
    assert row["machine_name"] == expected


# ==================== 2) crew_bind 端到端 ====================
@pytest.mark.parametrize("machine", sorted(USER_DIRECTIVE_MACHINES))
def test_crew_bind_end_to_end_no_warning(machine):
    """带 machine_name 的机械叶子 → machine_crew 非空、crew_warnings 为空。"""
    leaf = {
        "id": "VT1", "name": "%s作业" % machine, "kb_activity_id": "",
        "unit": "台", "quantity": 1,
        "norm_binding": {"task_id": "VT1", "mode": "machine", "norm_value": 0.05,
                         "quantity_basis": 1.0, "source_code": "AI_ESTIMATE_V1",
                         "match_type": "ai", "machine_name": machine},
    }
    wbs = {"phases": [{"phase": "P", "work_packages": [
        {"id": "W", "name": "W", "sub_packages": [leaf]}]}]}
    out = CrewBindNode().run({"wbs": wbs})

    assert leaf["machine_crew"] == USER_DIRECTIVE_MACHINES[machine]["crew"]
    assert out["crew_warnings"] == [], "配员表已覆盖，不该再有『无配员数据』警告"
    assert out["crew_stats"]["machines_total"] == 1
    assert out["crew_stats"]["machines_with_crew"] == 1
    # 来源必须原样透出「用户指令 + LOW」，不许被美化
    src = leaf["crew_source"]
    assert src["origin"] == "kb"
    assert src["ref"] == "user_directive"
    assert src["confidence"] == "LOW"


# ==================== 3) 诚实性锁 ====================
@pytest.mark.parametrize("machine", sorted(USER_DIRECTIVE_MACHINES))
def test_source_is_honest_not_regulation(machine):
    """新增行不许冒充规范来源：只能是 user_directive / LOW。"""
    row = kb.crew_for_machine(machine)
    assert row is not None
    assert row["source_type"] == "user_directive", row["source_type"]
    assert row["confidence"] == "LOW", row["confidence"]
    assert row["source_type"] not in REAL_SOURCE_TYPES


def test_no_fake_machinery_norm_rows():
    """占位台班行必须**如实标注**，绝不许冒充规范来源（判据 2026-09-20 变更）。

    `[用户 2026-09-20 裁定：保留占位但必须全面如实标注]`

    前提变了：**不是"真实规范已导入"**，而是用户裁定允许保留 `SCAFFOLD_V1` 类别占位行
    （`NE_SCAFFOLD_0001~0006`）。旧判据断言这些机械在 `Norm_Equipment_Table` 里
    **零行**；新判据**更严** —— 不再禁止"占位行存在"，而是禁止**占位行冒充规范**：

      ① 凡是 `machine_combination_json` 含 塔吊/塔式起重机/施工电梯/人货梯 的行，
         **每一行都必须**同时满足 `source_code='SCAFFOLD_V1'` /
         `source_type='scaffold_placeholder'` / `status='needs_review'`；
      ② 任何一行都不许声称 GD_2018_* / LD_T72* / `regional_quota` 等规范来源。

    旧判据只问"行在不在"；新判据问"这行的标签诚不诚实" —— 将来有人把占位值
    贴上规范标签（或反过来），这条都会红。
    """
    import sqlite3
    from pipeline import config
    con = sqlite3.connect(str(config.KB_DB_PATH))
    try:
        rows = {}
        for key in VERTICAL_TRANSPORT_MACHINE_KEYS:
            for r in con.execute(
                    "SELECT norm_id, activity_id, IFNULL(source_code,''), "
                    "IFNULL(source_type,''), IFNULL(status,''), "
                    "IFNULL(machine_combination_json,'') FROM Norm_Equipment_Table "
                    "WHERE machine_combination_json LIKE ?", ("%" + key + "%",)):
                rows[r[0]] = r
        for norm_id, aid, scode, stype, status, mcomb in rows.values():
            assert scode == "SCAFFOLD_V1", (
                "台班行 %s（活动 %s，机械 %s）含垂直运输机械却声称来源 %r —— "
                "占位行必须如实标注 source_code='SCAFFOLD_V1'，不许冒充规范来源"
                % (norm_id, aid, mcomb, scode))
            assert stype == "scaffold_placeholder", (
                "台班行 %s（活动 %s）的 source_type=%r —— 必须如实标注为 "
                "'scaffold_placeholder'" % (norm_id, aid, stype))
            assert status == "needs_review", (
                "台班行 %s（活动 %s）的 status=%r —— 占位行必须停在 "
                "'needs_review'（待人工复核/清退），不许伪装成已核验"
                % (norm_id, aid, status))
            for bad in FORBIDDEN_NORM_SOURCE_MARKERS:
                blob = "%s %s" % (scode, stype)
                assert bad.upper() not in blob.upper(), (
                    "台班行 %s（活动 %s）含垂直运输机械却带规范来源标记 %r —— "
                    "禁止占位行冒充规范（source_code=%r source_type=%r）"
                    % (norm_id, aid, bad, scode, stype))
    finally:
        con.close()


def test_no_main_machine_row_for_vertical_transport():
    """主控机械行必须**如实标注**，且不得挤掉真规范机械（判据 2026-09-20 变更）。

    `[用户 2026-09-20 裁定：保留占位但必须全面如实标注]`

    前提同样变了：**不是"真实规范已导入"**。旧判据断言这些机械在
    `Activity_Main_Machine` 里**零行**；新判据三条，**比旧判据更严** ——
    旧判据只禁止"行存在"，新判据还禁止"标签冒充规范"：

      (i) 凡 `machine_name` 含 塔吊/塔式起重机/施工电梯/人货梯 的主控机械行，
          `source_type` 必须是 `'scaffold_placeholder'` 且 `confidence` 必须是
          `'LOW'` —— 这是"如实标注"的锁：将来谁再写回 `regional_quota`/`HIGH` 就会红；
      (ii) **保护真规范**：这些机械**不许**出现在"该活动在 `Norm_Equipment_Table`
          里有非 SCAFFOLD 台班行"的活动上 —— 这才是原测试真正要防的
          "一条塔吊主控机械行挤掉 CONC_NEW_* 泵车定额"风险
          （见 `norm_bind._bind_machine` 取"第一条有名字的主控机械行"）；
      (iii) 对照组 `test_pump_truck_norm_still_selected`（同文件）保留不动。
    """
    import sqlite3
    from pipeline import config
    con = sqlite3.connect(str(config.KB_DB_PATH))
    try:
        where = " OR ".join("machine_name LIKE ?" for _ in
                            VERTICAL_TRANSPORT_MACHINE_KEYS)
        rows = con.execute(
            "SELECT activity_id, IFNULL(condition_text,''), machine_name, "
            "IFNULL(source_type,''), IFNULL(confidence,'') FROM Activity_Main_Machine "
            "WHERE " + where,
            tuple("%" + k + "%" for k in VERTICAL_TRANSPORT_MACHINE_KEYS)).fetchall()
        # (i) 如实标注锁
        for aid, ct, mn, stype, conf in rows:
            assert stype == "scaffold_placeholder", (
                "活动 %s（条件 %r）的主控机械「%s」source_type=%r —— 它的定额依据只有 "
                "SCAFFOLD_V1 占位行，必须如实标注 'scaffold_placeholder'，"
                "不许冒充规范台班" % (aid, ct, mn, stype))
            assert conf == "LOW", (
                "活动 %s（条件 %r）的主控机械「%s」confidence=%r —— 占位依据只能是 "
                "'LOW'" % (aid, ct, mn, conf))
        # (ii) 真规范保护锁：不许出现在有非 SCAFFOLD 台班行的活动上
        for aid, ct, mn, _stype, _conf in rows:
            real = con.execute(
                "SELECT COUNT(*) FROM Norm_Equipment_Table WHERE activity_id=? "
                "AND IFNULL(source_code,'') NOT LIKE 'SCAFFOLD%' "
                "AND IFNULL(source_type,'')<>'scaffold_placeholder'",
                (aid,)).fetchone()[0]
            assert real == 0, (
                "活动 %s（条件 %r）有 %d 条非 SCAFFOLD 的台班定额行，却给垂直运输机械"
                "「%s」加了主控机械行 —— 会挤掉真规范机械的定额（例如 CONC_NEW_FOUND "
                "的混凝土输送泵车）" % (aid, ct, real, mn))
    finally:
        con.close()


# ==================== 4) 对照组：泵车定额没被挤掉 ====================
def test_pump_truck_norm_still_selected():
    """CONC_NEW_FOUND 的主控机械仍是泵车，定额仍是 NE_CONC_002 0.055 台班/10m³。"""
    machines = kb.main_machine("CONC_NEW_FOUND")
    assert machines, "CONC_NEW_FOUND 必须有主控机械行"
    assert machines[0]["machine_name"] == "混凝土输送泵车", machines

    rows = kb.equipment_norms("CONC_NEW_FOUND")
    row, idx, hit = NB._pick_machine_row(rows, "混凝土输送泵车", "", "")
    assert row is not None, "泵车定额行必须能选中"
    norm, unit, basis, name = NB._machine_norm_at(row, idx)
    assert (norm, basis, name) == (0.055, 10.0, "混凝土输送泵车"), (norm, basis, name)

    # 塔吊在多机械台班表里零命中 —— 这正是"不借同行机械"的依据
    assert NB._pick_machine_row(rows, "塔吊", "", "") == (None, None, "")
