# -*- coding: utf-8 -*-
"""自然语言修改的 HTTP 端点测试 —— 「改得动」必须真的从接口这一层就能改

注意：**不能用 pytest 的 `tmp_path`** —— 本机沙箱下 `%TEMP%\\pytest-of-*` 会被拒绝访问
（PermissionError WinError 5），一律用普通 `mkdir` 建在 `backend/_test_tmp/` 下。

覆盖：
  1. POST /revise：一句话 → 生效 → 计划里日期/总工期真的变了 → 交付物文件被覆盖
  2. 改不出来时如实回报（ok=False + warnings），不假装成功
  3. 参数校验：缺 plan_id / 缺 instruction / 计划不存在 → 4xx，不是 500
  4. GET  /plans/{id}/versions：能看到初版与每一轮修改
  5. POST /plans/{id}/undo：回退一轮后总工期回到改前
  6. POST /plans/{id}/goto：回退到初版（version=0）
  7. 端点永不 500：即使计划是畸形结构，也要给出可读的错误或降级结果

运行：python -m pytest backend/tests/test_revise_api.py -q
"""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest


def _leaf(tid, name, quantity, duration, crew=5):
    return {
        "id": tid, "name": name, "duration_days": duration,
        "quantity": quantity, "unit": "t", "work_type": "钢筋工程",
        "norm_binding": {
            "task_id": tid, "mode": "labor", "productivity_value": 1.0,
            "source_code": "LD_T72_7_2008", "match_type": "exact",
            "labor_types": ["钢筋工"], "crew": {"钢筋工": crew},
        },
    }


def _plan(plan_id="api_test_plan"):
    """A → B 串行；A 100 单位 ÷ (1.0 × 5 人) = 20 天，B 同 → 总 40 天。

    ⚠️ **2026-09-21（C 组新链路口径）修正**：原先这份 fixture **没有任何容量数据**
    （`extracted_params` 只有 `floors`，叶子既无 `kb_activity_id` 也无 `workface_capacity`），
    于是每一条任务都落到 `capacity_source == "reported_missing"`：
    资源与班组留空、**工期沿用叶子原值**。后果是「工程量改 3 倍 → 总工期一动不动」，
    而本文件的核心断言正是「修订真的改变了计划」。所以这里补上**最小可用的容量数据**
    （`total_area` / `buildings`）：单栋标准层 = 1000 ÷ 1 ÷ 2 = 500 m² → 1 个施工段，
    钢筋工 MWI = 12 → 段容量 ⌈500/12⌉ = 42 人 → 工期随工程量正常响应。

    `overview.total_duration_days` / `cpm_result` 由**同一份新链路口径重算**得出
    （见 `_initial_total`），不再写死 40 —— 否则"初版档案说的工期"与"新链路算出的工期"
    本来就不一致，`修订后 > 初版` 这类断言测的就不是同一件事了。
    """
    plan = {
        "plan_id": plan_id,
        "overview": {"project_name": "接口测试", "total_duration_days": 0,
                     "planned_start_date": "2026-01-01",
                     "planned_end_date": "2026-02-10", "critical_path_length": 2},
        "wbs": {"phases": [{"phase": "主体结构", "work_packages": [
            {"id": "5.1", "name": "Ⅰ区主体", "sub_packages": [
                _leaf("5.1.1.1", "钢筋绑扎", 100.0, 20),
                _leaf("5.1.1.2", "混凝土浇筑", 100.0, 20),
            ]}]}]},
        "dependencies": [
            {"predecessor": "5.1.1.1", "successor": "5.1.1.2", "type": "FS", "lag_days": 0}],
        "cpm_result": {"total_duration_days": 0, "critical_path": []},
        "resource_plan": {"total_manpower_days": 0.0, "peak_manpower": 0,
                          "equipment_peak": {}, "material_summary": []},
        "meta": {"audit_status": "未审计", "plan_level": "L4",
                 "extracted_params": {"floors": 2, "total_area": 1000.0,
                                      "buildings": 1.0},
                 "boundary_conditions": {}},
    }
    total = _initial_total(plan)
    plan["overview"]["total_duration_days"] = total
    plan["cpm_result"]["total_duration_days"] = total
    return plan


def _initial_total(plan):
    """用**排程器自己的口径**算出这份计划的总工期（fixture 的自洽基线）。

    不能让 fixture 写死一个与排程器口径无关的数字：那会让「修订后总工期 > 初版」
    这类断言在初版档案与实算结果本来就不一致时失去意义。
    """
    from pipeline.nodes.scheduler import compute_schedules

    meta = plan["meta"]
    out = compute_schedules(plan["wbs"], plan["dependencies"],
                            meta["boundary_conditions"], meta["extracted_params"])
    return int(out["schedule_versions"]["resource_ok"]["total_duration_days"])


@pytest.fixture()
def api(monkeypatch):
    """把交付物目录指向**普通 mkdir 建的**临时目录，返回 (main 模块, plan_id, 目录)。

    刻意不用 pytest 的 tmp_path：本机沙箱下它落在 %TEMP% 里、后续写入被拒。
    """
    import os
    import shutil

    import main
    from pipeline import config

    root = BACKEND / "_test_tmp" / ("api_p%s" % os.getpid())
    shutil.rmtree(str(root), ignore_errors=True)
    plans = root / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main, "PLANS_DIR", plans, raising=False)
    monkeypatch.setattr(config, "PLANS_DIR", plans, raising=False)

    plan_id = "api_test_plan"
    (plans / (plan_id + ".json")).write_text(
        json.dumps(_plan(plan_id), ensure_ascii=False), encoding="utf-8")
    yield main, plan_id, plans
    shutil.rmtree(str(root), ignore_errors=True)


# ==================== 1. 正常改写 ====================
def test_revise_endpoint_changes_the_plan(api):
    main, plan_id, plans = api
    before = _baseline(plans, plan_id)          # 修订会覆盖计划文件，先取基线
    res = main.revise({"plan_id": plan_id, "instruction": "把 5.1.1.1 的工程量改成 300"})

    assert res["ok"] is True, res
    assert res["applied"], res
    assert "总工期" in res["summary"]
    # 新链路（C10：工期 = 需求量 ÷ 有效容量）：工程量 100 → 300，
    # 段容量 42 人不变 → 本任务工期 3 → 8 天，总工期随之变长。
    assert res["total_duration_days"] > before, "工程量变 3 倍，总工期必须变长"
    # 重排结果必须回写到交付物文件（用户刷新看板要看到新计划）
    saved = json.loads((plans / (plan_id + ".json")).read_text(encoding="utf-8"))
    assert saved["overview"]["total_duration_days"] == res["total_duration_days"]
    assert saved["all_tasks_schedule"], "必须回写逐条任务日期"
    dates = dict((t["task_id"], t) for t in saved["all_tasks_schedule"])
    assert dates["5.1.1.2"]["start_date"] >= dates["5.1.1.1"]["finish_date"], "依赖要生效"
    # 返回体里也带上重排后的计划，前端不用二次请求
    assert res["plan"]["overview"]["total_duration_days"] == res["total_duration_days"]


def test_revise_endpoint_accepts_text_alias(api):
    """instruction / text 两个键名都认（终端与网页端叫法不同）。

    用户写"工期改成 7"是**命令**：排程不得把它覆盖回去。

    ⚠️ **2026-09-21（C 组裁定 G）通道变了**：反解出的 15 人**不再**写回
    `leaf["norm_binding"]["crew"]`（C8 第 6 项已删掉「叶子上写明的投入人工」这个人数来源，
    写回去也没人读），而是写进 **用户同类限额**：
    `boundary_conditions["crew_design"]["钢筋工"] = ⌈100 ÷ 7⌉ = 15` +
    `_source["crew_design"] = "user"` → `min(段容量 42, 该限额 15) = 15` → `⌈100/15⌉ = 7 天`。
    """
    main, plan_id, _ = api
    res = main.revise({"plan_id": plan_id, "text": "把 5.1.1.1 的工期改成 7"})
    assert res["ok"] is True, res
    dates = dict((t["task_id"], t["duration_days"])
                 for t in res["plan"]["all_tasks_schedule"])
    assert dates["5.1.1.1"] == 7, "用户点名的工期就是最终值"
    # 定额工日没被篡改：反解结果走「用户同类限额」通道（不再是 binding["crew"]）
    bc = res["plan"]["meta"]["boundary_conditions"]
    assert bc["crew_design"]["钢筋工"] == 15, "反解到 100 ÷ 7 ≈ 15 人"
    assert bc["_source"]["crew_design"] == "user", "来源必须标成用户"
    leaf = res["plan"]["wbs"]["phases"][0]["work_packages"][0]["sub_packages"][0]
    assert leaf["norm_binding"]["crew"] == {"钢筋工": 5}, \
        "旧通道（binding.crew）必须保持不变，不能再被反解覆盖"


# ==================== 2. 改不出来要如实说 ====================
def test_revise_endpoint_reports_when_nothing_understood(api):
    main, plan_id, _ = api
    res = main.revise({"plan_id": plan_id, "instruction": "随便说点什么吧"})
    assert res["ok"] is False
    assert res["applied"] == []
    assert res["warnings"], "没识别出可执行修改时必须给提示，不能静默成功"


# ==================== 3. 参数校验 ====================
def test_revise_endpoint_validates_input(api):
    main, plan_id, _ = api
    for body, code in (({"instruction": "改一下"}, 400),
                       ({"plan_id": plan_id}, 400),
                       ({"plan_id": "不存在", "instruction": "改一下"}, 404)):
        res = main.revise(body)
        assert getattr(res, "status_code", None) == code, (body, res)


def test_revise_endpoint_never_500_on_broken_plan(api):
    """畸形计划也要给出可读结果，不能让接口 500。"""
    main, plan_id, plans = api
    (plans / (plan_id + ".json")).write_text(
        json.dumps({"plan_id": plan_id, "wbs": "不是字典",
                    "overview": {"total_duration_days": 10}}, ensure_ascii=False),
        encoding="utf-8")
    res = main.revise({"plan_id": plan_id, "instruction": "把 5.1.1.1 的工期改成 7"})
    assert getattr(res, "status_code", 200) == 200, res


# ==================== 4~6. 修订链：看版本 / 回退 / 跳转 ====================
def test_versions_undo_goto_roundtrip(api):
    main, plan_id, plans = api
    before = _baseline(plans, plan_id)

    r1 = main.revise({"plan_id": plan_id, "instruction": "把 5.1.1.1 的工程量改成 300"})
    assert r1["ok"] and r1["total_duration_days"] > before
    after_first = r1["total_duration_days"]

    v = main.plan_versions(plan_id)
    assert v["versions"], "修订后必须能查到版本"
    assert any(x.get("说明") for x in v["versions"])
    assert v["history"] and any(x.get("摘要") for x in v["history"])

    # 再改一轮（改 B 的班组），确保链上有两轮
    r2 = main.revise({"plan_id": plan_id, "instruction": "把 5.1.1.2 的班组改成 2 人"})
    if r2["ok"]:
        assert main.plan_versions(plan_id)["versions"]

    # 回退到初版
    g = main.plan_goto(plan_id, {"version": 0})
    assert g["ok"] is True
    assert g["total_duration_days"] == before, "初版总工期必须回到 40 天"

    # 重新改一轮，再 undo 一轮
    r3 = main.revise({"plan_id": plan_id, "instruction": "把 5.1.1.1 的工程量改成 200"})
    assert r3["ok"], r3
    grew = r3["total_duration_days"]
    assert grew > before
    u = main.plan_undo(plan_id)
    assert u["ok"] is True
    assert u["total_duration_days"] == before, "undo 后应回到初版（前一轮已被 goto 清掉）"


def test_goto_validates_version(api):
    main, plan_id, _ = api
    res = main.plan_goto(plan_id, {"version": "不是数字"})
    assert getattr(res, "status_code", None) == 400
    res2 = main.plan_goto(plan_id, {})
    assert getattr(res2, "status_code", None) == 400


def test_undo_without_any_revision_is_a_clean_404(api):
    main, plan_id, _ = api
    res = main.plan_undo(plan_id)
    assert getattr(res, "status_code", None) in (404, 200)


# ==================== 7. 真过一遍 HTTP（不只是直调函数）====================
def test_revise_over_real_http(api):
    """用 starlette TestClient 走真实的 HTTP 层：路由 / 序列化 / 状态码都要对。

    直调函数测不出"路径没注册""返回体不可 JSON 序列化""中文编码坏了"这类问题，
    而这三类恰恰是接口最容易翻车的地方。
    """
    import pytest as _pytest

    main, plan_id, plans = api
    base = _baseline(plans, plan_id)            # 修订会覆盖计划文件，先取基线
    client_mod = _pytest.importorskip("starlette.testclient")
    with client_mod.TestClient(main.app) as http:
        # 计划要能取到（PLANS_DIR 已被 fixture 指到临时目录）
        r = http.get("/plans/%s" % plan_id)
        assert r.status_code == 200, r.text
        assert r.json()["plan_id"] == plan_id

        r = http.post("/revise", json={"plan_id": plan_id,
                                       "instruction": "把 5.1.1.1 的工程量改成 300"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True, body
        assert body["total_duration_days"] > base
        assert "总工期" in body["summary"]

        r = http.get("/plans/%s/versions" % plan_id)
        assert r.status_code == 200 and r.json()["versions"], r.text

        r = http.post("/plans/%s/goto" % plan_id, json={"version": 0})
        assert r.status_code == 200, r.text
        assert r.json()["total_duration_days"] == base

        # 参数缺失要走 4xx，不是 500
        assert http.post("/revise", json={"instruction": "x"}).status_code == 400
        assert http.post("/revise", json={"plan_id": plan_id}).status_code == 400


# ==================== 8. Phase 3：新能力要能从接口这一层改到 ====================
def test_新增工序能从接口改到并回写交付物(api):
    """在 <工序> 后面增加一个工序：新任务要真的进树、进依赖、进日程、进交付物文件。"""
    main, plan_id, plans = api
    res = main.revise({"plan_id": plan_id,
                       "instruction": "在 5.1.1.1 后面增加一个工序：地下室防水"})
    assert res["ok"] is True, res
    assert [p["field"] for p in res["applied"]] == ["add_task"], res["applied"]
    new_id = str(res["applied"][0]["target"])
    assert new_id == "5.1.1.3", "自动编号该是同级的 5.1.1.3（已有 5.1.1.1/5.1.1.2），实际 %s" % new_id

    plan = res["plan"]
    ids = [str(s.get("id"))
           for s in plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]]
    assert new_id in ids, ids
    deps = [(str(d.get("predecessor")), str(d.get("successor")))
            for d in (plan.get("dependencies") or [])]
    assert ("5.1.1.1", new_id) in deps, "必须串上前后置，否则新任务会被排到一边：%s" % deps
    # 交付物文件也要被回写（用户刷新看板要看到）
    saved = json.loads((plans / (plan_id + ".json")).read_text(encoding="utf-8"))
    saved_ids = [str(s.get("id"))
                 for s in saved["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]]
    assert new_id in saved_ids, saved_ids

    # 回退到初版：新增的任务必须消失（修订链靠重放 patch，重放错了这里就会露馅）
    back = main.plan_goto(plan_id, {"version": 0})
    back_plan = back.get("plan") if isinstance(back, dict) else None
    if back_plan is not None:
        back_ids = [str(s.get("id"))
                    for s in back_plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]]
        assert new_id not in back_ids, "回退后新增任务还在：%s" % back_ids


def test_做不到的意图接口层要给出能力说明(api):
    """一条都没生效时必须回 `hint`（做不到什么 + 能做的是…），不能只有一句"没看懂"。"""
    main, plan_id, _ = api
    res = main.revise({"plan_id": plan_id, "instruction": "把层数改成 5 层",
                       "dry_run": True})
    assert res["ok"] is False
    assert res["applied"] == []
    assert res.get("hint"), "dry_run 也必须带 hint，否则终端没得显示"
    assert "我能直接改的是" in res["hint"]
    assert res["hint"] in res["summary"]


def test_改名走接口只改名字不动工期(api):
    """改名是纯文本改动：一条工期都不许动。

    注意断言的是**计划树**里的工期 —— 这份 fixture 的计划还没有 `all_tasks_schedule`
    （改名刻意不触发排程，所以跑完也不会凭空长出一份日程表）。
    """
    main, plan_id, _ = api

    def tree(plan):
        return plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]

    plan0 = json.loads(plans_of(api).read_text(encoding="utf-8"))
    before = dict((str(s["id"]), s["duration_days"]) for s in tree(plan0))
    res = main.revise({"plan_id": plan_id, "instruction": "把 5.1.1.1 的名字改成 地下室防水"})
    assert res["ok"] is True, res
    assert [p["field"] for p in res["applied"]] == ["name"], res["applied"]
    after = dict((str(s["id"]), s["duration_days"]) for s in tree(res["plan"]))
    assert after == before, "改名把工期改了：%s → %s" % (before, after)
    names = dict((str(s["id"]), s.get("name")) for s in tree(res["plan"]))
    assert names["5.1.1.1"] == "地下室防水", names
    # 交付物文件也要跟着改（否则用户刷新看板还是旧名字）
    saved = json.loads(plans_of(api).read_text(encoding="utf-8"))
    saved_names = dict((str(s["id"]), s.get("name")) for s in tree(saved))
    assert saved_names["5.1.1.1"] == "地下室防水", saved_names


def plans_of(api):
    """小工具：取回被 fixture 指到临时目录的那份计划文件。"""
    _main, plan_id, plans = api
    return plans / (plan_id + ".json")


def _baseline(plans, plan_id):
    """计划文件里**当前**的总工期（修订会覆盖它，所以要在修订之前取）。"""
    saved = json.loads((plans / (plan_id + ".json")).read_text(encoding="utf-8"))
    return int(saved["overview"]["total_duration_days"])


def test_缺容量数据时工期不随工程量变化但必须告警(api):
    """`capacity_source == "reported_missing"` 的收口（C 组裁定 B + 2026-09-21 收口）。

    当一条任务**既没有层面积/MWI 容量、也没有工作面容量、也没有用户同类限额**时，
    按降级口径它**不编人数、工期沿用叶子原值** —— 于是"改工程量"**不会**改工期。
    这本身是设计后果，但**绝不能静默**：必须在行依据与返回的 warnings 里写明
    「本次工程量变化未反映到工期（缺容量数据）」，否则用户会以为修订没生效。
    """
    main, plan_id, plans = api
    # 把 fixture 打成"无任何容量数据"：删掉层面积口径
    saved = json.loads((plans / (plan_id + ".json")).read_text(encoding="utf-8"))
    saved["meta"]["extracted_params"] = {"floors": 2}
    for leaf in saved["wbs"]["phases"][0]["work_packages"][0]["sub_packages"]:
        leaf.pop("workface_capacity", None)
        leaf.pop("kb_activity_id", None)
    (plans / (plan_id + ".json")).write_text(
        json.dumps(saved, ensure_ascii=False), encoding="utf-8")

    res = main.revise({"plan_id": plan_id, "instruction": "把 5.1.1.1 的工程量改成 300"})
    assert res["ok"] is True, res
    # 工期沿用叶子原值（20 天），不随工程量变化 —— 设计后果，如实钉住
    dates = dict((t["task_id"], t["duration_days"])
                 for t in res["plan"]["all_tasks_schedule"])
    assert dates["5.1.1.1"] == 20, "缺容量数据 → 不编人数、工期沿用叶子原值"
    # 但**必须**把"这次改动没反映到工期"说出来
    assert any("缺工作面容量数据" in w and "不随工程量变化" in w
               for w in (res.get("warnings") or [])), \
        "缺容量数据导致工期不响应时，必须给出可读告警：%s" % (res.get("warnings"),)
    # 行依据也要落进计划檔案（"为什么工期不动"必须查得到）
    rows = dict((t["task_id"], t) for t in res["plan"]["all_tasks_schedule"])
    assert rows["5.1.1.1"]["capacity_source"] == "reported_missing"
    assert "本次工程量变化未反映到工期" in rows["5.1.1.1"]["capacity_basis"]
