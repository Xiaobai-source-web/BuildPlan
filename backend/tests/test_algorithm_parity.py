"""算法一致性断言：backend 节点输出 == CPM .txt / 资源定额.txt 独立运行输出。

运行：python -m pytest backend/tests/test_algorithm_parity.py -v
（依赖 pytest；无 pytest 也可直接 python 运行本文件）
"""

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent          # backend/
ROOT = BACKEND.parent                                      # 生产级部署/
sys.path.insert(0, str(BACKEND))

from pipeline.nodes import cpm as our_cpm
from pipeline.nodes import resource as our_resource


def load_module(path, modname):
    # .txt 扩展名不被 spec_from_file_location 识别，用 SourceFileLoader 显式加载
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def find_orig(name, aliases=()):
    """算法原始稿的位置：根目录 / `docs/`（旧名 `资料/`）都探一遍；支持旧名别名。

    2026-09-18 的根目录整理把算法原始稿移到了资料目录；之后又把带空格的怪名字
    `CPM .txt` 改成了 `CPM算法来源.txt`（提交文件夹里不该有 `CPM .txt` 这种名字）；
    第 33 轮目录治理把 `资料/` 改名成 `docs/` —— 这里同时接受新旧目录名，
    这条测试的价值是**算法输出一致**，不是**文件放哪儿**。
    """
    names = (name,) + tuple(aliases)
    for base in (ROOT, ROOT / "docs", ROOT / "资料"):
        for n in names:
            p = base / n
            if p.exists():
                return p
    raise FileNotFoundError("找不到算法原始稿 %s（已查 %s 与 %s）"
                            % (" / ".join(names), ROOT, ROOT / "docs"))


orig_cpm = load_module(find_orig("CPM算法来源.txt", aliases=("CPM .txt",)), "orig_cpm_txt")
orig_resource = load_module(find_orig("资源定额.txt"), "orig_resource_txt")

# ======================================================================
# 样例输入（附录 A 风格 + SS/lag 用例）
# ======================================================================
WBS_A = {
    "phases": [
        {"phase": "施工准备", "work_packages": [
            {"id": "1.1", "name": "场地准备", "sub_packages": [
                {"id": "1.1.1", "name": "场地平整", "duration_days": 5,
                 "quantity": 12000, "unit": "㎡", "work_type": "土建临建"},
                {"id": "1.1.2", "name": "临时道路", "duration_days": 3,
                 "quantity": 1, "unit": "项", "work_type": "土建临建"}]},
            {"id": "1.2", "name": "临水临电", "sub_packages": [
                {"id": "1.2.1", "name": "临时水电", "duration_days": 4,
                 "quantity": 1, "unit": "项", "work_type": "机电安装"}]}]},
        {"phase": "地下结构", "work_packages": [
            {"id": "2.1", "name": "桩基", "sub_packages": [
                {"id": "2.1.1", "name": "PRC管桩", "duration_days": 30,
                 "quantity": 24000, "unit": "m", "work_type": "桩基工程"}]},
            {"id": "2.2", "name": "基坑与底板", "sub_packages": [
                {"id": "2.2.1", "name": "土方开挖", "duration_days": 25,
                 "quantity": 96000, "unit": "m³", "work_type": "土方工程"},
                {"id": "2.2.2", "name": "底板混凝土", "duration_days": 15,
                 "quantity": 15600, "unit": "m³", "work_type": "混凝土工程"}]}]},
    ]
}

# FS + 二级 ID 引用（1.1 → 叶子 1.1.1 映射）
DEPS_FS = {"dependencies": [
    {"predecessor": "1.1", "successor": "1.2.1", "type": "FS", "lag_days": 0},
    {"predecessor": "1.2.1", "successor": "2.1.1", "type": "FS", "lag_days": 0},
    {"predecessor": "2.1.1", "successor": "2.2.1", "type": "FS", "lag_days": 0},
    {"predecessor": "2.2.1", "successor": "2.2.2", "type": "FS", "lag_days": 0},
]}

# SS + lag
DEPS_SS = {"dependencies": [
    {"predecessor": "1.1.1", "successor": "1.2.1", "type": "SS", "lag_days": 2},
    {"predecessor": "2.1.1", "successor": "2.2.1", "type": "FS", "lag_days": 1},
    {"predecessor": "2.2.1", "successor": "2.2.2", "type": "SS", "lag_days": 3},
]}

PARAMS = {
    "total_concrete": 52000, "total_rebar": 7500, "total_earthwork": 96000,
    "total_area": 128000, "total_infill_wall": 24000, "total_pile": 8000,
}
BOUNDARY = {
    "equipment_peak": {"挖掘机": 12, "塔吊": 7},
    "labor_peak": 929,
    "trade_peak": {"钢筋工": 165, "模板工": 220},
}


def test_cpm_parity_fs():
    a = orig_cpm.main(WBS_A, DEPS_FS)
    b = our_cpm.main(WBS_A, DEPS_FS)
    assert a == b, f"CPM FS 输出不一致：\norig={a}\nnew={b}"


def test_cpm_parity_ss_lag():
    a = orig_cpm.main(WBS_A, DEPS_SS)
    b = our_cpm.main(WBS_A, DEPS_SS)
    assert a == b, f"CPM SS/lag 输出不一致：\norig={a}\nnew={b}"


def test_cpm_smoke():
    r = our_cpm.main(WBS_A, DEPS_FS)["cpm_result"]
    assert r["total_duration_days"] > 0
    assert all(x in (r["critical_path"]) for x in [])


def test_resource_parity_full():
    a = orig_resource.main(WBS_A, PARAMS, BOUNDARY)
    b = our_resource.compute_flat(WBS_A, PARAMS, BOUNDARY)
    assert a == b, f"资源定额(带参数/边界)输出不一致：\norig={a}\nnew={b}"


def test_resource_parity_min():
    a = orig_resource.main(WBS_A, None, None)
    b = our_resource.compute_flat(WBS_A, None, None)
    assert a == b, f"资源定额(无参数)输出不一致：\norig={a}\nnew={b}"


def test_resource_nested_shape():
    """v1.1 契约：嵌套 resources 结构正确，且与扁平结果总量一致。"""
    flat = our_resource.compute_flat(WBS_A, PARAMS, BOUNDARY)
    nested = our_resource.to_nested_resources(flat["resource_demand"])
    t = nested["tasks"][0]
    assert "resources" in t
    for res, q in t["resources"].items():
        assert set(q.keys()) == {"per_day", "total_days"}, res
        flat_key = f"{res}_per_day"
        flat_tasks = flat["resource_demand"]["tasks"]
        assert q["per_day"] == flat_tasks[0].get(flat_key), res


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n全部 {len(tests)} 个算法一致性用例通过 ✔")
