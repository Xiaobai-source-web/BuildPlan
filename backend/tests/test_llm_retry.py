# -*- coding: utf-8 -*-
"""`_post()` 的**有界瞬时错误重试**（真实故障修复）

## 真实故障（必须堵住）
一条真实流水线里 `boundary` 节点调用模型失败，**静默退回关键词兜底**，导致计划里
labor / equipment / materials **全空**（而手工复现同一次调用是成功的）。

根因在 `pipeline/llm.py::_post`：**只有"参数不被端点认识"（400/404/422）才退回重试，
5xx 直接抛**。而本项目实际在用的端点（`api.xiaomimimo.com`）会**间歇性返回 500**
（同一 model 同一时刻手工打就是 200）。于是"一次瞬时 5xx"就让一个节点的模型能力
整段丢失。

## 本文件钉住的契约
1. **只重试瞬时错误**：HTTP 5xx、HTTP 429、网络/超时异常；
2. **有界 + 退避**：最多 `1 + TRANSIENT_RETRIES` 次尝试，退避通过模块级 `_sleep`
   调用（测试里可替换），总等待远小于 10s；
3. **既有 4xx 行为不变**：参数类 400/404/422 仍走"去掉思考参数再试一次"的老逻辑，
   且**不**算瞬时重试、**不** sleep；
4. **成功路径不变**：首次就 200 时只发一次请求、不 sleep；
5. **留痕**：重试次数进 `usage.meter().retries`（`snapshot()["retries"]`）；
6. **失败文案**：重试耗尽后错误信息含**尝试次数**与**最后一次真实状态码/响应片段**。

**不联网**：照 `backend/tests/test_reasoning_speed.py` 的写法，monkeypatch
`pipeline.llm.httpx.Client` 成一个脚本化的假客户端。

运行：python -m pytest backend/tests/test_llm_retry.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
import pytest  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline import llm as llm_mod  # noqa: E402
from pipeline import usage  # noqa: E402
from pipeline.llm import (TRANSIENT_BACKOFF_S, TRANSIENT_RETRIES,  # noqa: E402
                          LLMClient, LLMError)


# ==================== 假 httpx（脚本化响应，绝不联网） ====================

class _Resp:
    """最小 httpx.Response 替身：只需要 status_code / text / json()。"""

    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _Recorder:
    """假 httpx.Client：记录每次请求体，按脚本回状态码 / 抛异常。"""

    def __init__(self, script):
        # script 元素：int 状态码，或 Exception 实例（模拟网络/超时异常）
        self.script = list(script)
        self.bodies = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        self.bodies.append(json)
        code = self.script.pop(0) if self.script else 200
        if isinstance(code, BaseException):
            raise code
        if code == 200:
            return _Resp(200, {"choices": [{"message": {"content": "{}"}}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        return _Resp(code, {"error": {"message": "服务端临时故障"}})


def _setup(monkeypatch, script, mode="none"):
    """装好假 httpx + 假 `_sleep` + 干净的 meter，返回 (client, recorder, slept)。"""
    rec = _Recorder(script)
    slept = []
    monkeypatch.setattr(config, "REASONING_MODE", mode, raising=False)
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-test", raising=False)
    monkeypatch.setattr(llm_mod.httpx, "Client", lambda *a, **k: rec)
    monkeypatch.setattr(llm_mod, "_sleep", lambda s: slept.append(s))
    usage.reset()
    client = LLMClient(base_url="https://example.invalid/v1",
                       api_key="sk-test", model="m")
    return client, rec, slept


# ==================== 1. 瞬时 5xx：重试一次就成功 ====================

def test_第一次500第二次200_最终成功且重试计数为1(monkeypatch):
    client, rec, slept = _setup(monkeypatch, [500, 200])

    assert client._chat("s", "u") == "{}"
    assert len(rec.bodies) == 2, "500 之后必须再打一次：%s" % len(rec.bodies)
    assert usage.meter().retries == 1, usage.meter().retries
    assert usage.meter().snapshot()["retries"] == 1, usage.meter().snapshot()
    assert slept == [TRANSIENT_BACKOFF_S[0]], "退避要走模块级 _sleep：%s" % slept
    assert usage.meter().failed_calls == 1, "失败的尝试仍要记一次 failed_calls"


def test_连续两次500第三次200_重试两次后成功(monkeypatch):
    """瞬时抖动可能连续两下 —— 预算内必须扛住。"""
    client, rec, slept = _setup(monkeypatch, [500, 503, 200])

    assert client._chat("s", "u") == "{}"
    assert len(rec.bodies) == 3, rec.bodies
    assert usage.meter().retries == 2
    assert slept == [TRANSIENT_BACKOFF_S[0], TRANSIENT_BACKOFF_S[1]], slept


# ==================== 2. 重试耗尽：有界 + 说清原因 ====================

def test_连续500_抛出且尝试次数为1加TRANSIENT_RETRIES(monkeypatch):
    client, rec, slept = _setup(monkeypatch, [500, 500, 500, 500, 500])

    with pytest.raises(LLMError) as ei:
        client._chat("s", "u")

    msg = str(ei.value)
    assert len(rec.bodies) == 1 + TRANSIENT_RETRIES, \
        "总尝试次数必须是 1 + TRANSIENT_RETRIES：%s" % len(rec.bodies)
    assert str(1 + TRANSIENT_RETRIES) in msg, "错误信息必须含尝试次数：%s" % msg
    assert "500" in msg, "错误信息必须含最后一次的真实状态码：%s" % msg
    assert "服务端临时故障" in msg, "错误信息必须含响应片段：%s" % msg
    assert usage.meter().retries == TRANSIENT_RETRIES, usage.meter().retries
    assert len(slept) == TRANSIENT_RETRIES, slept


def test_重试次数与退避总量都是有限的(monkeypatch):
    """不许无限重试、不许让调用者等超过约 10 秒（预算是整个调用共享的）。"""
    assert 0 <= TRANSIENT_RETRIES <= 3, TRANSIENT_RETRIES
    assert sum(TRANSIENT_BACKOFF_S) <= 10.0, TRANSIENT_BACKOFF_S

    client, rec, slept = _setup(monkeypatch, [502] * 10)
    with pytest.raises(LLMError):
        client._chat("s", "u")
    assert len(rec.bodies) == 1 + TRANSIENT_RETRIES
    assert sum(slept) < 10.0, "总等待必须远小于 10s：%s" % sum(slept)
    # 5xx 不许退回"不带思考参数"的 payload 当遮羞布（也就不会 3×2=6 次尝试）
    for body in rec.bodies:
        assert body.get("reasoning_effort") == "none", body


def test_瞬时预算跨payload共享_不与4xx退回叠乘(monkeypatch):
    """预算共享：500 → 400（退回）→ 500 → 500 时，总尝试数不许"两段各自 3 次"叠乘。

    脚本：payload0 先 500（用掉 1 次重试）→ 400（退回不带思考参数）→ payload1 两次 500
    （用掉第 2 次重试后耗尽）⇒ 共 4 次尝试、退避只有 1.0 + 3.0 两段。
    """
    client, rec, slept = _setup(monkeypatch, [500, 400, 500, 500])

    with pytest.raises(LLMError) as ei:
        client._chat("s", "u")

    assert len(rec.bodies) == 4, "预算必须共享：%s" % len(rec.bodies)
    assert usage.meter().retries == TRANSIENT_RETRIES, usage.meter().retries
    assert slept == [TRANSIENT_BACKOFF_S[0], TRANSIENT_BACKOFF_S[1]], slept
    assert "500" in str(ei.value), str(ei.value)


# ==================== 3. 429 限流：同样算瞬时 ====================

def test_429_会重试(monkeypatch):
    client, rec, slept = _setup(monkeypatch, [429, 200])

    assert client._chat("s", "u") == "{}"
    assert len(rec.bodies) == 2, "429 必须重试：%s" % len(rec.bodies)
    assert usage.meter().retries == 1
    assert slept == [TRANSIENT_BACKOFF_S[0]]


def test_429_重试耗尽也报尝试次数(monkeypatch):
    client, rec, _ = _setup(monkeypatch, [429] * 5)
    with pytest.raises(LLMError) as ei:
        client._chat("s", "u")
    assert len(rec.bodies) == 1 + TRANSIENT_RETRIES
    assert "429" in str(ei.value), str(ei.value)


# ==================== 4. 网络/超时异常：也算瞬时 ====================

def test_网络超时异常会重试(monkeypatch):
    client, rec, slept = _setup(
        monkeypatch, [httpx.ConnectTimeout("timed out"), 200])

    assert client._chat("s", "u") == "{}"
    assert len(rec.bodies) == 2, "网络抖动必须重试：%s" % len(rec.bodies)
    assert usage.meter().retries == 1
    assert slept == [TRANSIENT_BACKOFF_S[0]]


def test_非网络异常照旧直接抛不重试(monkeypatch):
    """非瞬时异常不许被本次改动放大成"重试 3 次"。"""
    client, rec, slept = _setup(monkeypatch, [ValueError("bad payload"), 200])

    with pytest.raises(ValueError):
        client._chat("s", "u")
    assert len(rec.bodies) == 1, rec.bodies
    assert usage.meter().retries == 0
    assert slept == []


# ==================== 5. 既有 4xx 逻辑：一个字都不许变 ====================

@pytest.mark.parametrize("code", [400, 404, 422])
def test_参数类4xx仍走去掉思考参数的既有逻辑(monkeypatch, code):
    """既有兼容性逻辑：端点不认 reasoning 参数 → 去掉参数再试一次。

    本次改动**只加**瞬时重试，不允许把这两个机制混在一起（400 不算瞬时、
    不许 sleep、不许把 400 重试成 3 次）。
    """
    client, rec, slept = _setup(monkeypatch, [code, 200])

    assert client._chat("s", "u") == "{}"
    assert len(rec.bodies) == 2, "必须恰好两次：带参数 → 去掉参数：%s" % len(rec.bodies)
    assert rec.bodies[0].get("reasoning_effort") == "none"
    assert "reasoning_effort" not in rec.bodies[1], rec.bodies[1]
    assert usage.meter().retries == 0, "参数类 4xx 不是瞬时错误，不许计入重试"
    assert slept == [], "参数类 4xx 不许走退避 sleep"


def test_4xx_没有可退回的payload时立即抛(monkeypatch):
    """没有思考参数可去（reasoning_payload() 为空）时，400 照旧一次就抛。"""
    client, rec, slept = _setup(monkeypatch, [400, 200], mode="也许吧")

    with pytest.raises(LLMError) as ei:
        client._chat("s", "u")
    assert len(rec.bodies) == 1, "不该重试：%s" % rec.bodies
    assert "400" in str(ei.value), str(ei.value)
    assert usage.meter().retries == 0
    assert slept == []


def test_401_这类非瞬时错误不重试(monkeypatch):
    client, rec, slept = _setup(monkeypatch, [401, 200])
    with pytest.raises(LLMError) as ei:
        client._chat("s", "u")
    assert len(rec.bodies) == 1, rec.bodies
    assert "401" in str(ei.value)
    assert usage.meter().retries == 0
    assert slept == []


# ==================== 6. 成功路径行为不变 ====================

def test_成功路径只调一次且不sleep(monkeypatch):
    client, rec, slept = _setup(monkeypatch, [200])

    assert client._chat("s", "u") == "{}"
    assert len(rec.bodies) == 1, "成功路径只许调一次：%s" % len(rec.bodies)
    assert slept == [], "成功路径不许 sleep：%s" % slept
    assert usage.meter().retries == 0
    assert usage.meter().failed_calls == 0
    assert usage.meter().calls == 1, "成功路径照旧记一次用量"
    assert usage.meter().snapshot()["retries"] == 0


def test_chat_json_也享受同一层重试(monkeypatch):
    """所有调用都走 `_post`，所以 chat_json/chat_tools 自动获得同一层保护。"""
    client, rec, _ = _setup(monkeypatch, [500, 200])
    assert client.chat_json("s", "只输出 JSON") == {}
    assert len(rec.bodies) == 2
    assert usage.meter().retries == 1


def test_reset_会清零重试计数(monkeypatch):
    client, rec, _ = _setup(monkeypatch, [500, 200])
    client._chat("s", "u")
    assert usage.meter().retries == 1
    usage.reset()
    assert usage.meter().retries == 0
    assert usage.meter().snapshot()["retries"] == 0


if __name__ == "__main__":
    print(__doc__)
    print("请用 pytest 运行（需要 monkeypatch fixture）")
