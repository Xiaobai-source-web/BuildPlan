"""微型标本验收测试：验证 A（细粒度）和 B（粗粒度）的契约与上卷一致性。"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

# 必须在 import pipeline 之前把 backend 加入 sys.path
ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from pipeline import quantity as Q  # noqa: E402
from pipeline import schemas  # noqa: E402

DATA_DIR = BACKEND / "sample_data" / "micro_demo"

_A_PATH = DATA_DIR / "specimen_A_细_工序级.json"
_B_PATH = DATA_DIR / "specimen_B_粗_整栋.json"


# ── fixtures ──────────────────────────────────────────────
def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _count_leaves(wbs: dict) -> int:
    """统计 WBS 叶子节点数（sub_packages 内的条目）。"""
    return sum(
        1
        for ph in wbs.get("phases", [])
        for wp in ph.get("work_packages", [])
        for _ in wp.get("sub_packages", [])
    )


# ── 测试 ──────────────────────────────────────────────────
class TestSpecimenA:
    """标本 A（细粒度：工序级 × 按层）"""

    def setup_method(self):
        self.data = _load(_A_PATH)
        self.wbs = self.data["wbs"]

    def test_契约校验通过(self):
        """1. 能通过 PlanJson.model_validate"""
        schemas.PlanJson.model_validate(self.data)

    def test_叶子数等于estimate_rows_for(self):
        """2. A 的叶子数 == estimate_rows_for(A, COMPONENT, PER_FLOOR)"""
        leaf_count = _count_leaves(self.wbs)
        estimated = Q.estimate_rows_for(self.wbs, Q.DEPTH_COMPONENT, Q.FLOOR_PER_FLOOR)
        assert leaf_count == estimated, (
            f"叶子数 {leaf_count} != estimate_rows_for 返回 {estimated}"
        )

    def test_叶子数大于B(self):
        """5. A 的叶子数 > B 的行数（粒度差异真实存在）"""
        b_data = _load(_B_PATH)
        b_leaf_count = _count_leaves(b_data["wbs"])
        a_leaf_count = _count_leaves(self.wbs)
        assert a_leaf_count > b_leaf_count, (
            f"A 叶子数 {a_leaf_count} 应 > B 叶子数 {b_leaf_count}"
        )

    def test_总工期等于关键路径长度(self):
        """6. overview.total_duration_days == cpm_result.total_duration_days"""
        ov = self.data["overview"]
        cpm = self.data["cpm_result"]
        assert ov["total_duration_days"] == cpm["total_duration_days"], (
            f"overview 工期 {ov['total_duration_days']} != cpm_result 工期 {cpm['total_duration_days']}"
        )

    def test_竣工日期与开工日期一致(self):
        """7. planned_end_date == planned_start_date + total_duration_days"""
        ov = self.data["overview"]
        start = date.fromisoformat(ov["planned_start_date"])
        end = date.fromisoformat(ov["planned_end_date"])
        total = ov["total_duration_days"]
        expected_end = start + timedelta(days=total)
        assert end == expected_end, (
            f"竣工日期 {end} != 开工 {start} + {total} 天 = {expected_end}"
        )


class TestSpecimenB:
    """标本 B（粗粒度：工种级 × 整栋）"""

    def setup_method(self):
        self.data = _load(_B_PATH)
        self.wbs = self.data["wbs"]

    def test_契约校验通过(self):
        """1. 能通过 PlanJson.model_validate"""
        schemas.PlanJson.model_validate(self.data)

    def test_行数等于estimate_rows_for(self):
        """3. B 的行数 == estimate_rows_for(B, COARSE, WHOLE)"""
        leaf_count = _count_leaves(self.wbs)
        estimated = Q.estimate_rows_for(self.wbs, Q.DEPTH_COARSE, Q.FLOOR_WHOLE)
        assert leaf_count == estimated, (
            f"叶子数 {leaf_count} != estimate_rows_for 返回 {estimated}"
        )

    def test_总工期等于关键路径长度(self):
        """6. overview.total_duration_days == cpm_result.total_duration_days"""
        ov = self.data["overview"]
        cpm = self.data["cpm_result"]
        assert ov["total_duration_days"] == cpm["total_duration_days"], (
            f"overview 工期 {ov['total_duration_days']} != cpm_result 工期 {cpm['total_duration_days']}"
        )

    def test_竣工日期与开工日期一致(self):
        """7. planned_end_date == planned_start_date + total_duration_days"""
        ov = self.data["overview"]
        start = date.fromisoformat(ov["planned_start_date"])
        end = date.fromisoformat(ov["planned_end_date"])
        total = ov["total_duration_days"]
        expected_end = start + timedelta(days=total)
        assert end == expected_end, (
            f"竣工日期 {end} != 开工 {start} + {total} 天 = {expected_end}"
        )


class TestRollupConsistency:
    """跨标本一致性：上卷关系"""

    def test_上卷性质断言(self):
        """4. A 按 (COARSE, WHOLE) 分组的行数 == B 的叶子数"""
        a_data = _load(_A_PATH)
        b_data = _load(_B_PATH)

        a_grouped = Q.group_rows(a_data["wbs"], Q.DEPTH_COARSE, Q.FLOOR_WHOLE)
        b_leaf_count = _count_leaves(b_data["wbs"])

        assert len(a_grouped) == b_leaf_count, (
            f"group_rows(A, COARSE, WHOLE) = {len(a_grouped)} 行，"
            f"B 的叶子数 = {b_leaf_count}，两者不等 —— 上卷关系不成立"
        )


# ── 派生值独立重算 ────────────────────────────────────────
# 背景：标本初稿把关键路径和总工期**手写**进文件，结果 A 声称 40 天而实际 45 天
# （关键路径抄了近路 3.1.11→5.1.1，跳过更长的 3.1.12→4.1.1），B 声称 40 天而自身
# 链上工期之和就是 49 天，材料汇总混凝土也少算了一层。旧测试只断言
# overview 与 cpm_result 两个字段相等（互相拷贝的自洽），所以全都漏过。
# 下面独立重算一遍，锁死"派生值必须由数据算出"。

_SPECIMENS = (("A", _A_PATH), ("B", _B_PATH))


def _leaves(wbs: dict) -> list:
    return [
        sp
        for ph in wbs.get("phases", [])
        for wp in ph.get("work_packages", [])
        for sp in (wp.get("sub_packages") or [])
    ]


def _forward_pass(leaves: list, deps: list):
    """独立实现 CPM 前向遍历（FS + lag）。返回 (总工期, ES, EF, 工期表)。"""
    dur = {x["id"]: x.get("duration_days", 0) for x in leaves}
    succ = {i: [] for i in dur}
    indeg = {i: 0 for i in dur}
    for d in deps:
        p, s = d["predecessor"], d["successor"]
        if p in dur and s in dur:
            succ[p].append((s, d.get("lag_days", 0) or 0))
            indeg[s] += 1
    es = {i: 0 for i in dur}
    deg = dict(indeg)
    queue = [i for i, v in deg.items() if v == 0]
    seen = 0
    while queue:
        n = queue.pop(0)
        seen += 1
        for s, lag in succ[n]:
            es[s] = max(es[s], es[n] + dur[n] + lag)
            deg[s] -= 1
            if deg[s] == 0:
                queue.append(s)
    assert seen == len(dur), "依赖图中存在环"
    ef = {i: es[i] + dur[i] for i in dur}
    return max(ef.values()), es, ef, dur


class TestDerivedValuesConsistent:
    """派生值必须由数据算出，不能手写。"""

    def test_关键路径工期之和等于总工期(self):
        for tag, path in _SPECIMENS:
            data = _load(path)
            dur = {x["id"]: x.get("duration_days", 0) for x in _leaves(data["wbs"])}
            cp = data["cpm_result"]["critical_path"]
            total = sum(dur.get(i, 0) for i in cp)
            claimed = data["overview"]["total_duration_days"]
            assert total == claimed, (
                f"{tag}：关键路径工期之和 {total} != 总工期 {claimed}"
            )

    def test_前向遍历等于总工期(self):
        for tag, path in _SPECIMENS:
            data = _load(path)
            total, _es, _ef, _dur = _forward_pass(
                _leaves(data["wbs"]), data["dependencies"]
            )
            claimed = data["overview"]["total_duration_days"]
            assert total == claimed, (
                f"{tag}：CPM 前向遍历 {total} != 总工期 {claimed}"
            )

    def test_关键路径确为最长路(self):
        """逐节点核对 ES：若某节点 ES 大于路径累加值，说明这条路径不是最长路。"""
        for tag, path in _SPECIMENS:
            data = _load(path)
            _total, es, _ef, dur = _forward_pass(
                _leaves(data["wbs"]), data["dependencies"]
            )
            run = 0
            for node in data["cpm_result"]["critical_path"]:
                assert es[node] == run, (
                    f"{tag}：{node} 的最早开始 {es[node]} != 关键路径累加 {run}"
                    f"（该路径不是最长路）"
                )
                run += dur[node]

    def test_overview与cpm字段一致(self):
        for tag, path in _SPECIMENS:
            data = _load(path)
            ov = data["overview"]["total_duration_days"]
            cp = data["cpm_result"]["total_duration_days"]
            assert ov == cp, f"{tag}：overview {ov} != cpm_result {cp}"

    def test_材料汇总与叶子实算一致(self):
        for tag, path in _SPECIMENS:
            data = _load(path)
            leaves = _leaves(data["wbs"])
            want = {
                "混凝土": sum(
                    x.get("quantity", 0)
                    for x in leaves
                    if "混凝土" in x.get("work_type", "")
                    or "混凝土" in x.get("_step_name", "")
                ),
                "砌块": sum(
                    x.get("quantity", 0)
                    for x in leaves
                    if "砌体" in x.get("work_type", "")
                ),
                "钢筋": sum(
                    x.get("quantity", 0)
                    for x in leaves
                    if "钢筋" in x.get("work_type", "")
                ),
            }
            got = {
                m["name"]: m["total_quantity"]
                for m in data["resource_plan"]["material_summary"]
            }
            for name, value in want.items():
                assert name in got, f"{tag}：材料汇总缺少 {name}"
                assert abs(got[name] - value) < 0.01, (
                    f"{tag}：材料 {name} 汇总 {got[name]} != 叶子实算 {value}"
                )

    def test_无悬挂依赖引用(self):
        for tag, path in _SPECIMENS:
            data = _load(path)
            ids = {x["id"] for x in _leaves(data["wbs"])}
            for d in data["dependencies"]:
                assert d["predecessor"] in ids, (
                    f"{tag}：依赖前驱 {d['predecessor']} 不存在"
                )
                assert d["successor"] in ids, (
                    f"{tag}：依赖后继 {d['successor']} 不存在"
                )
