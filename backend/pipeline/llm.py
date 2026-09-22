"""LLM 客户端封装 — qwen-plus（OpenAI 兼容 /chat/completions）

提供：
- chat_text(system, user) -> str   纯文本
- chat_json(system, user) -> dict  要求模型输出合法 JSON 并解析（失败抛 LLMError）
- 重试一次 + 超时
"""

import json
import time

import httpx

from . import config, usage


# ---------------- 瞬时错误重试（有界 + 退避） ----------------
# 为什么需要：本项目实际在用的端点（api.xiaomimimo.com）会**间歇性返回 500**
# （同一 model 同一时刻手工打就是 200）。原来 `_post` 只对"参数不认识"的 4xx 退回
# 重试，5xx 直接抛 —— 一次瞬时 5xx 就让一个节点的模型能力整段丢失：boundary 静默
# 退回关键词兜底，计划里 labor/equipment/materials 全空。
#
# 判据（只认瞬时错误）：
#   · HTTP 5xx（500/502/503/504…服务端临时故障）
#   · HTTP 429（限流）
#   · 网络/超时异常（httpx.HTTPError / OSError，含 ConnectError、ReadTimeout 等）
# **不碰**其它 4xx：参数类 400/404/422 仍走既有的"去掉思考参数再试"逻辑。
TRANSIENT_RETRIES = 2             # 除首次外的重试次数 → 最多 1 + 2 = 3 次尝试
TRANSIENT_BACKOFF_S = (1.0, 3.0)  # 每次重试前的等待（累计最多 4s，远小于 10s 上限）
TRANSIENT_STATUS = (429,)         # 另加所有 5xx，见 _is_transient_status()

_sleep = time.sleep               # 模块级钩子：测试可 monkeypatch，不在函数里硬编码 sleep


def _is_transient_status(status) -> bool:
    """瞬时 HTTP 状态：429 限流 或 任意 5xx。其它 4xx 一律不是。"""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return code in TRANSIENT_STATUS or 500 <= code <= 599


def _is_transient_exc(exc) -> bool:
    """瞬时异常：网络/超时类（httpx.HTTPError 覆盖 TimeoutException、ConnectError
    等；OSError 覆盖底层连接被重置）。其它异常照旧直接抛，不做无谓重试。"""
    return isinstance(exc, (httpx.HTTPError, OSError))


def _backoff_seconds(retry_index: int) -> float:
    """第 retry_index 次重试（0 起）的等待秒数；超出表长就沿用最后一个值。"""
    if not TRANSIENT_BACKOFF_S:
        return 0.0
    return TRANSIENT_BACKOFF_S[min(retry_index, len(TRANSIENT_BACKOFF_S) - 1)]


class LLMError(Exception):
    pass


def _parse_tool_args(raw):
    """工具参数归一：dict 原样返回，字符串按 JSON 解析（容错）。"""
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {"_raw": raw}


class LLMClient:
    def __init__(self, base_url=None, api_key=None, model=None, timeout=None):
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.api_key = api_key or config.LLM_API_KEY
        self.model = model or config.LLM_MODEL
        self.timeout = timeout or config.LLM_TIMEOUT

    # ---------------- 底层 ----------------
    def _post(self, payload) -> dict:
        """POST /chat/completions，返回完整响应 JSON（_chat / chat_tools 共用）。

        这里是**所有 LLM 调用的唯一出口**，因此也是：
          · token 用量与费用的唯一记账点（见 pipeline/usage.py）；
          · "思考开关"的唯一注入点（见下）—— 节点代码一行都不用改。

        **思考开关（第 35 轮，本项目最大的性能修复）**：小米 mimo-v2.5 这类
        思考型模型默认会先生成一大段内部推理，实测第 6 步 93.7s 里有 2115 token
        是思考、正文才 952 字；关掉之后 12.8s（7.3 倍），正文反而更长。
        所以这里统一按 `config.REASONING_MODE`（默认 none）注入；
        端点若**不认**这个参数（400/422），自动去掉参数重试一次，保证兼容性。
        记账失败绝不影响主流程。

        **瞬时错误重试（有界）**：一次瞬时 5xx/429/网络抖动不再等于"这个节点的模型
        能力整段丢失"。判据见 `_is_transient_status` / `_is_transient_exc`，次数与
        退避见 `TRANSIENT_RETRIES` / `TRANSIENT_BACKOFF_S`；每次重试都通过
        `usage.record_retry()` 留痕（`usage.meter().snapshot()["retries"]`）。
        成功路径不变：首次就 200 时只发一次请求、不 sleep。
        """
        if not self.api_key:
            raise LLMError("未配置 QWEN_API_KEY（见 backend/.env.example）")
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        extra = config.reasoning_payload()
        attempt_payloads = []
        if extra:
            attempt_payloads.append(dict(payload, **extra))
            attempt_payloads.append(payload)        # 退回：不带思考参数
        else:
            attempt_payloads.append(payload)
        last_err = None
        attempts = 0            # 真实发出的 HTTP 尝试次数（含首次）
        # 瞬时重试预算是**整个调用共享**的（不是每个 payload 各一份）：
        # 这样"一次 _post 最多多等 4s"是硬上界，与是否走过 4xx 退回路径无关。
        transient_left = TRANSIENT_RETRIES
        for i, body in enumerate(attempt_payloads):
            while True:
                attempts += 1
                with httpx.Client(timeout=self.timeout) as client:
                    try:
                        resp = client.post(url, json=body, headers=headers)
                    except Exception as e:
                        usage.record_failure()
                        # 网络/超时抖动：有预算就退避重试，否则照旧抛出
                        if transient_left > 0 and _is_transient_exc(e):
                            transient_left -= 1
                            usage.record_retry()
                            _sleep(_backoff_seconds(
                                TRANSIENT_RETRIES - transient_left - 1))
                            continue
                        raise
                if resp.status_code == 200:
                    data = resp.json()
                    try:
                        usage.record(self.model, data.get("usage"))  # token 用量 + 费用
                    except Exception:
                        pass
                    if i > 0:
                        # 端点不认思考开关 —— 记一条，否则用户永远不知道自己在慢速档
                        usage.record_note(
                            "本端点不支持关闭思考（reasoning），已退回默认；"
                            "速度可能明显偏慢（可换模型或设置 LLM_REASONING=high 明确开启）")
                    return data
                usage.record_failure()
                last_err = f"LLM HTTP {resp.status_code}: {resp.text[:300]}"
                # 瞬时错误（5xx/429）：有预算就退避重试；重试耗尽后直接报，**不**换 payload
                if transient_left > 0 and _is_transient_status(resp.status_code):
                    transient_left -= 1
                    usage.record_retry()
                    _sleep(_backoff_seconds(TRANSIENT_RETRIES - transient_left - 1))
                    continue
                # 只有"参数不认识"这类错误才值得退回重试（4xx）；5xx 重试耗尽后直接报
                if i + 1 < len(attempt_payloads) and resp.status_code in (400, 404, 422):
                    break       # 换下一个 payload（去掉思考参数）再试一次
                raise LLMError(
                    f"LLM 调用失败（共尝试 {attempts} 次）：{last_err}")
        raise LLMError(f"LLM 调用失败（共尝试 {attempts} 次）：{last_err or '未知错误'}")

    def _chat(self, system, user, temperature=0.3) -> str:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = self._post(payload)
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 响应结构异常：{e} :: {str(data)[:300]}")

    # ---------------- 公开方法 ----------------
    def chat_text(self, system, user, temperature=0.3, retries=1) -> str:
        last = None
        for attempt in range(retries + 1):
            try:
                return self._chat(system, user, temperature)
            except (httpx.HTTPError, LLMError) as e:
                last = e
                if attempt < retries:
                    time.sleep(1)
        raise LLMError(f"LLM 调用失败：{last}")

    def chat_json(self, system, user, temperature=0.3, retries=1) -> dict:
        """要求模型只输出 JSON（在 system 里已声明），解析并返回 dict。"""
        text = self.chat_text(system, user, temperature, retries)
        try:
            return self._extract_json(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"LLM 输出不是合法 JSON：{e}\n原文：{text[:500]}")

    def chat_tools(self, system, user, tools, exec_tool, temperature=0.3,
                   max_rounds=6) -> str:
        """带工具调用的对话（MCP 等工具循环，仿 qwen 本地 chat_mcp.py）。

        - tools: OpenAI function 定义列表
        - exec_tool(name, args) -> (text, is_error)：执行业务工具
        模型请求调工具 → 执行喂回 → 直到给出最终回答；超 max_rounds 返回空串。
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        for _ in range(max_rounds):
            payload = {
                "model": self.model,
                "temperature": temperature,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
            }
            data = self._post(payload)
            try:
                msg = data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as e:
                raise LLMError(f"LLM 响应结构异常：{e} :: {str(data)[:300]}")
            tcs = msg.get("tool_calls") or []
            if not tcs:
                return (msg.get("content") or "").strip()
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": [
                    {"id": tc.get("id") or f"call_{i}", "type": "function",
                     "function": tc.get("function") or {}}
                    for i, tc in enumerate(tcs)
                ],
            })
            for i, tc in enumerate(tcs):
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                args = _parse_tool_args(fn.get("arguments"))
                try:
                    text, is_err = exec_tool(name, args)
                except Exception as e:
                    text, is_err = f"工具执行失败：{e}", True
                if is_err:
                    text = f"工具执行失败：{text}"
                messages.append({"role": "tool",
                                 "tool_call_id": tc.get("id") or f"call_{i}",
                                 "content": text})
        return ""

    @staticmethod
    def _extract_json(text: str) -> dict:
        """容忍模型在 JSON 前后加了 ```json ... ``` 标记或闲聊文字。"""
        # 去掉 ```json ... ``` 包裹
        start = text.find("```")
        if start != -1:
            end = text.find("```", start + 3)
            if end != -1:
                inner = text[start + 3:end].strip()
                if inner.startswith("json"):
                    inner = inner[4:].strip()
                text = inner
        # 从第一个 { 到最后一个 }
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            text = text[s:e + 1]
        return json.loads(text)
