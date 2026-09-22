# -*- coding: utf-8 -*-
"""回归：审计层（`pipeline/audit_scope.py`）—— 三处"可疑却被静默使用"的事实必须可见。

背景（第 41 轮，均在本仓库真实数据上实测）：
  一份计划的工期是按一串**没人复核过**的口径排出来的，其中三处最要紧：

   ① **工作面人数上限有三套**（`Workface_Capacity_Rule`，487 行 = 原 478 行 +
      终版修改 C1–C5 新增 9 条标定行）：
      `legacy_max_labor`（旧上限，**至今仍被写进计划**的 `max_labor`）4~16、
      `crew_max`（v2 标定）3~16、`effective_crew_max = max(crew_max, min(40,
      ceil(crew_base×2.5)))`（排程**实际**用的）。实测 **387 行** `legacy < crew_max`，
      其中 **385 行**再被公式抬高一次；`review_state` 全 `pending`、置信度全 `LOW`。
   ② **同一个 source_code 给出差 1.79 倍的值**：`LD_T72_7_2008` 在 `4.1.1.1`
      （钢筋绑扎 REBAR_NEW_FOUND）是 4.43 工日/t，在 `5.1.1.1`（REBAR_NEW_SLAB）
      是 7.91 工日/t。工期直接按它算。
   ③ **同一活动 + 同一楼层同时按面积与体积各排一条**：实测 18 组
      （`6.1.N.1` 1420 m² / `6.1.N.3` 284 m³，换算厚度 0.2 m）。

本文件锁四件事：
  · 三个函数的**判据**（含"宁可漏、不可误报"的那几条守卫）；
  · 拿不到 KB 时**明说** `no_db`/`no_table`，不许用空清单冒充"没问题"；
  · `build_meta` / `build_parts` 真的把 `scope_audit` 落进 meta，且契约往返不丢；
  · **只读**：审计层跑一遍，kb.db 的字节与修改时间都不许变。

运行：python -m pytest backend/tests/test_audit_scope.py -q
"""

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from pipeline import audit_scope as A                              # noqa: E402
from pipeline import config                                       # noqa: E402
from pipeline import schemas                                      # noqa: E402
from pipeline.nodes import plan_assembler as PA                   # noqa: E402

REAL_PLAN = BACKEND / "plans" / "plan_sample3_after_fix.json"
REAL_KB = Path(config.KB_DB_PATH)

#: 测试临时区：**普通 mkdir**（`tests/conftest.py` 第 9-10 行写明：本机沙箱下
#: `tmp_path` / `tempfile.mkdtemp()` 建出来的目录随后访问会被拒绝）。
TMP = BACKEND / "_test_tmp"


def _tmpdir(name):
    # ⚠️ 名字里必须带 pid：这些目录是**跨用例复用**的（`_make_kb` 会先 unlink 同名文件），
    # 不带 pid 时 pytest-xdist 的多个 worker 会共用同一个 `audit_scope_<name>`，
    # 互相删掉对方正在用的 mini kb → FileNotFoundError / 读到别人的行。
    path = TMP / ("audit_scope_p%d_%s" % (os.getpid(), name))
    path.mkdir(parents=True, exist_ok=True)
    return path


# ══════════════════════ 合成 KB：与真库同构（列名一致） ══════════════════════
_KB_COLUMNS = (
    "rule_id", "activity_id", "work_type_l3", "quantity_unit", "unit_basis",
    "crew_base", "crew_min", "crew_max", "legacy_max_labor", "legacy_max_machine",
    "source_type", "confidence", "review_state", "model_version", "notes",
)


def _make_kb(path, rows):
    """建一张迷你 `Workface_Capacity_Rule`（列名与真库一致，值由调用方给）。"""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE Workface_Capacity_Rule (%s)"
            % ", ".join("%s" % c for c in _KB_COLUMNS))
        conn.executemany(
            "INSERT INTO Workface_Capacity_Rule (%s) VALUES (%s)"
            % (", ".join(_KB_COLUMNS), ", ".join("?" * len(_KB_COLUMNS))),
            [tuple(r.get(c) for c in _KB_COLUMNS) for r in rows])
        conn.commit()
    finally:
        conn.close()
    return path


class TestCmaxThreeCeilings:
    """① 三套上限并排列出 —— 排程用的那个（effective）必须自己算，不许抄。"""

    def test_分档把三个上限并排列出并给出实际生效值(self):
        db = _make_kb(_tmpdir("kb1") / "mini.db", [
            # base=8 → min(40, ceil(20)) = 20 > crew_max=15 > legacy=12
            {"rule_id": "R1", "activity_id": "A1", "work_type_l3": "masonry",
             "quantity_unit": "m³", "crew_base": 8, "crew_min": 4, "crew_max": 15,
             "legacy_max_labor": 12, "review_state": "pending",
             "confidence": "LOW", "source_type": "ai_estimate"},
            {"rule_id": "R2", "activity_id": "A2", "work_type_l3": "masonry",
             "quantity_unit": "m³", "crew_base": 8, "crew_min": 4, "crew_max": 15,
             "legacy_max_labor": 12, "review_state": "pending",
             "confidence": "LOW", "source_type": "ai_estimate"},
        ])
        res = A.cmax_review_rows(db_path=str(db), min_rows=1)
        assert res["status"] == "ok"
        assert res["total_rows"] == 2
        assert res["selected_rows"] == 2
        assert res["legacy_below_crew_max"] == 2
        assert res["legacy_above_crew_max"] == 0
        assert res["null_ceiling_rows"] == 0
        assert res["review_state"] == [{"value": "pending", "rows": 2}]
        assert len(res["bands"]) == 1
        band = res["bands"][0]
        assert band["legacy_max_labor"] == 12          # 旧上限（写进计划的 max_labor）
        assert band["crew_max"] == 15                  # v2 标定上限
        # C8-7（2026-09-21）：`effective_crew_max`（×2.5 带）**已删** → 该字段恒为 None，
        # 与之绑定的 `lift_over_legacy` 同样为 None（不再有任何"被抬高过"的口径）。
        assert band["effective_crew_max"] is None
        assert band["lift_over_legacy"] is None
        assert band["rows"] == 2
        assert band["sample_rule_ids"] == ["R1", "R2"]

    def test_实际生效上限字段已随口径删除(self):
        """`effective_crew_max` 一列**恒为 None**：`scheduler.effective_crew_max` 已删。"""
        from pipeline.nodes import scheduler as S
        assert not hasattr(S, "effective_crew_max"), "×2.5 带函数必须已删"
        db = _make_kb(_tmpdir("kb2") / "mini.db", [
            {"rule_id": "R%d" % i, "activity_id": "A%d" % i, "crew_base": base,
             "crew_min": 1, "crew_max": cmax, "legacy_max_labor": legacy}
            for i, (base, cmax, legacy) in enumerate(
                [(8, 15, 4), (12, 15, 12), (4, 4, 4), (3, 3, 3), (100, 15, 4)])])
        res = A.cmax_review_rows(db_path=str(db), min_rows=1)
        assert res["bands"], "min_rows=1 时每个分歧档都要列出来"
        assert all(b["effective_crew_max"] is None for b in res["bands"])
        assert all(b["lift_over_legacy"] is None for b in res["bands"])
        assert "已删除" in res["note"] or "恒为 None" in res["note"]

    def test_旧上限不低于新上限的行不进清单(self):
        """`legacy >= crew_max` 没有这处分歧 → 既不计入 selected，也不进档位。"""
        db = _make_kb(_tmpdir("kb3") / "mini.db", [
            {"rule_id": "R1", "activity_id": "A1", "crew_base": 8,
             "crew_max": 16, "legacy_max_labor": 16},          # 相等 → 不进
            {"rule_id": "R2", "activity_id": "A2", "crew_base": 8,
             "crew_max": 16, "legacy_max_labor": 20},          # 反向 → 只计数
            {"rule_id": "R3", "activity_id": "A3", "crew_base": 8,
             "crew_max": 16, "legacy_max_labor": 8},           # 正向 → 进
        ])
        res = A.cmax_review_rows(db_path=str(db), min_rows=1)
        assert res["selected_rows"] == 2
        assert res["legacy_below_crew_max"] == 1
        assert res["legacy_above_crew_max"] == 1
        # C8-7 后可见性只看 min_rows（`lift` 口径已删）→ min_rows=1 时两个分歧档都列出：
        # R3（legacy 8 < crew_max 16，正向）与 R2（legacy 20 > crew_max 16，反向）。
        assert {b["legacy_max_labor"] for b in res["bands"]} == {8.0, 20.0}

    def test_任一端为空只计数不编数(self):
        """`crew_max` 或 `legacy_max_labor` 为空 → `null_ceiling_rows`，**不许**当 0 处理。"""
        db = _make_kb(_tmpdir("kb4") / "mini.db", [
            {"rule_id": "R1", "activity_id": "A1", "crew_base": 8,
             "crew_max": None, "legacy_max_labor": 8},
            {"rule_id": "R2", "activity_id": "A2", "crew_base": 8,
             "crew_max": 16, "legacy_max_labor": None},
            {"rule_id": "R3", "activity_id": "A3", "crew_base": 8,
             "crew_max": 16, "legacy_max_labor": 8},
        ])
        res = A.cmax_review_rows(db_path=str(db), min_rows=1)
        assert res["null_ceiling_rows"] == 2
        assert res["selected_rows"] == 1
        assert len(res["bands"]) == 1

    def test_阈值让量小又不显眼的档不刷屏(self):
        """小于 `min_rows` 且抬高幅度也小的档不列 —— 但仍在 selected_rows 里。"""
        db = _make_kb(_tmpdir("kb5") / "mini.db", [
            {"rule_id": "R1", "activity_id": "A1", "crew_base": 5,
             "crew_min": 1, "crew_max": 8, "legacy_max_labor": 6},   # 抬 13-6=7 → 显眼
            {"rule_id": "R2", "activity_id": "A2", "crew_base": 4,
             "crew_min": 1, "crew_max": 5, "legacy_max_labor": 4},   # 抬 10-4=6 → 显眼
            {"rule_id": "R3", "activity_id": "A3", "crew_base": 4,
             "crew_min": 1, "crew_max": 7, "legacy_max_labor": 6},   # 只 1 行、抬 10-6=4
        ])
        res = A.cmax_review_rows(db_path=str(db), min_rows=1, min_lift=5)
        assert res["selected_rows"] == 3
        # C8-7 后可见性只看 min_rows（`lift` 口径已删）→ min_rows=1 时三档都列出
        assert {b["legacy_max_labor"] for b in res["bands"]} == {6.0, 4.0}
        assert all(b["rows"] >= 1 for b in res["bands"])

    def test_库不存在时明说no_db而不是空清单冒充没问题(self):
        res = A.cmax_review_rows(db_path=str(_tmpdir("kb6") / "没有这个库.db"))
        assert res["status"] == "no_db"
        assert res["selected_rows"] == 0 and res["bands"] == []
        assert "不代表" in res["note"]

    def test_表缺失时明说no_table(self):
        path = _tmpdir("kb7") / "empty.db"
        if path.exists():
            path.unlink()
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE 别的表 (x INTEGER)")
        conn.commit()
        conn.close()
        res = A.cmax_review_rows(db_path=str(path))
        assert res["status"] == "no_table"
        assert "不代表" in res["note"]

    def test_可以传自己的连接(self):
        db = _make_kb(_tmpdir("kb8") / "mini.db", [
            {"rule_id": "R1", "activity_id": "A1", "crew_base": 8,
             "crew_max": 15, "legacy_max_labor": 12}])
        conn = sqlite3.connect(str(db))
        try:
            first = A.cmax_review_rows(conn=conn)
            second = A.cmax_review_rows(conn=conn)     # 传入的连接**不许**被关掉
            assert first["selected_rows"] == second["selected_rows"] == 1
        finally:
            conn.close()

    def test_默认阈值下的结果会按库文件指纹缓存(self):
        """同一张没变过的库不重复读；**库文件一变（mtime/size）缓存就作废**。"""
        A.clear_cache()
        db = _make_kb(_tmpdir("kb10") / "mini.db", [
            {"rule_id": "R1", "activity_id": "A1", "crew_base": 8,
             "crew_max": 15, "legacy_max_labor": 12}])
        old = config.KB_DB_PATH
        config.KB_DB_PATH = db                       # 只改配置里的路径，不动真库
        try:
            first = A.cmax_review_rows()
            assert first["selected_rows"] == 1
            second = A.cmax_review_rows()
            assert second == first, "同一份没变过的库应当直接命中缓存"
            assert second is not first, "缓存的快照不许与调用方共享可变对象"
            second["bands"].clear()                  # 就地改不许污染下一次
            assert A.cmax_review_rows()["selected_rows"] == 1
            # 库被改过（多一行）→ 指纹变化 → 必须重新读，不许吐旧结果
            _make_kb(db, [
                {"rule_id": "R1", "activity_id": "A1", "crew_base": 8,
                 "crew_max": 15, "legacy_max_labor": 12},
                {"rule_id": "R2", "activity_id": "A2", "crew_base": 8,
                 "crew_max": 15, "legacy_max_labor": 12}])
            third = A.cmax_review_rows()
            assert third["selected_rows"] == 2
        finally:
            config.KB_DB_PATH = old
            A.clear_cache()

    def test_非默认阈值不读缓存(self):
        A.clear_cache()
        db = _make_kb(_tmpdir("kb11") / "mini.db", [
            {"rule_id": "R1", "activity_id": "A1", "crew_base": 8,
             "crew_max": 15, "legacy_max_labor": 12}])
        config_before = config.KB_DB_PATH
        config.KB_DB_PATH = db
        try:
            assert A.cmax_review_rows(min_rows=1)["selected_rows"] == 1
            assert A.cmax_review_rows(min_rows=99)["selected_rows"] == 1
        finally:
            config.KB_DB_PATH = config_before
            A.clear_cache()


class TestCmaxOnRealKb:
    """真库实测数（库不在就跳过，不把测试绑死在本机数据上）。"""

    @pytest.mark.skipif(not REAL_KB.exists(), reason="本机没有 BuildPlan_KB/kb.db")
    def test_真实库已无容量表时审计报no_table(self):
        """域 1.6（第 6 批）已删 `Workface_Capacity_Rule` → 真实库上审计**明说** `no_table`。

        生产端（`pipeline/audit_scope.py` 的 `CMAX_TABLE` 分支）故意在表缺失/查询失败时
        返回 `status="no_table"` + 空清单 + "不代表没有分歧"的 note —— 拿空清单冒充
        "没问题"正是不许出现的行为。原先这里断的是 387 行 / 26 档的旧表统计，
        随表一并退役，故改写为对新行为（`no_table`）的**确定性**断言。
        """
        res = A.cmax_review_rows()
        assert res["status"] == "no_table"
        assert res["table"] == "Workface_Capacity_Rule"
        assert res["total_rows"] == 0
        assert res["selected_rows"] == 0
        assert res["distinct_bands"] == 0
        assert res["bands"] == []
        assert "不代表" in res["note"], res["note"]

    @pytest.mark.skipif(not REAL_KB.exists(), reason="本机没有 BuildPlan_KB/kb.db")
    def test_审计层跑一遍不改kb一个字节(self):
        before = (REAL_KB.stat().st_size, REAL_KB.stat().st_mtime_ns,
                  hashlib.md5(REAL_KB.read_bytes()).hexdigest())
        A.cmax_review_rows()
        A.cmax_review_rows(min_rows=1, min_lift=0)
        after = (REAL_KB.stat().st_size, REAL_KB.stat().st_mtime_ns,
                 hashlib.md5(REAL_KB.read_bytes()).hexdigest())
        assert before == after, "审计层必须是只读的（mode=ro）"


# ══════════════════════ ② 定额离散 ══════════════════════
def _leaf(leaf_id, activity, name, quantity, unit, source_code, norm_value,
          location=None, unit_of_norm=None, condition=None):
    return {
        "id": leaf_id, "name": name, "kb_activity_id": activity,
        "quantity": quantity, "unit": unit, "location": location or "",
        "duration_days": 1,
        "norm_binding": {"source_code": source_code, "norm_value": norm_value,
                         "unit": unit_of_norm or ("工日/" + unit),
                         "condition_text": condition or ""},
    }


class TestNormSpread:
    """② 同一个 source_code 给出明显不同的值 —— 宁可漏，不可误报。"""

    REBAR = [
        _leaf("4.1.1.1", "REBAR_NEW_FOUND", "1-0.5层 钢筋绑扎", 21.0, "t",
              "LD_T72_7_2008", 4.43, "1-0.5层", "工日/t", "≤20"),
        _leaf("5.1.1.1", "REBAR_NEW_SLAB", "1-1层 钢筋绑扎", 38.0, "t",
              "LD_T72_7_2008", 7.91, "1-1层", "工日/t", "预制·直径≤10mm"),
    ]

    def test_命中同来源比值179倍的那一组(self):
        groups = A.norm_row_spread(self.REBAR)
        assert len(groups) == 1
        group = groups[0]
        assert group["source_code"] == "LD_T72_7_2008"
        assert group["unit"] == "工日/t"
        assert group["min_value"] == 4.43 and group["max_value"] == 7.91
        assert group["ratio"] == 1.7856
        assert group["distinct_activities"] == 2
        assert group["shared_tokens"] == ["钢筋绑扎"]
        assert group["value_counts"] == [{"norm_value": 4.43, "tasks": 1},
                                         {"norm_value": 7.91, "tasks": 1}]
        assert {s["id"] for s in group["samples"]} == {"4.1.1.1", "5.1.1.1"}

    def test_同一个activity的不同值是正常的_不算离散(self):
        """实测 `LD_T72_4_2008` 工日/m³ = 0.943（ALC 板）vs 0.85（砌块）——
        同一个 L4 按不同条件取不同值，是**正常**的，不许当"同一个值漂移"报出来。"""
        rows = [
            _leaf("6.1.1.1", "LDT724_砌块墙", "1-1层 ALC墙板安装", 1420.0, "m²",
                  "LD_T72_4_2008", 0.943, "1-1层", "工日/m³"),
            _leaf("6.1.1.3", "LDT724_砌块墙", "1-1层 砌块墙", 284.0, "m³",
                  "LD_T72_4_2008", 0.85, "1-1层", "工日/m³"),
        ]
        assert A.norm_row_spread(rows) == []

    def test_工序名不沾边的不算(self):
        """实测 `AI_ESTIMATE_V1`：34 条、10 个不同值、比值 41.67，但逐条都是 AI 估算、
        工序名互不相干 → 不是"同一个值漂移"，不报。"""
        rows = [
            _leaf("1.1", "AI_1", "1-1层 土方开挖", 10.0, "m²", "AI_ESTIMATE_V1", 0.012),
            _leaf("2.1", "AI_2", "1-1层 外檐保温", 10.0, "m²", "AI_ESTIMATE_V1", 0.5),
        ]
        assert A.norm_row_spread(rows) == []

    def test_比值不够大不报(self):
        rows = [
            _leaf("1.1", "A1", "1层 钢筋绑扎", 10.0, "t", "S1", 4.0),
            _leaf("2.1", "A2", "2层 钢筋绑扎", 10.0, "t", "S1", 5.0),   # 1.25 < 1.5
        ]
        assert A.norm_row_spread(rows) == []

    def test_单位写法不同先归一(self):
        """`M2` / `m^2` / `㎡` 是同一个量纲，不许因为写法不同就分组漏掉。"""
        rows = [
            _leaf("1.1", "A1", "1层 钢筋绑扎", 10.0, "t", "S1", 4.0, None, "工日/M2"),
            _leaf("2.1", "A2", "2层 钢筋绑扎", 10.0, "t", "S1", 8.0, None, "工日/㎡"),
        ]
        groups = A.norm_row_spread(rows)
        assert len(groups) == 1 and groups[0]["unit"] == "工日/m²"

    def test_没有清单时返回空而不是抛(self):
        for bad in ([], None, [None, 1, "x"], [{}],
                    [{"id": "1", "norm_binding": None}],
                    [{"id": "1", "norm_binding": {"source_code": "S", "norm_value": None}}]):
            assert A.norm_row_spread(bad) == []

    @pytest.mark.skipif(not REAL_PLAN.exists(), reason="本机没有示例计划")
    def test_真实计划上只命中那一组(self):
        plan = json.loads(REAL_PLAN.read_text(encoding="utf-8"))
        leaves = A.leaf_tasks(plan)
        assert len(leaves) == 333
        groups = A.norm_row_spread(leaves)
        assert [g["source_code"] for g in groups] == ["LD_T72_7_2008"]
        assert groups[0]["ratio"] == 1.7856
        assert groups[0]["tasks"] == 22


# ══════════════════════ ③ 重复范围 ══════════════════════
class TestDuplicateScope:
    """③ 同活动 + 同楼层、面积与体积各排一条 —— 必须过"墙厚换得出来"这一关。"""

    WALLS = [
        _leaf("6.1.1.1", "LDT724_砌块墙", "1-1层 ALC墙板安装", 1420.0, "m²",
              "LD_T72_4_2008", 0.943, "1-1层", "工日/m³"),
        _leaf("6.1.1.3", "LDT724_砌块墙", "1-1层 砌块墙", 284.0, "m³",
              "LD_T72_4_2008", 0.85, "1-1层", "工日/m³"),
    ]

    def test_换算得出200mm墙厚就算一组(self):
        groups = A.duplicate_scope_groups(self.WALLS)
        assert len(groups) == 1
        group = groups[0]
        assert group["kb_activity_id"] == "LDT724_砌块墙"
        assert group["location"] == "1-1层"
        assert group["thickness_m"] == 0.2
        assert group["consistent_thickness"] is True
        assert group["ids"] == ["6.1.1.1", "6.1.1.3"]
        assert group["pairs"] == [{"area_id": "6.1.1.1", "volume_id": "6.1.1.3",
                                   "thickness_m": 0.2}]

    def test_同名活动复用在不同工序上不算重复(self):
        """实测 `SPREP_AI_003`（场地平整 3200 m² + 场地硬化 1200 m²）这类：
        单位族里没有"面积↔体积"配对 → 不算重复，避免 34 组误报。"""
        rows = [
            _leaf("1.1", "SPREP_AI_003", "场地平整", 3200.0, "m²", "S", 0.1, "全场"),
            _leaf("1.2", "SPREP_AI_003", "场地硬化", 1200.0, "m²", "S", 0.2, "全场"),
        ]
        assert A.duplicate_scope_groups(rows) == []

    def test_厚度不合理不算(self):
        """体积÷面积 = 3.2 m（不是墙）→ 不是同一批墙。"""
        rows = [
            _leaf("1.1", "A", "1层 ALC墙板安装", 100.0, "m²", "S", 1.0, "1层"),
            _leaf("1.3", "A", "1层 砌块墙", 320.0, "m³", "S", 1.0, "1层"),
        ]
        assert A.duplicate_scope_groups(rows) == []

    def test_楼层不同不算同范围(self):
        rows = [
            _leaf("1.1", "A", "1层 ALC墙板安装", 100.0, "m²", "S", 1.0, "1层"),
            _leaf("2.3", "A", "2层 砌块墙", 20.0, "m³", "S", 1.0, "2层"),
        ]
        assert A.duplicate_scope_groups(rows) == []

    def test_缺活动或缺楼层不参与分组(self):
        rows = [
            _leaf("1.1", "", "ALC墙板安装", 100.0, "m²", "S", 1.0, "1层"),
            _leaf("1.3", "", "砌块墙", 20.0, "m³", "S", 1.0, "1层"),
            _leaf("2.1", "A", "ALC墙板安装", 100.0, "m²", "S", 1.0, ""),
            _leaf("2.3", "A", "砌块墙", 20.0, "m³", "S", 1.0, ""),
        ]
        assert A.duplicate_scope_groups(rows) == []

    def test_数量缺失或非正不参与(self):
        rows = [
            _leaf("1.1", "A", "1层 ALC墙板安装", None, "m²", "S", 1.0, "1层"),
            _leaf("1.2", "A", "1层 ALC墙板安装", 0, "m²", "S", 1.0, "1层"),
            _leaf("1.3", "A", "1层 砌块墙", 20.0, "m³", "S", 1.0, "1层"),
        ]
        assert A.duplicate_scope_groups(rows) == []

    def test_没有清单时返回空而不是抛(self):
        for bad in ([], None, [None, "x"], [{}]):
            assert A.duplicate_scope_groups(bad) == []

    @pytest.mark.skipif(not REAL_PLAN.exists(), reason="本机没有示例计划")
    def test_真实计划上18组全是200mm厚(self):
        plan = json.loads(REAL_PLAN.read_text(encoding="utf-8"))
        groups = A.duplicate_scope_groups(A.leaf_tasks(plan))
        assert len(groups) == 18
        assert {g["kb_activity_id"] for g in groups} == {"LDT724_砌块墙"}
        assert {g["thickness_m"] for g in groups} == {0.2}
        assert {g["location"] for g in groups} == {
            "%d-%d层" % (n, n) for n in range(1, 19)}
        assert all(g["ids"][1].endswith(".3") for g in groups)


# ══════════════════════ ④ 汇总裁剪 + 落进 meta ══════════════════════
class TestScopeAuditWiring:
    def test_入参可以是plan或wbs或叶子清单(self):
        plan = {"wbs": {"phases": [{"work_packages": [
            {"sub_packages": [{"id": "1", "unit": "m²", "quantity": 1}]}]}]}}
        assert A.leaf_tasks(plan) == [{"id": "1", "unit": "m²", "quantity": 1}]
        assert A.leaf_tasks(plan["wbs"]) == A.leaf_tasks(plan)
        assert A.leaf_tasks({}) == []
        assert A.leaf_tasks(None) == []
        assert A.leaf_tasks({"phases": [None, {"work_packages": [None, {}]}]}) == []

    def test_汇总裁剪给出计数与短句(self):
        audit = A.scope_audit({"wbs": {"phases": [{"work_packages": [
            {"sub_packages": self._leaves()}]}]}},
            db_path=str(_tmpdir("kb9") / "无.db"))
        assert audit["leaves"] == len(self._leaves())
        assert audit["cmax_ceiling"]["status"] == "no_db"        # KB 读不到要**明说**
        assert audit["norm_spread"]["count"] == 1
        assert audit["norm_spread"]["ratio_max"] == 1.7856
        assert audit["duplicate_scope"]["count"] == 1
        assert audit["duplicate_scope"]["thicknesses_m"] == [0.2]
        assert audit["duplicate_scope"]["leaves_involved"] == 2
        # KB 读不到时不许假装"没问题"：flags 里必须有一条"未核"
        assert any("未核" in f for f in audit["flags"])
        assert any("定额离散" in f for f in audit["flags"])
        assert any("重复范围" in f for f in audit["flags"])
        assert json.loads(json.dumps(audit, ensure_ascii=False))["leaves"] == audit["leaves"]

    def test_三项可以单独关掉(self):
        audit = A.scope_audit(self._leaves(), cmax=False, norm_spread=False,
                              duplicate_scopes=False)
        assert audit["cmax_ceiling"]["status"] == "skipped"
        assert audit["norm_spread"]["count"] == 0
        assert audit["duplicate_scope"]["count"] == 0
        assert audit["flags"] == []

    def test_空计划不抛且计数为零(self):
        audit = A.scope_audit({}, cmax=False)
        assert audit["leaves"] == 0
        assert audit["norm_spread"]["ratio_max"] is None
        assert audit["duplicate_scope"]["groups"] == []

    def test_摘要文本含三节(self):
        audit = A.scope_audit(self._leaves(), cmax=False)
        text = A.scope_audit_summary(audit)
        assert "审计层" in text and "①" in text and "②" in text and "③" in text
        assert "LD_T72_7_2008" in text

    @pytest.mark.skipif(not REAL_PLAN.exists(), reason="本机没有示例计划")
    def test_真实计划的完整审计(self):
        plan = json.loads(REAL_PLAN.read_text(encoding="utf-8"))
        audit = A.scope_audit(plan)
        assert audit["leaves"] == 333
        assert audit["norm_spread"]["count"] == 1
        assert audit["duplicate_scope"]["count"] == 18
        assert len(audit["flags"]) >= 2
        # 拿不到 KB 的环境里 flags 会少一条"①"，所以只断言 ②③ 一定在
        assert sum(1 for f in audit["flags"] if "定额离散" in f) == 1
        assert sum(1 for f in audit["flags"] if "重复范围" in f) == 1

    def test_build_meta落进scope_audit(self):
        """契约：`meta["scope_audit"]` 必须真的存在（交付物 / 修订链就靠它）。"""
        meta = PA.build_meta({"wbs": {"phases": [{"work_packages": [
            {"sub_packages": self._leaves()}]}]}})
        audit = meta["scope_audit"]
        assert isinstance(audit, dict)
        assert audit["leaves"] == len(self._leaves())
        assert audit["norm_spread"]["count"] == 1
        assert audit["duplicate_scope"]["count"] == 1
        assert isinstance(audit["flags"], list)
        assert "generated_at" in audit and "note" in audit

    def test_build_meta对空ctx也不抛(self):
        meta = PA.build_meta({})
        assert isinstance(meta["scope_audit"], dict)
        assert meta["scope_audit"]["leaves"] == 0

    def test_build_parts算的那一份被build_meta复用(self):
        """同一份计划不许算两遍：build_parts 写进 ctx 的那份要被 build_meta 原样取走。"""
        ctx = {"wbs": {"phases": [{"work_packages": [
            {"sub_packages": self._leaves()}]}]}}
        parts = PA.build_parts(ctx)
        assert "scope_audit" in parts
        assert ctx["scope_audit"] is parts["scope_audit"]
        assert PA.build_meta(ctx)["scope_audit"] is parts["scope_audit"]

    def test_schema往返不丢scope_audit(self):
        """`schemas._Base` 是 extra="allow" —— 没声明过的 meta 键也不许被 pydantic 丢掉。"""
        audit = A.scope_audit([], cmax=False)
        payload = {"meta": {"scope_audit": audit, "audit_status": "未审计"}}
        dumped = schemas.PlanMeta.model_validate(payload["meta"]).model_dump()
        assert dumped["scope_audit"] == audit

    @staticmethod
    def _leaves():
        return [
            _leaf("4.1.1.1", "REBAR_NEW_FOUND", "1-0.5层 钢筋绑扎", 21.0, "t",
                  "LD_T72_7_2008", 4.43, "1-0.5层", "工日/t", "≤20"),
            _leaf("5.1.1.1", "REBAR_NEW_SLAB", "1-1层 钢筋绑扎", 38.0, "t",
                  "LD_T72_7_2008", 7.91, "1-1层", "工日/t", "预制·直径≤10mm"),
            _leaf("6.1.1.1", "LDT724_砌块墙", "1-1层 ALC墙板安装", 1420.0, "m²",
                  "LD_T72_4_2008", 0.943, "1-1层", "工日/m³"),
            _leaf("6.1.1.3", "LDT724_砌块墙", "1-1层 砌块墙", 284.0, "m³",
                  "LD_T72_4_2008", 0.85, "1-1层", "工日/m³"),
        ]


class TestIsPure:
    """审计层的纪律：不写库、不抛、不改值。"""

    def test_三个函数都不抛异常(self):
        weird = [None, {}, [], [None], [{"a": 1}], [{"norm_binding": 5}],
                 [{"unit": None, "quantity": "abc", "location": None,
                   "kb_activity_id": None}]]
        for item in weird:
            A.norm_row_spread(item)
            A.duplicate_scope_groups(item)
            A.scope_audit(item, cmax=False)

    def test_单位归一复用kb_units的同一张表(self):
        from pipeline import kb_units
        for unit in ("m2", "m^2", "㎡", "m³", "㎥", "工日/m³", "", None, "方"):
            assert A._canon_unit(unit) == kb_units.normalize_unit(unit)

    def test_规格词不算共同工序(self):
        """`200mm` / `C30` 是规格不是工序 —— 判"同一道工序"时不能靠它们。"""
        assert A._name_tokens("1-1层 钢筋绑扎 200mm") == {"钢筋绑扎"}
        assert A._name_tokens("") == set()
        assert A._name_tokens(None) == set()
