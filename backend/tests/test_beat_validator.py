"""节拍专属校验测试 — T-12 / v2.2（一层一段）

对 4 个节拍阶段（4 地下 / 5 主体 / 6 二次 / 8 装修）办公同一套
BASE+专属规则 + 通用校验（层数守恒/量级±20%/节拍域[2,90]）的正反例。

运行：python -m pytest backend/tests/test_beat_validator.py -v
"""

import sys
import copy
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import layer_engine as LE
from pipeline.nodes.beat_configs import BASE_BEAT_CONFIGS, specific_error


def _clone(name, **mut):
    cfg = copy.deepcopy(BASE_BEAT_CONFIGS[name])
    for k, v in mut.items():
        cfg[k] = v
    return cfg


# ---------------- 专属规则：正例（基线本身应通过） ----------------
def test_specific_rules_pass_baseline():
    for name in BASE_BEAT_CONFIGS:
        assert specific_error(name, BASE_BEAT_CONFIGS[name]) is None, name


# ---------------- 专属规则：反例 ----------------
def test_basement_requires_floors2_segments4():
    assert specific_error("地下室结构", _clone("地下室结构", segments=3))

def test_main_requires_one_floor_per_segment():
    # v2.2：主体不再是「segments=8」，而是「一层一段（floors_per_segment=1）」
    assert specific_error("地上主体结构", _clone("地上主体结构", floors_per_segment=5))
    # segments 只是兜底展示值，改了不该报错（真实段数由层数推导）
    assert specific_error("地上主体结构", _clone("地上主体结构", segments=7)) is None

def test_secondary_requires_one_floor_and_floors_ahead3():
    assert specific_error("二次结构与砌体", _clone("二次结构与砌体", floors_per_segment=5))
    bad = _clone("二次结构与砌体")
    bad["lead_in"] = {"from_node": "5", "floors_ahead": 1}
    assert specific_error("二次结构与砌体", bad)

def test_decoration_requires_parallel():
    bad = _clone("装饰装修", parallel_work=[])
    assert specific_error("装饰装修", bad)


# ---------------- 通用校验：层数守恒 / 量级 / 节拍域 ----------------
def test_common_validate_catches_floor_conservation():
    # 正例：一层一段下段数随层数走，40 层 → 40 段全覆盖
    cfg = _clone("地上主体结构", floors=40)
    ph, _ = LE.expand_node(cfg, {"floors": 40})
    assert LE.common_validate(cfg, ph, {"floors": 40}) == []

    # 正例：项目 38 层（配置兜底 38）
    bad = _clone("地上主体结构", floors=38)
    ph2, _ = LE.expand_node(bad, {"floors": 38})
    assert LE.common_validate(bad, ph2, {"floors": 38}) == []

    # 反例：无 per、段数被写死 → 层数不均也守恒，但人为只铺前 3 段必然不守恒
    partial = copy.deepcopy(ph2)
    partial["work_packages"] = partial["work_packages"][:1]
    errs = LE.common_validate(cfg, partial, {"floors": 40})
    assert any("量级偏差" in e for e in errs), errs


def test_common_validate_flags_magnitude_and_domain():
    cfg = copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"])
    ph, _ = LE.expand_node(cfg, {"floors": 38})

    # 节拍域：上限已放宽到 90，30 天属合法（旧口径会误报）
    wp = ph["work_packages"][0]
    wp["sub_packages"][0]["duration_days"] = 30
    assert not any("节拍越界" in e for e in LE.common_validate(cfg, ph, {"floors": 38}))

    # 越界仍然要报：超过上限 → 报错
    wp["sub_packages"][0]["duration_days"] = LE.CLAMP_MAX + 1
    errs = LE.common_validate(cfg, ph, {"floors": 38})
    assert any("节拍越界" in e for e in errs), errs


if __name__ == "__main__":
    import inspect
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and inspect.isfunction(v)]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print("全部 beat_validator 用例通过 ✔")