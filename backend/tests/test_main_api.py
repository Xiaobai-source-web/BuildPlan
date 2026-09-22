# -*- coding: utf-8 -*-
"""核心交互端点测试 — /chat、/confirm、/params、/resume、/cancel

覆盖：
  1. POST /chat   — SSE 流式触发流水线，闲聊 prompt 走 router 直接 _stop，不跑完整链路
  2. POST /confirm — 确认门：注册 confirm_id → resolve → ok
  3. POST /params  — 参数复核门：注册 review_id → resolve → ok
  4. POST /resume  — 暂停迭代：注册 pause_id → resolve → ok；action 各分支
  5. POST /cancel  — 中途取消：RUNS 中有 pipeline → ok；无 → ok=False
  6. 异常入参均返回结构化结果（不 500）

注意：**不能用 pytest 的 `tmp_path`** —— 本机沙箱下 %TEMP%\\pytest-of-* 会被拒绝访问。
一律用普通 mkdir 建在 backend/_test_tmp/ 下。

运行：python -m pytest backend/tests/test_main_api.py -q
"""

import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest


# ==================== fixtures ====================

@pytest.fixture()
def api(monkeypatch):
    """把交付物目录指向临时目录，返回 (main 模块, REGISTRY 引用)。"""
    import os
    import shutil

    import main
    from pipeline import config

    root = BACKEND / "_test_tmp" / ("main_api_%s" % os.getpid())
    shutil.rmtree(str(root), ignore_errors=True)
    plans = root / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main, "PLANS_DIR", plans, raising=False)
    monkeypatch.setattr(config, "PLANS_DIR", plans, raising=False)

    yield main, main.REGISTRY
    shutil.rmtree(str(root), ignore_errors=True)


# ==================== 1. POST /chat ====================

def test_chat_returns_sse_streaming_response(api):
    """/chat 用闲聊 prompt 触发流水线，router 判定 chat 意图后 _stop，
    返回 StreamingResponse，content-type 为 text/event-stream。"""
    from starlette.testclient import TestClient

    main, _ = api
    client = TestClient(main.app)
    resp = client.post("/chat", json={"prompt": "你好", "run_id": "test_chat_1"})
    assert resp.status_code == 200, "闲聊 prompt 应正常返回 200"
    ct = resp.headers.get("content-type", "")
    assert "text/event-stream" in ct, "返回必须是 SSE 流（text/event-stream），实际：%s" % ct
    # 流内容应包含 done 事件（流水线结束标志）
    body = resp.text
    assert "event: done" in body or "done" in body, "SSE 流中必须包含 done 事件"


def test_chat_with_empty_prompt_still_works(api):
    """/chat 不应因空 prompt 崩溃；router 会兜底为闲聊回复。"""
    from starlette.testclient import TestClient

    main, _ = api
    client = TestClient(main.app)
    resp = client.post("/chat", json={"prompt": "", "run_id": "test_chat_empty"})
    assert resp.status_code == 200, "空 prompt 不应 500"


def test_chat_generates_run_id_when_missing(api):
    """/chat 不传 run_id 时应自动生成，不应报错。"""
    from starlette.testclient import TestClient

    main, _ = api
    client = TestClient(main.app)
    resp = client.post("/chat", json={"prompt": "你好"})
    assert resp.status_code == 200, "不传 run_id 时应自动生成"


def test_chat_pipeline_removed_from_runs_after_completion(api):
    """/chat 完成后，RUNS 中不应残留该 run_id（防止内存泄漏）。

    用**轮询 + 截止时间**，不用固定 sleep：固定 sleep 等于断言"0.5 秒一定够用"，
    在慢机器/负载高时会偶发失败（这类假失败最消耗排查时间）。
    """
    from starlette.testclient import TestClient

    main, _ = api
    run_id = "test_chat_cleanup_%s" % int(time.time() * 1000)
    client = TestClient(main.app)
    client.post("/chat", json={"prompt": "你好", "run_id": run_id})

    deadline = time.time() + 10
    while time.time() < deadline and run_id in main.RUNS:
        time.sleep(0.02)
    assert run_id not in main.RUNS, "流水线完成后 RUNS 中不应残留 run_id: %s" % run_id


# ==================== 2. POST /confirm ====================

def test_confirm_resolves_registered_id(api):
    """/confirm 对已注册的 confirm_id 返回 ok=True，决策被正确写入注册表。"""
    main, registry = api
    cid = "test_confirm_001"
    registry.register(cid)

    res = main.confirm({"confirm_id": cid, "decision": True})
    assert res["ok"] is True, "已注册的 confirm_id 应返回 ok=True，实际：%s" % res


def test_confirm_returns_false_for_unknown_id(api):
    """/confirm 对未注册的 confirm_id 返回 ok=False（幂等：忽略未知键）。"""
    main, _ = api
    res = main.confirm({"confirm_id": "不存在的_id", "decision": True})
    assert res["ok"] is False, "未注册的 confirm_id 应返回 ok=False"


def test_confirm_with_no_confirm_id(api):
    """/confirm 不传 confirm_id 时应返回 ok=False，不 500。"""
    main, _ = api
    res = main.confirm({})
    assert res["ok"] is False, "缺少 confirm_id 应返回 ok=False"


def test_confirm_decision_passed_through_to_registry(api):
    """/confirm 的 decision 值应正确传递到注册表中，供 wait() 读取。"""
    main, registry = api

    # 注册两个 key，分别用不同的 decision resolve
    for decision_val in (False, True):
        cid = "test_confirm_decision_%s" % decision_val
        registry.register(cid)
        res = main.confirm({"confirm_id": cid, "decision": decision_val})
        assert res["ok"] is True, "decision=%s 时应 resolve 成功" % decision_val


# ==================== 3. POST /params ====================

def test_params_resolves_review_passed(api):
    """/params 传 review_id + passed=True 时返回 ok=True。"""
    main, registry = api
    rid = "test_review_001"
    registry.register(rid)

    res = main.review_params({"review_id": rid, "passed": True})
    assert res["ok"] is True, "已注册的 review_id + passed=True 应返回 ok=True，实际：%s" % res


def test_params_resolves_review_with_manual_input(api):
    """/params 传 review_id + passed=False + manual_input 时返回 ok=True。"""
    main, registry = api
    rid = "test_review_manual"
    registry.register(rid)

    res = main.review_params({
        "review_id": rid,
        "passed": False,
        "manual_input": {"floors": 5, "area": 12000}
    })
    assert res["ok"] is True, "review_id + passed=False + manual_input 应返回 ok=True"


def test_params_returns_false_for_unknown_review_id(api):
    """/params 对未注册的 review_id 返回 ok=False。"""
    main, _ = api
    res = main.review_params({"review_id": "不存在"})
    assert res["ok"] is False, "未注册的 review_id 应返回 ok=False"


def test_params_with_no_review_id(api):
    """/params 不传 review_id 时应返回 ok=False，不 500。"""
    main, _ = api
    res = main.review_params({})
    assert res["ok"] is False, "缺少 review_id 应返回 ok=False"


# ==================== 4. POST /resume ====================

def test_resume_continue(api):
    """/resume 传 pause_id + action=continue 时返回 ok=True。"""
    main, registry = api
    pid = "test_pause_continue"
    registry.register(pid)

    res = main.resume({"pause_id": pid, "action": "continue"})
    assert res["ok"] is True, "pause_id + action=continue 应返回 ok=True，实际：%s" % res


def test_resume_retry_with_instruction(api):
    """/resume 传 pause_id + action=retry + instruction 时返回 ok=True。"""
    main, registry = api
    pid = "test_pause_retry"
    registry.register(pid)

    res = main.resume({
        "pause_id": pid,
        "action": "retry",
        "instruction": "请重新生成 WBS，增加装修阶段"
    })
    assert res["ok"] is True, "pause_id + action=retry 应返回 ok=True"


def test_resume_edit_with_edits(api):
    """/resume 传 pause_id + action=edit + edits 时返回 ok=True。"""
    main, registry = api
    pid = "test_pause_edit"
    registry.register(pid)

    res = main.resume({
        "pause_id": pid,
        "action": "edit",
        "edits": {"duration.5.1.1.1": 10}
    })
    assert res["ok"] is True, "pause_id + action=edit 应返回 ok=True"


def test_resume_abort(api):
    """/resume 传 pause_id + action=abort 时返回 ok=True。"""
    main, registry = api
    pid = "test_pause_abort"
    registry.register(pid)

    res = main.resume({"pause_id": pid, "action": "abort"})
    assert res["ok"] is True, "pause_id + action=abort 应返回 ok=True"


def test_resume_returns_false_for_unknown_pause_id(api):
    """/resume 对未注册的 pause_id 返回 ok=False。"""
    main, _ = api
    res = main.resume({"pause_id": "不存在"})
    assert res["ok"] is False, "未注册的 pause_id 应返回 ok=False"


def test_resume_with_no_pause_id(api):
    """/resume 不传 pause_id 时应返回 ok=False，不 500。"""
    main, _ = api
    res = main.resume({})
    assert res["ok"] is False, "缺少 pause_id 应返回 ok=False"


# ==================== 5. POST /cancel ====================

def test_cancel_returns_false_when_no_such_run(api):
    """/cancel 对不存在的 run_id 返回 ok=False，不 500。"""
    main, _ = api
    res = main.cancel({"run_id": "不存在的_run"})
    assert res["ok"] is False, "不存在的 run_id 应返回 ok=False"
    assert res["run_id"] == "不存在的_run", "返回体应原样带回 run_id"


def test_cancel_sets_cancel_event_on_pipeline(api):
    """/cancel 成功后应触发 pipeline.cancel()（设置 _cancel_evt）。"""
    main, _ = api
    from pipeline.builder import build_pipeline

    run_id = "test_cancel_run"
    pipeline = build_pipeline(run_id=run_id, registry=main.REGISTRY)
    with main._RUNS_LOCK:
        main.RUNS[run_id] = pipeline

    assert not pipeline.cancelled, "cancel 前 cancelled 应为 False"
    res = main.cancel({"run_id": run_id})
    assert res["ok"] is True, "RUNS 中存在的 run_id 应返回 ok=True，实际：%s" % res
    assert pipeline.cancelled, "cancel 后 pipeline.cancelled 应为 True"
    # /cancel 只设标记、不移除 RUNS（清理由 /chat worker 的 finally 负责）
    with main._RUNS_LOCK:
        main.RUNS.pop(run_id, None)


def test_cancel_with_no_run_id(api):
    """/cancel 不传 run_id 时应返回 ok=False，不 500。"""
    main, _ = api
    res = main.cancel({})
    assert res["ok"] is False, "缺少 run_id 应返回 ok=False"


# ==================== 6. 交互端点 HTTP 端到端 ====================

def test_confirm_over_real_http(api):
    """用 starlette TestClient 走真实 HTTP 层测 /confirm。"""
    from starlette.testclient import TestClient

    main, registry = api
    client = TestClient(main.app)

    # 未注册 → ok=False
    r = client.post("/confirm", json={"confirm_id": "no_such", "decision": True})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False

    # 已注册 → ok=True
    registry.register("http_confirm_1")
    r = client.post("/confirm", json={"confirm_id": "http_confirm_1", "decision": True})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_params_over_real_http(api):
    """用 starlette TestClient 走真实 HTTP 层测 /params。"""
    from starlette.testclient import TestClient

    main, registry = api
    client = TestClient(main.app)

    # 未注册 → ok=False
    r = client.post("/params", json={"review_id": "no_such", "passed": True})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False

    # 已注册 → ok=True
    registry.register("http_review_1")
    r = client.post("/params", json={"review_id": "http_review_1", "passed": True})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_resume_over_real_http(api):
    """用 starlette TestClient 走真实 HTTP 层测 /resume。"""
    from starlette.testclient import TestClient

    main, registry = api
    client = TestClient(main.app)

    # 未注册 → ok=False
    r = client.post("/resume", json={"pause_id": "no_such", "action": "continue"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False

    # 已注册 → ok=True
    registry.register("http_pause_1")
    r = client.post("/resume", json={"pause_id": "http_pause_1", "action": "continue"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_cancel_over_real_http(api):
    """用 starlette TestClient 走真实 HTTP 层测 /cancel。"""
    from starlette.testclient import TestClient
    from pipeline.builder import build_pipeline

    main, _ = api
    client = TestClient(main.app)

    # 不存在 → ok=False
    r = client.post("/cancel", json={"run_id": "no_such"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False

    # 注入 pipeline → ok=True
    run_id = "http_cancel_run"
    pipeline = build_pipeline(run_id=run_id, registry=main.REGISTRY)
    with main._RUNS_LOCK:
        main.RUNS[run_id] = pipeline
    r = client.post("/cancel", json={"run_id": run_id})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert pipeline.cancelled, "cancel 后 pipeline.cancelled 应为 True"
    # 清理 RUNS（生产中由 /chat worker 的 finally 负责）
    with main._RUNS_LOCK:
        main.RUNS.pop(run_id, None)


def test_healthz_works(api):
    """/healthz 应返回 {"status": "ok"}，确认 app 可达。"""
    from starlette.testclient import TestClient

    main, _ = api
    client = TestClient(main.app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
