"""通用 SSE 流式客户端（零第三方依赖）— T-02

按 v1.1 §5.1 帧规范实现：
- data 强制单行 JSON（此处兼容多行 data 折叠的容错，但生产后端只发单行）
- 事件间空行分隔
- 流终止统一用 `done` 事件（不使用 OpenAI 风格 [DONE]）
- 忽略 `ping` 事件与 `:` 注释行；未知事件不崩溃

关键能力：stream() 返回**可暂停生成器**（pause/resume）：
终端收到 confirm_required / node_paused 时先 pause()（不再 readline，
socket 保持连接），由 confirmer 处理后端交互 POST，再 resume() 继续读
同一连接——避免单线程读流+等待输入的死锁（§5.3）。
"""

import json
import threading
import time
import urllib.error
import urllib.request

# ---- SSE 事件类型（与 backend/pipeline/events.py 保持一致）----
EV_NODE_START = "node_start"
EV_NODE_PROGRESS = "node_progress"
EV_NODE_DONE = "node_done"
EV_NODE_PAUSED = "node_paused"
EV_CONFIRM_REQUIRED = "confirm_required"
EV_PARAM_REVIEW = "param_review"
EV_PLAN_FINAL = "plan_final"
EV_ERROR = "error"
EV_DONE = "done"
EV_PING = "ping"

# 需要暂停等待人工介入的事件
INTERACTIVE_EVENTS = (EV_CONFIRM_REQUIRED, EV_NODE_PAUSED, EV_PARAM_REVIEW)


class SSEClient:
    """一个后端实例的连接客户端。base_url 形如 http://localhost:8000"""

    def __init__(self, base_url="http://localhost:8000", timeout=60, retries=2):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self._resume_evt = threading.Event()
        self._resume_evt.set()  # 初始未暂停
        self._paused = False

    # ------------------------------------------------------------------
    # 暂停/恢复（供消费方在 interactive 事件处调用）
    # ------------------------------------------------------------------
    def pause(self):
        """暂停读取：之后的 next() 会阻塞，直到 resume()。"""
        self._paused = True
        self._resume_evt.clear()

    def resume(self):
        """恢复读取。"""
        self._paused = False
        self._resume_evt.set()

    @property
    def is_paused(self):
        return self._paused

    # ------------------------------------------------------------------
    # 底层 SSE 流
    # ------------------------------------------------------------------
    def stream(self, path, payload):
        """POST path 并逐事件读取，产出 (event, data_dict)。

        在 pause() 之后、resume() 之前，迭代会阻塞在此处，不消费 socket。
        连接级失败会重试（仅当尚未收到任何事件时才有意义）。
        """
        url = self.base_url + path
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                )
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
        else:
            raise ConnectionError(f"连接后端 {self.base_url} 失败：{last_err}")

        try:
            event = None
            buf = []
            for raw in resp:
                self._resume_evt.wait()  # 暂停期间阻塞，不读 socket
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if event is not None:
                        yield event, self._parse_data("\n".join(buf))
                        event = None
                        buf = []
                    continue
                if line.startswith(":"):
                    continue  # 注释行
                if line.startswith("event:"):
                    event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    buf.append(line[len("data:"):].strip())
        except GeneratorExit:
            pass
        finally:
            resp.close()

    @staticmethod
    def _parse_data(text):
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------
    def post_chat(self, prompt, run_id=None, endpoint="/chat", mode=None, chat_scope=None):
        payload = {"prompt": prompt}
        if run_id:
            payload["run_id"] = run_id
        if mode:
            # 第 34 轮：模式由终端决定（用户手选），后端不再做意图识别
            payload["mode"] = mode
        if chat_scope:
            # 第 35 轮：改计划模式下"基于当前计划聊天"要把口径也告诉后端，
            # 否则后端按**普通模式**的口径回答（会答错模式，实测踩到）
            payload["chat_scope"] = chat_scope
        return self.stream(endpoint, payload)

    def post(self, path, payload):
        """普通 JSON POST（用于 /confirm /resume /cancel）。"""
        url = self.base_url + path
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(body) if body.strip() else {}

    def post_confirm(self, confirm_id, decision, run_id=None):
        payload = {"confirm_id": confirm_id, "decision": decision}
        if run_id:
            payload["run_id"] = run_id
        return self.post("/confirm", payload)

    def post_params(self, review_id, passed, manual_input=None, run_id=None):
        """参数人工复核门决策：passed=True 采信；False 且 manual_input 非空作为补充。"""
        payload = {"review_id": review_id, "passed": bool(passed)}
        if manual_input:
            payload["manual_input"] = manual_input
        if run_id:
            payload["run_id"] = run_id
        return self.post("/params", payload)

    def post_resume(self, pause_id, action, instruction=None, edits=None, run_id=None,
                    repair_key=None):
        """暂停门决策。repair_key：一键修复的编号选项（规格 §3 的冻结上行契约）。

        带 repair_key 时 `instruction` 同时是该选项的 label —— 后端**不认识**
        repair_key 的老路径收到的就是一条具体意见（退化成自由意见，不会崩）。
        """
        payload = {"pause_id": pause_id, "action": action}
        if instruction:
            payload["instruction"] = instruction
        if edits:
            payload["edits"] = edits
        if repair_key:
            payload["repair_key"] = repair_key
            # 契约要求：选了编号时 manual_input 同时填该选项的 label
            payload["manual_input"] = instruction or repair_key
        if run_id:
            payload["run_id"] = run_id
        return self.post("/resume", payload)

    def post_cancel(self, run_id):
        return self.post("/cancel", {"run_id": run_id})

    # ---------------- 自然语言修改（「改得动」）----------------
    def post_revise(self, plan_id, instruction):
        """一句话改计划。返回 (status, {summary/applied/rejected/plan...})。"""
        return self.post("/revise", {"plan_id": plan_id, "instruction": instruction})

    def get(self, path):
        """普通 JSON GET（用于修订链 / 计划读取）。"""
        url = self.base_url + path
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(body) if body.strip() else {}

    def get_versions(self, plan_id):
        return self.get("/plans/%s/versions" % plan_id)

    # ---------------- 已有计划 / WBS 档 / 输入档 / 模式（第 33 轮）----------------
    def list_plans(self):
        """已有计划概览（只读）。"""
        return self.get("/plans")

    def list_wbs(self):
        """已留档的 WBS 树概览（只读）。"""
        return self.get("/wbs")

    def list_inputs(self):
        """已留档的输入（只读）。"""
        return self.get("/inputs")

    def get_mode(self):
        """读终端模式（跨会话）。"""
        return self.get("/mode")

    def post_mode(self, mode, plan_id=""):
        """保存终端模式。"""
        return self.post("/mode", {"mode": mode, "plan_id": plan_id})

    def post_baseline(self, plan_id, plan):
        """把一份计划落成"基线"（幂等：已有基线不覆盖），供 /versions、/undo 使用。"""
        return self.post("/plans/%s/baseline" % plan_id, {"plan": plan})

    def post_revise_preview(self, plan_id, instruction):
        """**只看不改**：算出这句话会改什么，但不落盘、不进修订链（第 34 轮）。"""
        return self.post("/revise", {"plan_id": plan_id, "instruction": instruction,
                                     "dry_run": True})

    # ---------------- 模型档位：多 key / 随时换模型（第 35 轮）----------------
    def list_llm_profiles(self):
        """当前档 + 全部档位（后端已把 key 打码）。"""
        return self.get("/llm")

    def use_llm_profile(self, key):
        """切换到某一档（序号 / 名字 / id）。切换后**下一次**运行即生效。"""
        return self.post("/llm/use", {"key": str(key)})

    def add_llm_profile(self, name, base_url, model, api_key, note="", use=False):
        """新增一档；use=True 时顺便切过去。"""
        return self.post("/llm/add", {"name": name, "base_url": base_url, "model": model,
                                      "api_key": api_key, "note": note, "use": bool(use)})

    def remove_llm_profile(self, key):
        """删除一档。"""
        return self.post("/llm/remove", {"key": str(key)})

    def post_undo(self, plan_id):
        return self.post("/plans/%s/undo" % plan_id, {})

    def post_goto(self, plan_id, version):
        return self.post("/plans/%s/goto" % plan_id, {"version": int(version)})
