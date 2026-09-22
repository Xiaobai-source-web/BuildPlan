# -*- coding: utf-8 -*-
"""模型档位端点 — /llm、/llm/use、/llm/add、/llm/remove（第 35 轮）

只测**契约与安全**三件事：
  1. key 永不出现在响应里（GET /llm 也一律打码）；
  2. 参数不合法时返回结构化错误，不许 500；
  3. `/chat` 在建流水线之前刷新当前档 —— 这是"不重启后端就能换模型"的唯一支点。

刻意**不连网**：conftest 已把 API key 钉成空字符串，且档位文件被重定向到临时目录；
本文件另外断言"写档位不会影响真实 backend/llm_profiles.json"。

运行：python -m pytest backend/tests/test_main_llm_api.py -q
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

KEY_A = "sk-aaaaaaaaaaaaaaaaaaaa1111"
KEY_B = "sk-bbbbbbbbbbbbbbbbbbbb2222"


@pytest.fixture()
def api(monkeypatch, tmp_path_factory):
    """返回 (main 模块, 档位文件路径)；档位文件指向临时目录。"""
    import main
    from pipeline import config, llm_profiles

    store = tmp_path_factory.mktemp("llmapi") / "profs.json"
    monkeypatch.setenv(llm_profiles.ENV_PATH, str(store))
    # 真实环境变量优先级最高，会把档位顶掉 —— 这些用例要测档位，先清空
    monkeypatch.setattr(config, "_REAL_ENV", {}, raising=False)
    return main, store


def _client(main):
    from starlette.testclient import TestClient

    return TestClient(main.app)


# ==================== GET /llm ====================

def test_没有档位时返回空列表与回退信息(api):
    main, _store = api
    body = _client(main).get("/llm").json()
    assert body["profiles"] == []
    assert body["active"] is None
    assert "fallback" in body and "source" in body, "没有档位也要说清现在用的是什么"
    assert "key_masked" in body["fallback"]


def test_新增档位后列表可见且key打码(api):
    main, _store = api
    c = _client(main)
    r = c.post("/llm/add", json={"name": "千问 · 主力",
                                 "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                                 "model": "qwen-plus", "api_key": KEY_A})
    assert r.status_code == 200
    assert r.json()["key_masked"] == "sk-aa…1111"
    assert KEY_A not in r.text, "响应里绝不许出现完整 key"

    body = c.get("/llm").json()
    assert len(body["profiles"]) == 1
    assert body["profiles"][0]["name"] == "千问 · 主力"
    assert body["profiles"][0]["has_key"] is True
    assert KEY_A not in c.get("/llm").text


def test_两档并存键与端点各自独立(api):
    main, _store = api
    c = _client(main)
    c.post("/llm/add", json={"name": "A", "base_url": "https://a.example/v1",
                             "model": "m-a", "api_key": KEY_A})
    c.post("/llm/add", json={"name": "B", "base_url": "https://b.example/v1",
                             "model": "m-b", "api_key": KEY_B})
    rows = c.get("/llm").json()["profiles"]
    assert [r["model"] for r in rows] == ["m-a", "m-b"]
    assert [r["host"] for r in rows] == ["a.example", "b.example"]
    assert [r["key_masked"] for r in rows] == ["sk-aa…1111", "sk-bb…2222"]


# ==================== POST /llm/use ====================

def test_按序号切换并回显生效来源(api):
    main, _store = api
    c = _client(main)
    c.post("/llm/add", json={"name": "A", "base_url": "https://a.example/v1",
                             "model": "m-a", "api_key": KEY_A})
    c.post("/llm/add", json={"name": "B", "base_url": "https://b.example/v1",
                             "model": "m-b", "api_key": KEY_B})
    body = c.post("/llm/use", json={"key": "2"}).json()
    assert body["ok"] is True
    assert body["active"]["name"] == "B"
    assert body["active"]["model"] == "m-b"
    assert "B" in body["source"], "必须回显是哪个档位在生效"
    assert KEY_B not in json.dumps(body, ensure_ascii=False)

    listed = c.get("/llm").json()
    assert listed["profiles"][1]["active"] is True
    assert listed["profiles"][0]["active"] is False


def test_切换后config真的换了模型(api):
    """`/llm/use` 之后 config 的三个值必须已更新 —— 下一条 /chat 才会用新模型。"""
    main, _store = api
    from pipeline import config

    c = _client(main)
    c.post("/llm/add", json={"name": "B", "base_url": "https://b.example/v1",
                             "model": "m-b", "api_key": KEY_B})
    c.post("/llm/use", json={"key": "1"})
    assert config.LLM_MODEL == "m-b"
    assert config.LLM_BASE_URL == "https://b.example/v1"
    assert config.LLM_API_KEY == KEY_B, "切档必须同时换来那把 key（多 key 的意义）"


def test_切到不存在的档返回404而不是500(api):
    main, _store = api
    r = _client(main).post("/llm/use", json={"key": "99"})
    assert r.status_code == 404
    assert r.json()["error"] == "profile not found"


def test_缺key字段时返回404而不是崩(api):
    main, _store = api
    r = _client(main).post("/llm/use", json={})
    assert r.status_code == 404


# ==================== POST /llm/add ====================

def test_新增必填缺失时400(api):
    main, _store = api
    c = _client(main)
    for bad in ({}, {"base_url": "https://a.example/v1"}, {"model": "m"}):
        r = c.post("/llm/add", json=bad)
        assert r.status_code == 400, bad
        assert "base_url" in r.json()["error"]


def test_新增时可以顺手切过去(api):
    main, _store = api
    from pipeline import config

    c = _client(main)
    body = c.post("/llm/add", json={"name": "A", "base_url": "https://a.example/v1",
                                    "model": "m-a", "api_key": KEY_A,
                                    "use": True}).json()
    assert body["switched"] is True
    assert body["total"] == 1
    assert config.LLM_MODEL == "m-a"
    assert c.get("/llm").json()["active"]["name"] == "A"


def test_新增不带use时不改变当前档(api):
    main, _store = api
    c = _client(main)
    c.post("/llm/add", json={"name": "A", "base_url": "https://a.example/v1",
                             "model": "m-a", "api_key": KEY_A, "use": True})
    c.post("/llm/add", json={"name": "B", "base_url": "https://b.example/v1",
                             "model": "m-b", "api_key": KEY_B})
    assert c.get("/llm").json()["active"]["name"] == "A", "只加不切，当前档不许被动"


def test_没有key也允许建档(api):
    """本地模型（Ollama）常常不需要 key，所以 key 不是必填。"""
    main, _store = api
    body = _client(main).post("/llm/add", json={
        "name": "本地", "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b"}).json()
    assert body["ok"] is True
    rows = _client(main).get("/llm").json()["profiles"]
    assert rows[0]["has_key"] is False


# ==================== POST /llm/remove ====================

def test_删档后列表变短(api):
    main, _store = api
    c = _client(main)
    c.post("/llm/add", json={"name": "A", "base_url": "https://a.example/v1",
                             "model": "m-a", "api_key": KEY_A})
    c.post("/llm/add", json={"name": "B", "base_url": "https://b.example/v1",
                             "model": "m-b", "api_key": KEY_B})
    body = c.post("/llm/remove", json={"key": "1"}).json()
    assert body["removed"]["name"] == "A" and body["total"] == 1
    assert [r["name"] for r in c.get("/llm").json()["profiles"]] == ["B"]


def test_删掉当前档后退回env基线且不清空配置(api):
    """关键：删掉当前档不许把 key/端点清成空（那会让下一条消息直接报错）。"""
    main, _store = api
    from pipeline import config

    c = _client(main)
    c.post("/llm/add", json={"name": "A", "base_url": "https://a.example/v1",
                             "model": "m-a", "api_key": KEY_A, "use": True})
    before = (config.LLM_BASE_URL, config.LLM_MODEL)
    c.post("/llm/remove", json={"key": "1"})
    assert (config.LLM_BASE_URL, config.LLM_MODEL) == before, \
        "无档时刷新必须是 no-op，否则会把基线打回默认值"
    assert c.get("/llm").json()["active"] is None


# ==================== /chat 的刷新时机 ====================

def test_chat之前会刷新当前档(api, monkeypatch):
    """`/chat` 必须**在建流水线之前**刷新，否则切档只对再下一条消息生效。"""
    main, _store = api
    from pipeline import config

    seen = {}
    real_refresh = config.refresh_active_profile
    real_build = main.build_pipeline

    def spy_refresh(*a, **k):
        seen["refresh"] = True
        return real_refresh(*a, **k)

    def spy_build(*a, **k):
        seen["built_after_refresh"] = seen.get("refresh", False)
        return real_build(*a, **k)

    monkeypatch.setattr(config, "refresh_active_profile", spy_refresh)
    monkeypatch.setattr(main, "build_pipeline", spy_build)

    resp = _client(main).post("/chat", json={"prompt": "你好", "run_id": "t_llm_refresh"})
    assert resp.status_code == 200
    assert seen.get("refresh") is True, "/chat 必须刷新模型档位"
    assert seen.get("built_after_refresh") is True, "刷新必须发生在建流水线之前"


def test_删档之后chat仍然能跑(api):
    """档位文件被删光后 /chat 不许 500（配置是旁路，坏了也得能聊天）。"""
    main, store = api
    c = _client(main)
    c.post("/llm/add", json={"name": "A", "base_url": "https://a.example/v1",
                             "model": "m-a", "api_key": KEY_A, "use": True})
    c.post("/llm/remove", json={"key": "1"})
    store.write_text("{ 坏掉的 JSON", encoding="utf-8")
    resp = c.post("/chat", json={"prompt": "你好", "run_id": "t_llm_broken"})
    assert resp.status_code == 200
    assert "event: done" in resp.text or "done" in resp.text


def test_写档位不会碰真实配置文件(api):
    """兜底断言：本文件所有写入都必须落在临时路径，不能污染用户真实档位。"""
    main, store = api
    _client(main).post("/llm/add", json={"name": "A", "base_url": "https://a.example/v1",
                                         "model": "m-a", "api_key": KEY_A})
    assert store.exists(), "应写到被重定向的临时路径"
    from pipeline import llm_profiles

    real = llm_profiles.DEFAULT_FILE
    if real.exists():
        assert KEY_A not in real.read_text(encoding="utf-8"), "真实档位文件被写脏了"
