# -*- coding: utf-8 -*-
"""第 35 轮性能修复：关掉"思考"（reasoning），并保证换厂商不被它打死

## 实测数据（小米 mimo-v2.5 端点，第 6 步"补全边界条件"）
| 配置 | 耗时 | 输出正文 | 思考 token |
|---|---|---|---|
| 默认（思考开） | 93.7s | 952 字 | 2115 |
| `reasoning_effort=none` | 12.8s | 1023 字 | 0 |

**慢的不是网络、也不是我们的代码，是模型把时间花在了内部思考上**（思考 token 占
输出量的 2/3 以上）。本项目多数模型任务是"读长文、吐短 JSON"的抽取/补全，思考纯开销。

## 本文件钉住三件事
1. 默认**关掉**思考（否则用户又回到 90 秒）；
2. 想开回来时 `LLM_REASONING=high` 能生效；
3. 端点**不认**这个参数（400/422）时必须自动去掉参数重试一次 ——
   否则换成通义/DeepSeek 等不支持该参数的端点会直接报 HTTP 400，整条流水线降级兜底。

运行：python -m pytest backend/tests/test_reasoning_speed.py -q
"""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline.llm import LLMClient, LLMError  # noqa: E402


# ==================== 1. 开关本身 ====================

def test_默认关掉思考(monkeypatch):
    monkeypatch.setattr(config, "REASONING_MODE", "none", raising=False)
    payload = config.reasoning_payload()
    assert payload.get("reasoning_effort") == "none"
    assert payload.get("thinking") == {"type": "disabled"}


@pytest.mark.parametrize("mode,expect", [
    ("high", "high"), ("low", "low"), ("medium", "medium"),
])
def test_想开回来时按档位下发(monkeypatch, mode, expect):
    monkeypatch.setattr(config, "REASONING_MODE", mode, raising=False)
    assert config.reasoning_payload().get("reasoning_effort") == expect
    # 开了思考就不该再带"禁用"的写法（自相矛盾）
    assert "thinking" not in config.reasoning_payload()


@pytest.mark.parametrize("mode", ["none", "off", "false", "0", "no", "disabled", ""])
def test_各种关闭写法都等价(monkeypatch, mode):
    monkeypatch.setattr(config, "REASONING_MODE", mode, raising=False)
    assert config.reasoning_payload().get("reasoning_effort") == "none"


def test_认不出的值就不加参数(monkeypatch):
    monkeypatch.setattr(config, "REASONING_MODE", "也许吧", raising=False)
    assert config.reasoning_payload() == {}


# ==================== 2. 真的注入到请求里 ====================

class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


class _Recorder:
    """假 httpx.Client：记录每次请求体，按脚本回状态码。"""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.bodies = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        self.bodies.append(json)
        code = self.statuses.pop(0) if self.statuses else 200
        if code == 200:
            return _Resp(200, {"choices": [{"message": {"content": "{}"}}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        return _Resp(code, {"error": {"message": "unsupported parameter"}})


def _client_with(monkeypatch, recorder, mode="none"):
    monkeypatch.setattr(config, "REASONING_MODE", mode, raising=False)
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-test", raising=False)
    monkeypatch.setattr("pipeline.llm.httpx.Client", lambda *a, **k: recorder)
    return LLMClient(base_url="https://example.invalid/v1", api_key="sk-test", model="m")


def test_请求体里真的带了关闭思考的参数(monkeypatch):
    rec = _Recorder([200])
    client = _client_with(monkeypatch, rec)
    client._chat("s", "u")
    assert rec.bodies[0].get("reasoning_effort") == "none"


def test_端点不认这个参数时自动退回重试(monkeypatch):
    """换厂商的关键兼容性：不支持该参数时**不能**把整条流水线打死。"""
    rec = _Recorder([400, 200])
    client = _client_with(monkeypatch, rec)
    out = client._chat("s", "u")           # 不该抛
    assert out == "{}"
    assert len(rec.bodies) == 2, "应先带参数试一次、再去掉参数重试一次"
    assert "reasoning_effort" in rec.bodies[0]
    assert "reasoning_effort" not in rec.bodies[1], "第二次必须是不带思考参数的版本"


def test_退回之后记一条提示(monkeypatch):
    """用户有权知道自己在慢速档 —— 否则"为什么这么慢"永远查不出来。"""
    from pipeline import usage

    usage.reset()
    rec = _Recorder([422, 200])
    client = _client_with(monkeypatch, rec)
    client._chat("s", "u")
    assert any("不支持关闭思考" in n for n in usage.meter().notes), usage.meter().notes


def test_服务端错误重试耗尽后上报且不换payload(monkeypatch):
    """5xx 做**有界瞬时重试**，但绝不用"去掉参数再试一次"掩盖真正的服务端故障。

    ⚠️ 第 42 轮改了断言的**期望行为**，意图没变（不许换 payload 遮羞）：
    旧行为是"5xx 一次就上报、不重试"，实测后果是端点间歇 500 时
    `boundary` 节点**一次瞬时错误**就静默退回关键词兜底（labor/equipment/materials 全空、
    交付物里一个字不留）。现在 5xx/429 会退避重试，重试预算用完才上报。
    """
    from pipeline import llm as llm_mod

    monkeypatch.setattr(llm_mod, "_sleep", lambda s: None)   # 别真等 4 秒
    rec = _Recorder([500, 500, 500])
    client = _client_with(monkeypatch, rec)
    with pytest.raises(LLMError) as ei:
        client._chat("s", "u")
    assert "500" in str(ei.value)
    assert len(rec.bodies) == 1 + llm_mod.TRANSIENT_RETRIES, \
        "有界重试：一次调用最多 1+TRANSIENT_RETRIES 次尝试，不许无限重试"
    assert all(b.get("reasoning_effort") == "none" for b in rec.bodies), \
        "5xx 不许靠第二个（去掉思考参数的）payload 蒙过去 —— 每次尝试都必须带参数"


def test_开启思考时不带禁用写法(monkeypatch):
    rec = _Recorder([200])
    client = _client_with(monkeypatch, rec, mode="high")
    client._chat("s", "u")
    assert rec.bodies[0].get("reasoning_effort") == "high"
    assert "thinking" not in rec.bodies[0]


# ==================== 3. 静默降级必须留痕（补边界条件那次踩的坑）====================

def test_模型失败要发警告事件而不是静默降级():
    """用户实测反馈："AI 不返回所缺失的参数了，为什么" —— 因为原来**静默**吞了异常。

    现在至少要发一条 warning 事件，把真实原因（HTTP 状态）带出来。
    """
    from pipeline.nodes.boundary import BoundaryNode

    class _Boom:
        def chat_json(self, *a, **k):
            raise LLMError("LLM HTTP 429: rate limited")

    node = BoundaryNode(llm=_Boom())
    events = []
    node.emit = lambda ev, data: events.append((ev, data))
    node.run({"prompt": "18 层 14200 平", "extracted_params": {"total_area": 14200}})

    warns = [d for ev, d in events if ev == "warning"]
    assert warns, "模型失败必须留痕（不能静默降级）：%s" % events
    assert "429" in json.dumps(warns, ensure_ascii=False), warns
    assert any("关键词" in str(d.get("message") or "") for _ev, d in events)
