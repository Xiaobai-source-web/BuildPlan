"""产能口径「单一真源」回归门 —— 第 37 轮 P0-1 的收口测试。

背景（已证实的根因）
--------------------
`Norm_Labor_Table.labor_norm_value` 在入库时**已经归一**为「工日 / 1×单位」，
留档不变式为 `raw_value / raw_quantity_basis == labor_norm_value`
（见 `devtools/verify_kb_invariants.py`）。因此:

    产能（单位 / 工日） == 1 / labor_norm_value

历史上围绕 `quantity_basis`（第 37 轮起改名为 `raw_quantity_basis`）出现过
两个**互为镜像**的错误写法，且都被写进了注释"自证"：

    · `basis / norm_value`  → 产能放大 basis 倍 → 班组/工期缩小 basis 倍
      （`norm_bind.py` 写库、`scheduler.py` / `resource.py` / `recompute.py` 反推）
    · `norm_value / basis`  → 产能缩小 basis 倍 → 工期放大 basis 倍（`revise.py`）

`quantity_basis` 只是原始书页基数（10 / 100 / 1000），**只作溯源、不参与乘法**。

反例（本项目实测）：`LN_3853` 铝模定额 `norm=0.025 工日/m²`、`basis=10`
→ 正确产能 40 m²/工日；错写成 `basis/norm` 得 400，1972 m² ÷ (400 × 14 天)
= **1 人**（正确应为 4 人），工期与班组一起塌掉。

机械口径**不适用**本门：`machine_shift_norm` 没有归一，仍是「台班 / basis×单位」，
所以 `scheduler.py` / `resource.py` 的机械分支必须继续乘 basis。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import kb_units                                    # noqa: E402
from pipeline.nodes import revise as revise_mod                  # noqa: E402

# 受本门约束的模块（labor 产能的读取/写入方）
GUARDED = [
    BACKEND / "pipeline" / "nodes" / "norm_bind.py",
    BACKEND / "pipeline" / "nodes" / "scheduler.py",
    BACKEND / "pipeline" / "nodes" / "resource.py",
    BACKEND / "pipeline" / "recompute.py",
    BACKEND / "pipeline" / "nodes" / "revise.py",
]

import ast  # noqa: E402


def _dotted(node) -> str:
    """把 AST 节点还原成一段可读文本（注释/文档字符串不在 AST 里，天然免疫）。"""
    try:
        return ast.unparse(node)
    except Exception:                                        # pragma: no cover
        return ""


def _is_basis(text: str) -> bool:
    t = text.lower()
    return "basis" in t and "norm" not in t


def _is_norm(text: str) -> bool:
    t = text.lower()
    return ("norm" in t or t in ("nv", "n")) and "basis" not in t


def _has_quantity(text: str) -> bool:
    """分子里是否带工程量因子 —— 用来区分「机械台班公式」与「labor 产能反推」。

    机械台班定额**没有**归一，正确写法就是 `量 × 台班定额 / basis`
    （KB 的 machine_shift_norm 是原始值、按 basis×单位 计）；
    而 labor 侧的 `norm / basis` / `basis / norm` 才是那个镜像 bug。
    """
    t = text.lower()
    return ("quantity" in t) or ("qty" in t) or bool(re.search(r"(?:^|[^\w])q(?:[^\w]|$)", t))


def _divisions(path: pathlib.Path):
    """产出 (行号, 左式文本, 右式文本) —— 只看真实的除法表达式。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = _dotted(node.left), _dotted(node.right)
            out.append((getattr(node, "lineno", 0), left, right))
    return out


# ---------------------------------------------------------------- 纯函数层


def test_kb_units_productivity_is_reciprocal():
    """`kb_units.productivity_of` 的唯一真值是 1/norm；basis 只做交叉校验。"""
    assert kb_units.productivity_of(0.025) == pytest.approx(40.0)
    assert kb_units.productivity_of(0.025) != pytest.approx(400.0)      # basis/norm
    assert kb_units.productivity_of(0.025) != pytest.approx(0.0025)     # norm/basis
    # 带 raw_quantity_basis 时结果不变（只作交叉校验，不参与乘法）
    assert kb_units.productivity_of(0.025, 10) == pytest.approx(40.0)


def test_revise_leaf_productivity_uses_reciprocal_not_basis():
    """`revise._leaf_productivity` 必须给 1/norm，而不是历史上的 norm/basis。"""
    leaf = {"norm_binding": {"norm_value": 0.025, "quantity_basis": 10}}
    prod = revise_mod._leaf_productivity(leaf)
    assert prod == pytest.approx(40.0)
    assert prod != pytest.approx(0.0025)     # norm / basis（旧镜像错误）

    # 铝模实例：1972 m²、14 天 → 正确班组 ceil(1972/(40*14)) = 4 人（不是 1 人）
    import math
    assert math.ceil(1972 / (prod * 14)) == 4


def test_revise_leaf_productivity_prefers_binding_value():
    """binding 里已写明的 `productivity_value` 优先于任何反推。"""
    leaf = {"norm_binding": {"norm_value": 0.025, "quantity_basis": 10,
                             "productivity_value": 40.0}}
    assert revise_mod._leaf_productivity(leaf) == pytest.approx(40.0)


def test_revise_leaf_productivity_handles_missing_data():
    assert revise_mod._leaf_productivity({}) is None
    assert revise_mod._leaf_productivity({"norm_binding": None}) is None
    assert revise_mod._leaf_productivity({"norm_binding": {"norm_value": 0}}) is None


# ---------------------------------------------------------------- 知识库层


def test_kb_table_is_reciprocal_and_basis_is_trace_only():
    """知识库落库不变量：每一行 productivity_value == 1/labor_norm_value。

    直连随仓库附带的 `BuildPlan_KB/kb.db`（与其他 KB 测试同口径），
    不经过 `pipeline.kb` 的路径配置。
    """
    import sqlite3

    db = BACKEND.parent / "BuildPlan_KB" / "kb.db"
    assert db.exists(), "随仓库附带的 kb.db 不存在：%s" % db
    con = sqlite3.connect(str(db))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(Norm_Labor_Table)")]
        assert "raw_quantity_basis" in cols, "列未改名：quantity_basis → raw_quantity_basis"
        bad = con.execute(
            "SELECT COUNT(*) FROM Norm_Labor_Table "
            "WHERE labor_norm_value > 0 AND productivity_value IS NOT NULL "
            "AND ABS(productivity_value - 1.0/labor_norm_value) > 1e-9").fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM Norm_Labor_Table").fetchone()[0]
        assert bad == 0, "有 %d/%d 行的 productivity_value != 1/labor_norm_value" % (bad, total)
        # basis≠1 的行最容易被写错，单独确认它们也存在且已被修正
        nz = con.execute(
            "SELECT COUNT(*) FROM Norm_Labor_Table WHERE raw_quantity_basis != 1").fetchone()[0]
        assert nz > 0, "应有 basis≠1 的行可校验"
        # 交叉校验用的溯源值仍在（不参与乘法，只用于 raw_value/basis == norm 的留档不变式）
        r = con.execute(
            "SELECT norm_id, labor_norm_value, raw_value, raw_quantity_basis "
            "FROM Norm_Labor_Table WHERE raw_quantity_basis != 1 AND raw_value IS NOT NULL "
            "LIMIT 1").fetchone()
        assert r is not None
        assert float(r[2]) / float(r[3]) == pytest.approx(float(r[1]), rel=1e-9)
    finally:
        con.close()


# ---------------------------------------------------------------- 绑定层收口点


def test_norm_bind_writes_reciprocal_productivity():
    """P0-1 的收口点：绑定层写进 `binding` 的 productivity_value 必须是 1/norm。

    历史上这里是 `basis / nv`（产能放大 basis 倍），且注释"自证"其对；
    下游全部优先读这个值，所以这里写错 = 数据库修得再干净也白费。
    """
    from pipeline.nodes.norm_bind import NormBindNode

    node = NormBindNode()
    binding = {}
    row = {
        "condition_text": "垫层，带形，木模板",
        "norm_value": 0.025,          # 工日/m²（已归一）
        "norm_unit": "工日/m²",
        "quantity_basis": 10,         # 仅溯源
        "quantity_unit": "m²",
        "source_code": "LD_T72_6_2008",
    }
    node._fill_from_labor_row(binding, row, "default", "kb", "中", "", leaf_unit="m²")

    assert binding["productivity_value"] == pytest.approx(40.0), (
        "产能应为 1/0.025 = 40（单位/工日）；写成 basis/nv = 400 会把班组压成 1/10")
    assert binding["norm_value"] == pytest.approx(0.025)
    assert binding["quantity_basis"] == 10, "溯源字段应保留"
    assert binding["unit"].endswith("m²")


# ---------------------------------------------------------------- 源码守卫


@pytest.mark.parametrize("path", GUARDED, ids=lambda p: p.name)
def test_no_live_module_multiplies_labor_norm_by_basis(path):
    """AST 级守卫：真实的除法表达式里不得再出现两种镜像写法。

    只看 AST，所以注释、文档字符串里讲历史口径不算违规（那些恰恰是防复发的说明）。
    """
    hits = []
    for lineno, left, right in _divisions(path):
        if _is_basis(left) and _is_norm(right):
            hits.append("%s:%d  %s / %s —— 产能被放大 basis 倍，应改为 1 / norm_value"
                        % (path.name, lineno, left, right))
        elif _is_norm(left) and _is_basis(right) and not _has_quantity(left):
            hits.append("%s:%d  %s / %s —— 产能被缩小 basis 倍，应改为 1 / norm_value"
                        % (path.name, lineno, left, right))
    assert not hits, (
        "labor 产能只能来自 1/norm_value；检测到旧口径残留：\n  " + "\n  ".join(hits))


def test_machine_branch_still_divides_by_basis():
    """反向守卫：机械台班**没有**归一，机械分支必须保留「除以 basis」。

    若有人"顺手统一"，把机械侧的 basis 也删掉，台班数会错 basis 倍（最大 1000）。
    用 AST 判定，不受 `item["quantity"] / item["basis"]` 这类写法影响。
    """
    path = BACKEND / "pipeline" / "nodes" / "scheduler.py"
    by_basis = [(lineno, left, right) for (lineno, left, right) in _divisions(path)
                if _is_basis(right) and _has_quantity(left)]
    assert by_basis, (
        "scheduler.py 的机械分支应保留「工程量 / basis × 台班定额」"
        "（台班定额未归一，basis 必须参与乘法）；当前找不到任何「量 / basis」除法")

