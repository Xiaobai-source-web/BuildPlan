# -*- coding: utf-8 -*-
"""D2 回归：工程量的中文算式必须**落在值上**。

背景（真实缺陷，已修）：
  `_make_leaf` 里 `quantity = 单层量 × 覆盖层数`，但挂上去的 `_qty_formula` 是
  **单层量表**达式（以 "/层" 结尾），层数这一步**没写进算式**。于是出现：

      值 27.0 t ——  算式却是 `…÷2层÷1区 = 53.33 t/层`

  用户按算式复核会算出 53.33，与值差一倍，看起来像算错了（"算得清"直接受损）。
  同族的还有：单层量四舍五入后（22.46 → 22）算式末尾也对不上。

修法（`layer_engine._make_leaf`）：
  · 覆盖层数 ≠ 1 → 算式补 `×N层 = 值 单位`，落不到整数时标"（取整）"；
  · 覆盖层数 = 1 但取整后与单层值不等 → 补"（取整为 值 单位）"。

运行：python -m pytest backend/tests/test_qty_formula_layer_step.py -q
"""

import copy
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from pipeline import layer_engine as LE  # noqa: E402
from pipeline.nodes.beat_configs import BASE_BEAT_CONFIGS  # noqa: E402

PARAMS = {"floors": 38, "total_area": 301354.26, "building_count": 1,
          "total_concrete": 82000, "total_rebar": 12800}

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _flow(ph):
    return [l for wp in ph["work_packages"] for l in wp["sub_packages"]
            if not l.get("_parallel")]


def _last_num(text):
    nums = _NUM.findall(text or "")
    return float(nums[-1]) if nums else None


def _step(per_floor, name="钢筋绑扎", unit="t"):
    return {"name": name, "unit": unit, "resource": "钢筋工",
            "qty_per_floor": per_floor, "work_type": "钢筋工程"}


def _note(formula):
    return {"source": "参数推算", "formula": formula}


# ---------------- 单元级：直接盯住缺陷本身 ----------------
class TestMakeLeafFormula:
    def test_半层任务必须写明层数折算(self):
        leaf = LE._make_leaf("9", 1, 1, 1, "Ⅰ区", 1.0, 1.5, 0.5,
                             _step(53.33),
                             _note("1066.67t×10%÷2层÷1区 = 53.33 t/层"))
        assert leaf["quantity"] == 27.0
        f = leaf["_qty_formula"]
        assert "×0.5层 = 27 t" in f, f
        assert _last_num(f) == 27.0, f

    def test_单层取整也要写明(self):
        leaf = LE._make_leaf("9", 1, 1, 1, "Ⅰ区", 1.0, 2.0, 1.0,
                             _step(22.46),
                             _note("1066.67t×80%÷38层÷1区 = 22.46 t/层"))
        assert leaf["quantity"] == 22.0
        f = leaf["_qty_formula"]
        assert "取整为 22 t" in f, f
        assert _last_num(f) == 22.0, f

    def test_三层一组要写明乘三(self):
        leaf = LE._make_leaf("9", 1, 1, 1, "Ⅰ区", 1.0, 4.0, 3.0,
                             _step(800.0, name="内墙抹灰", unit="m²"),
                             _note("单栋标准层471.49㎡×1.7÷1区 = 801.53 m²/层"))
        f = leaf["_qty_formula"]
        assert "×3层" in f, f
        assert _last_num(f) == float(leaf["quantity"]), f

    def test_单层量正好整除时不加多余括号(self):
        leaf = LE._make_leaf("9", 1, 1, 1, "Ⅰ区", 1.0, 2.0, 1.0,
                             _step(50.0), _note("1000t×5%÷1层÷1区 = 50 t/层"))
        assert leaf["quantity"] == 50.0
        assert "取整" not in leaf["_qty_formula"], leaf["_qty_formula"]

    def test_没有公式时不编造(self):
        leaf = LE._make_leaf("9", 1, 1, 1, "Ⅰ区", 1.0, 1.5, 0.5, _step(53.33), None)
        assert leaf["_qty_formula"] == ""
        assert leaf["quantity"] == 27.0


# ---------------- 集成级：真实配置展开后逐叶核对 ----------------
@pytest.mark.parametrize("phase", ["地下室结构", "地上主体结构", "装饰装修",
                                   "二次结构与砌体"])
def test_展开后每条算式的末值都等于工程量(phase):
    cfg = BASE_BEAT_CONFIGS[phase]
    ph, _ = LE.expand_node(copy.deepcopy(cfg), dict(PARAMS))
    leaves = _flow(ph)
    assert leaves, "%s 未产出叶子" % phase
    checked = 0
    for lf in leaves:
        f = lf.get("_qty_formula") or ""
        if not f:
            continue
        checked += 1
        got = _last_num(f)
        assert got is not None, "%s：算式里没有数字 → %s" % (lf["id"], f)
        assert abs(got - float(lf["quantity"])) <= 0.51, (
            "%s（%s）：算式末值 %s != 工程量 %s\n  %s"
            % (lf["id"], phase, got, lf["quantity"], f))
    assert checked, "%s 没有任何带算式的叶子，测试没有实际覆盖" % phase


def test_地下室半层任务写明折算():
    cfg = BASE_BEAT_CONFIGS["地下室结构"]
    ph, _ = LE.expand_node(copy.deepcopy(cfg), dict(PARAMS))
    halves = []
    for lf in _flow(ph):
        pf = lf.get("_qty_per_floor") or 0
        if not pf or not lf.get("_qty_formula"):
            continue
        if abs(lf["quantity"] / pf - 1.0) > 0.01:      # 覆盖层数 ≠ 1
            halves.append(lf)
    assert halves, "地下室应有非单层的任务（0.5 层一段）"
    for lf in halves:
        assert "层 = " in lf["_qty_formula"], lf["_qty_formula"]


def test_一层一段不出现乘一():
    cfg = BASE_BEAT_CONFIGS["地上主体结构"]
    ph, _ = LE.expand_node(copy.deepcopy(cfg), dict(PARAMS))
    for lf in _flow(ph):
        assert "×1层" not in (lf.get("_qty_formula") or ""), \
            "单层任务不该出现无意义的「×1层」"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            fn()
            print("ok  %s" % fn.__name__)
        except TypeError:
            pass          # 带参数的用例交给 pytest
    cls = TestMakeLeafFormula()
    for name in sorted(dir(cls)):
        if name.startswith("test_"):
            getattr(cls, name)()
            print("ok  TestMakeLeafFormula.%s" % name)
    print("全部通过")
