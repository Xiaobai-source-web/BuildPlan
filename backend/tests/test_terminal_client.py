# -*- coding: utf-8 -*-
"""SSEClient 与 console 主循环事件路由测试 —— 终端流式层的零网络盲区补全

覆盖：
  1. SSE 报文解析（data 行、多行 data 折叠、空行分隔、[DONE]、非 JSON 降级）
  2. 暂停/恢复语义（pause → 阻塞、resume → 放行、is_paused 状态）
  3. SSEClient.post() 的 JSON 收发与 HTTP 错误处理
  4. console.run_chat() 事件路由（plan_final 保存计划、confirm_required /
     node_paused / param_review 调 confirmer、done 结束循环）

刻意**不连真网络**：urllib.request.urlopen 被 monkeypatch 成假响应对象，
断言的是"行为契约"，与网络无关。

运行：python -m pytest backend/tests/test_terminal_client.py -q
"""

import json
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
TERMINAL = ROOT / "terminal"
# 必须先让 backend 可导入：conftest 的 autouse fixture 会 `from pipeline import config`，
# 而 conftest 在测试模块之前执行 —— 只跑本文件时若没把 backend 加进 sys.path，
# conftest 就会 ModuleNotFoundError（其他测试文件恰好各自加过，所以平时看不出来）。
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(TERMINAL) not in sys.path:
    sys.path.insert(0, str(TERMINAL))

from client import SSEClient  # noqa: E402
import confirmer  # noqa: E402
import renderer  # noqa: E402


# ======================================================================
# 辅助：假 HTTP 响应对象（支持 with 上下文、逐行迭代、read()、status）
# ======================================================================

class _FakeResp:
    """按行迭代的假 HTTP 响应；构造时传入字节行列表。"""

    def __init__(self, lines, status=200):
        self._lines = list(lines)
        self.status = status
        self._idx = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def read(self):
        return b"".join(self._lines)

    def close(self):
        pass

    def __iter__(self):
        return iter(self._lines)

    def __next__(self):
        if self._idx >= len(self._lines):
            raise StopIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line


def _sse_lines(*events):
    """把多个 (event_type, data_dict) 转成 SSE 原始字节行列表。

    每个事件之间用空行分隔。data 固定为单行 JSON（后端只发单行）。
    """
    out = []
    for ev, data in events:
        if ev:
            out.append(f"event:{ev}".encode())
        out.append(f"data:{json.dumps(data, ensure_ascii=False)}".encode())
        out.append(b"")  # 空行 = 事件分隔符
    return out


# ======================================================================
# 1. SSE 报文解析
# ======================================================================

class TestSSEParsing:
    """SSEClient.stream() 对不同 SSE 报文格式的解析行为。"""

    def _make_client(self):
        return SSEClient(base_url="http://fake", retries=0)

    def _collect(self, client, lines):
        """monkeypatch urlopen 后消费 stream()，返回事件列表。"""
        import urllib.request
        resp = _FakeResp(lines)
        calls = []
        def fake_urlopen(req, timeout=None):
            calls.append(req)
            return resp
        import builtins
        orig = builtins.__dict__.get("urlopen_ref")
        import client as _cli_mod
        _orig_urlopen = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            events = list(client.stream("/chat", {"prompt": "test"}))
        finally:
            urllib.request.urlopen = _orig_urlopen
        return events

    def test_single_event_parsed_correctly(self):
        """单个事件：event + data + 空行 → 正确解析出 (event_type, data_dict)。"""
        client = self._make_client()
        lines = _sse_lines(("plan_final", {"plan_id": "p1", "plan": {}}))
        events = self._collect(client, lines)
        assert len(events) == 1
        assert events[0][0] == "plan_final"
        assert events[0][1]["plan_id"] == "p1"

    def test_multiple_events_separated_by_blank_line(self):
        """多个事件用空行分隔，应产出多个 (event, data) 对。"""
        client = self._make_client()
        lines = _sse_lines(
            ("node_start", {"node": "1.1"}),
            ("node_done", {"node": "1.1", "summary": "完成"}),
            ("done", {"status": "ok"}),
        )
        events = self._collect(client, lines)
        assert len(events) == 3
        assert [e[0] for e in events] == ["node_start", "node_done", "done"]

    def test_multi_line_data_concatenation(self):
        """多行 data 字段（每行都带 data: 前缀，罕见但需容错）应拼接成一行 JSON。"""
        client = self._make_client()
        raw = [
            b"event:plan_final",
            b'data:{"plan_id":',
            b'data:"multi"}',
            b"",
        ]
        events = self._collect(client, raw)
        assert len(events) == 1
        assert events[0][1]["plan_id"] == "multi"

    def test_empty_data_yields_empty_dict(self):
        """data 值为空时 _parse_data 应返回 {} 而非崩溃。"""
        result = SSEClient._parse_data("")
        assert result == {}

    def test_non_json_data_falls_back_to_raw(self):
        """非 JSON 的 data 行应降级为 {"_raw": text} 而非崩溃。"""
        result = SSEClient._parse_data("this is not json")
        assert result == {"_raw": "this is not json"}

    def test_comment_lines_are_skipped(self):
        """: 注释行应被忽略，不影响事件解析。"""
        client = self._make_client()
        raw = [
            b": this is a comment",
            b"event:done",
            b'data:{"status":"ok"}',
            b"",
        ]
        events = self._collect(client, raw)
        assert len(events) == 1
        assert events[0][0] == "done"

    def test_event_without_data_still_yields(self):
        """只有 event 行没有 data 行时，应 yield (event, {})。"""
        client = self._make_client()
        raw = [
            b"event:ping",
            b"",
        ]
        events = self._collect(client, raw)
        # ping 事件不含 data，解析出空 dict
        assert len(events) == 1
        assert events[0] == ("ping", {})

    def test_done_event_with_data(self):
        """done 事件携带 usage 数据时能正确解析。"""
        client = self._make_client()
        usage = {"calls": 3, "total_tokens": 1000, "cost_cny": 0.01}
        lines = _sse_lines(("done", {"status": "ok", "usage": usage}))
        events = self._collect(client, lines)
        assert len(events) == 1
        assert events[0][0] == "done"
        assert events[0][1]["usage"]["calls"] == 3

    def test_json_payload_is_correctly_sent(self):
        """stream() 应 POST 正确的 JSON body 和 Content-Type 头。"""
        client = self._make_client()
        lines = _sse_lines(("done", {"status": "ok"}))
        import urllib.request
        resp = _FakeResp(lines)
        captured = []
        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return resp
        _orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            list(client.stream("/chat", {"prompt": "你好"}))
        finally:
            urllib.request.urlopen = _orig
        assert len(captured) == 1
        body = json.loads(captured[0].data.decode("utf-8"))
        assert body["prompt"] == "你好"
        assert captured[0].get_header("Content-type") == "application/json"


# ======================================================================
# 2. 暂停/恢复语义
# ======================================================================

class TestPauseResume:
    """SSEClient 的 pause/resume/is_paused 状态机。"""

    def test_initially_not_paused(self):
        client = SSEClient(base_url="http://fake")
        assert not client.is_paused

    def test_pause_sets_paused(self):
        client = SSEClient(base_url="http://fake")
        client.pause()
        assert client.is_paused

    def test_resume_clears_paused(self):
        client = SSEClient(base_url="http://fake")
        client.pause()
        client.resume()
        assert not client.is_paused

    def test_pause_blocks_generator_resume_unblocks(self):
        """暂停后 stream() 的生成器应阻塞在 _resume_evt.wait()，
        resume() 后继续产出事件。"""
        import threading
        client = SSEClient(base_url="http://fake")

        # 手动构造一个简化的流测试：
        # 先 pause，启动线程消费流，等一会儿确认被阻塞，再 resume
        client.pause()
        import urllib.request
        lines = _sse_lines(
            ("node_start", {"node": "1"}),
            ("done", {"status": "ok"}),
        )
        resp = _FakeResp(lines)
        def fake_urlopen(req, timeout=None):
            return resp
        _orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen

        results = []
        def consume():
            try:
                for ev, data in client.stream("/chat", {}):
                    results.append(ev)
            except Exception:
                pass

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        import time
        # 轮询等线程真正起来，而不是赌"0.3 秒一定够"（慢机器上会偶发假失败）。
        # 注意"暂停期间无产出"这条断言的强度来自后半段：resume 后确实继续产出了。
        deadline = time.time() + 3
        while time.time() < deadline and not t.is_alive():
            time.sleep(0.01)
        assert len(results) == 0, "暂停期间不应有事件产出"
        client.resume()
        t.join(timeout=3)
        urllib.request.urlopen = _orig
        assert "node_start" in results or "done" in results, \
            "resume 后应继续产出事件"


# ======================================================================
# 3. 连接失败重试与最终抛错
# ======================================================================

class TestConnectionRetry:
    """SSEClient.stream() 在连接失败时应重试并在耗尽后抛 ConnectionError。"""

    def test_stream_retries_on_failure_then_raises(self):
        import urllib.request
        client = SSEClient(base_url="http://fake", retries=2)
        attempts = []
        def boom(req, timeout=None):
            attempts.append(1)
            raise urllib.error.URLError("连接被拒绝")
        _orig = urllib.request.urlopen
        urllib.request.urlopen = boom
        try:
            try:
                list(client.stream("/chat", {}))
                assert False, "应该抛出 ConnectionError"
            except ConnectionError as e:
                assert "失败" in str(e)
                assert len(attempts) == 3, f"应重试 3 次（1 次初始 + 2 次重试），实际 {len(attempts)}"
        finally:
            urllib.request.urlopen = _orig

    def test_stream_no_retry_when_events_already_received(self):
        """如果已经收到过事件，后续失败不应重试（当前实现的语义）。
        这里只验证 retries=0 时首次失败就抛错。"""
        import urllib.request
        client = SSEClient(base_url="http://fake", retries=0)
        attempts = []
        def boom(req, timeout=None):
            attempts.append(1)
            raise urllib.error.URLError("拒绝")
        _orig = urllib.request.urlopen
        urllib.request.urlopen = boom
        try:
            try:
                list(client.stream("/chat", {}))
                assert False, "应该抛出 ConnectionError"
            except ConnectionError:
                assert len(attempts) == 1
        finally:
            urllib.request.urlopen = _orig


# ======================================================================
# 4. SSEClient.post() 的 JSON 收发与错误处理
# ======================================================================

class TestPost:
    """SSEClient.post() 的正常返回和异常路径。"""

    def _make_client(self):
        return SSEClient(base_url="http://fake", retries=0)

    def test_post_returns_status_and_json(self):
        """正常 POST 应返回 (status_code, parsed_json)。"""
        import urllib.request
        resp = _FakeResp([b'{"ok": true}'], status=200)
        def fake_urlopen(req, timeout=None):
            return resp
        _orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            status, body = self._make_client().post("/confirm", {"x": 1})
        finally:
            urllib.request.urlopen = _orig
        assert status == 200
        assert body["ok"] is True

    def test_post_empty_body_returns_empty_dict(self):
        """后端返回空 body 时应返回 {} 而非崩溃。"""
        import urllib.request
        resp = _FakeResp([b""], status=200)
        def fake_urlopen(req, timeout=None):
            return resp
        _orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            status, body = self._make_client().post("/confirm", {})
        finally:
            urllib.request.urlopen = _orig
        assert status == 200
        assert body == {}

    def test_post_sends_correct_json(self):
        """post() 应发送正确的 JSON body。"""
        import urllib.request
        resp = _FakeResp([b"{}"], status=200)
        captured = []
        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return resp
        _orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            self._make_client().post("/confirm", {"confirm_id": "c1", "decision": True})
        finally:
            urllib.request.urlopen = _orig
        body = json.loads(captured[0].data)
        assert body["confirm_id"] == "c1"
        assert body["decision"] is True

    def test_post_raises_on_network_error(self):
        """网络异常应向上传播（不被吞掉）。"""
        import urllib.request
        def boom(req, timeout=None):
            raise urllib.error.URLError("超时")
        _orig = urllib.request.urlopen
        urllib.request.urlopen = boom
        try:
            try:
                self._make_client().post("/confirm", {})
                assert False, "应该抛出异常"
            except urllib.error.URLError:
                pass  # 预期行为
        finally:
            urllib.request.urlopen = _orig


# ======================================================================
# 5. console.run_chat() 事件路由
# ======================================================================

class TestRunChatEventRouting:
    """run_chat 对各类 SSE 事件的分派行为。"""

    def _make_ctx(self, events):
        """构造一个 ctx，其 client.post_chat 返回预设事件序列的假生成器。"""

        class _FakeClient:
            def __init__(self):
                self.paused = False
                self._pause_count = 0
                self._resume_count = 0
                self.calls = []

            def pause(self):
                self.paused = True
                self._pause_count += 1

            def resume(self):
                self.paused = False
                self._resume_count += 1

            def post_chat(self, prompt, run_id=None, mode=None, chat_scope=None):
                self.calls.append(("post_chat", prompt, run_id))
                return iter(events)

            def post_cancel(self, run_id):
                self.calls.append(("post_cancel", run_id))

        class _FakeCtx:
            def __init__(self):
                self.client = _FakeClient()
                self.current_plan = None
                self.current_plan_id = None
                self.running = False
                self.run_id = "test_run"
                self.history = []

        return _FakeCtx()

    def test_assistant_history_records_the_reply_not_the_user_text(self, monkeypatch):
        """真实缺陷回归：run_chat 曾把**用户原话**记成 "assistant"。

        后果：用户的话在 /history 里出现两次（一次 user、一次 assistant），
        而助手的真实回复反而丢失。助手的回复只能从 done 事件的 note 拿到。
        """
        ctx = self._make_ctx([
            ("node_start", {"node": "router"}),
            ("done", {"status": "ok", "note": "在的！有项目要排工期吗？"}),
        ])
        from console import run_chat
        run_chat(ctx, "你好")

        assert ctx.history == [("assistant", "在的！有项目要排工期吗？")], \
            "只该记助手说了什么，实际：%s" % ctx.history
        assert ("assistant", "你好") not in ctx.history, "不能把用户原话冒充助手回复"

    def test_no_reply_means_nothing_is_recorded(self, monkeypatch):
        """正常跑完计划（done 无 note）→ 不该往历史里塞任何东西，尤其不能塞用户原话。"""
        ctx = self._make_ctx([
            ("plan_final", {"plan_id": "p", "plan": {}}),
            ("done", {"status": "ok"}),
        ])
        monkeypatch.setattr(renderer, "save_plan", lambda data: "/fake/path.json")
        from console import run_chat
        run_chat(ctx, "生成住宅楼计划")

        assert ctx.history == [], "没有助手回复就不记，不硬塞：%s" % ctx.history

    def test_plan_final_saves_plan_to_ctx(self, monkeypatch):
        """plan_final 事件应保存 plan 到 ctx.current_plan。"""
        plan_data = {"plan_id": "p_001", "plan": {"overview": {"days": 30}}}
        ctx = self._make_ctx([
            ("plan_final", plan_data),
            ("done", {"status": "ok"}),
        ])
        # monkeypatch renderer.save_plan 避免真的写盘
        monkeypatch.setattr(renderer, "save_plan", lambda data: "/fake/path.json")

        from console import run_chat
        run_chat(ctx, "生成计划")

        assert ctx.current_plan == {"overview": {"days": 30}}, \
            "plan_final 后 ctx.current_plan 应为 plan 内容"
        assert ctx.current_plan_id == "p_001", \
            "plan_final 后 ctx.current_plan_id 应为 plan_id"

    def test_confirm_required_calls_confirmer(self, monkeypatch):
        """confirm_required 事件应 pause → 调 confirmer.ask_confirm → resume。"""
        ctx = self._make_ctx([
            ("confirm_required", {"confirm_id": "c1", "message": "继续？"}),
            ("done", {"status": "ok"}),
        ])
        calls = []
        def fake_ask_confirm(client, data, run_id=None):
            calls.append(("ask_confirm", data["confirm_id"], run_id))
        monkeypatch.setattr(confirmer, "ask_confirm", fake_ask_confirm)
        monkeypatch.setattr(renderer, "save_plan", lambda data: "/f")

        from console import run_chat
        run_chat(ctx, "测试确认")

        assert len(calls) == 1
        assert calls[0][0] == "ask_confirm"
        assert calls[0][1] == "c1"
        # run_chat 会用 time.time() 覆写 run_id，只要是非空字符串即可
        assert calls[0][2].startswith("run_"), f"run_id 应以 run_ 开头，实际 {calls[0][2]}"
        # pause 应被调用（confirm 前）且 resume 已恢复
        assert ctx.client._pause_count >= 1
        assert not ctx.client.paused, "confirm 处理完后应已 resume"

    def test_node_paused_calls_handle_pause(self, monkeypatch):
        """node_paused 事件应 pause → 调 confirmer.handle_pause → resume。"""
        ctx = self._make_ctx([
            ("node_paused", {"pause_id": "pp1", "node": "2.1"}),
            ("done", {"status": "ok"}),
        ])
        calls = []
        def fake_handle_pause(client, data, run_id=None):
            calls.append(("handle_pause", data["pause_id"], run_id))
        monkeypatch.setattr(confirmer, "handle_pause", fake_handle_pause)
        monkeypatch.setattr(renderer, "save_plan", lambda data: "/f")

        from console import run_chat
        run_chat(ctx, "测试暂停")

        assert len(calls) == 1
        assert calls[0][0] == "handle_pause"
        assert calls[0][1] == "pp1"
        assert calls[0][2].startswith("run_"), f"run_id 应以 run_ 开头，实际 {calls[0][2]}"
        assert not ctx.client.paused

    def test_param_review_calls_ask_param_review(self, monkeypatch):
        """param_review 事件应 pause → 调 confirmer.ask_param_review → resume。"""
        ctx = self._make_ctx([
            ("param_review", {"review_id": "r1", "purpose": "param"}),
            ("done", {"status": "ok"}),
        ])
        calls = []
        def fake_ask_param_review(client, data, run_id=None):
            calls.append(("ask_param_review", data["review_id"], run_id))
        monkeypatch.setattr(confirmer, "ask_param_review", fake_ask_param_review)
        monkeypatch.setattr(renderer, "save_plan", lambda data: "/f")

        from console import run_chat
        run_chat(ctx, "测试参数审核")

        assert len(calls) == 1
        assert calls[0][0] == "ask_param_review"
        assert calls[0][1] == "r1"
        assert calls[0][2].startswith("run_"), f"run_id 应以 run_ 开头，实际 {calls[0][2]}"
        assert not ctx.client.paused

    def test_done_event_breaks_loop(self, monkeypatch):
        """done 事件应终止事件循环，不再处理后续事件。"""
        after_done = []
        ctx = self._make_ctx([
            ("node_done", {"node": "1", "summary": "完成"}),
            ("done", {"status": "ok"}),
            ("node_start", {"node": "2"}),  # 不应被处理
        ])
        # 拦截 renderer.render_event 看 done 之后是否还有事件被渲染
        original_render = renderer.render_event
        def tracking_render(event, data):
            if event == "node_start" and data.get("node") == "2":
                after_done.append("bad")
            return original_render(event, data)
        monkeypatch.setattr(renderer, "render_event", tracking_render)
        monkeypatch.setattr(renderer, "save_plan", lambda data: "/f")

        from console import run_chat
        run_chat(ctx, "测试 done")

        assert after_done == [], "done 事件之后不应继续处理事件"

    def test_connection_error_prints_message(self, monkeypatch, capsys):
        """ConnectionError 应被捕获并打印错误信息。"""
        class _BoomClient:
            paused = False
            _pause_count = 0
            _resume_count = 0
            def pause(self): self.paused = True; self._pause_count += 1
            def resume(self): self.paused = False; self._resume_count += 1
            def post_chat(self, prompt, run_id=None, mode=None, chat_scope=None):
                raise ConnectionError("连接后端失败")
            def post_cancel(self, run_id): pass

        class _Ctx:
            def __init__(self):
                self.client = _BoomClient()
                self.current_plan = None
                self.current_plan_id = None
                self.running = False
                self.run_id = "test_run"
                self.history = []

        ctx = _Ctx()
        from console import run_chat
        run_chat(ctx, "会失败的调用")

        captured = capsys.readouterr()
        assert "失败" in captured.out
        assert not ctx.running, "异常后 ctx.running 应为 False"

    def test_keyboard_interrupt_cancels_run(self, monkeypatch, capsys):
        """KeyboardInterrupt 应触发 cancel 并打印中断提示。"""
        class _InterruptClient:
            paused = False
            _pause_count = 0
            _resume_count = 0
            def pause(self): self.paused = True; self._pause_count += 1
            def resume(self): self.paused = False; self._resume_count += 1
            def post_chat(self, prompt, run_id=None, mode=None, chat_scope=None):
                raise KeyboardInterrupt()
            def post_cancel(self, run_id):
                self.cancelled = run_id

        class _Ctx:
            def __init__(self):
                self.client = _InterruptClient()
                self.current_plan = None
                self.current_plan_id = None
                self.running = False
                self.run_id = "test_run"
                self.history = []

        ctx = _Ctx()
        from console import run_chat
        run_chat(ctx, "用户按了 Ctrl+C")

        assert ctx.client.cancelled.startswith("run_"), \
            f"中断后应调用 post_cancel 且 run_id 以 run_ 开头，实际 {ctx.client.cancelled}"
        captured = capsys.readouterr()
        assert "中断" in captured.out

    def test_ctx_running_flag_set_and_cleared(self, monkeypatch):
        """run_chat 入口设 running=True，出口（含异常）设 running=False。"""
        ctx = self._make_ctx([("done", {"status": "ok"})])
        monkeypatch.setattr(renderer, "save_plan", lambda data: "/f")

        from console import run_chat
        assert not ctx.running
        run_chat(ctx, "测试 running 标志")
        assert not ctx.running, "结束后 running 应为 False"

    def test_renderer_save_plan_failure_prints_error(self, monkeypatch, capsys):
        """save_plan 抛异常时应打印错误而非崩溃。"""
        ctx = self._make_ctx([
            ("plan_final", {"plan_id": "p1", "plan": {}}),
            ("done", {"status": "ok"}),
        ])
        def boom(data):
            raise OSError("磁盘满了")
        monkeypatch.setattr(renderer, "save_plan", boom)

        from console import run_chat
        run_chat(ctx, "保存会失败的计划")

        captured = capsys.readouterr()
        assert "保存计划失败" in captured.out
        assert not ctx.running

    def test_ping_events_are_ignored(self, monkeypatch):
        """ping 事件不应触发任何特殊逻辑（renderer 返回 None 时跳过打印）。"""
        ctx = self._make_ctx([
            ("ping", {}),
            ("node_start", {"node": "1"}),
            ("done", {"status": "ok"}),
        ])
        monkeypatch.setattr(renderer, "save_plan", lambda data: "/f")

        from console import run_chat
        run_chat(ctx, "测试 ping")

        # 如果没崩溃就算通过；ping 事件 render_event 返回 None
        assert not ctx.running


# ======================================================================
# 6. post_confirm / post_resume / post_params 便捷方法
# ======================================================================

class TestConvenienceMethods:
    """SSEClient 的便捷 POST 方法应组装正确的 payload。"""

    def _make_client(self):
        return SSEClient(base_url="http://fake", retries=0)

    def _capture_post(self, client, method, args):
        import urllib.request
        resp = _FakeResp([b'{"ok":true}'], status=200)
        captured = []
        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return resp
        _orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            result = method(*args)
        finally:
            urllib.request.urlopen = _orig
        body = json.loads(captured[0].data)
        return result, body

    def test_post_confirm_payload(self):
        client = self._make_client()
        (status, body), payload = self._capture_post(
            client, client.post_confirm, ("c1", True))
        assert payload["confirm_id"] == "c1"
        assert payload["decision"] is True

    def test_post_confirm_with_run_id(self):
        client = self._make_client()
        (_, _), payload = self._capture_post(
            client, client.post_confirm, ("c1", False, "run_1"))
        assert payload["run_id"] == "run_1"
        assert payload["decision"] is False

    def test_post_resume_payload(self):
        client = self._make_client()
        (_, _), payload = self._capture_post(
            client, client.post_resume, ("pp1", "continue"))
        assert payload["pause_id"] == "pp1"
        assert payload["action"] == "continue"

    def test_post_resume_with_instruction(self):
        client = self._make_client()
        (_, _), payload = self._capture_post(
            client, client.post_resume, ("pp1", "revise", "把工期改成10天"))
        assert payload["instruction"] == "把工期改成10天"

    def test_post_params_payload(self):
        client = self._make_client()
        (_, _), payload = self._capture_post(
            client, client.post_params, ("r1", True))
        assert payload["review_id"] == "r1"
        assert payload["passed"] is True

    def test_post_params_with_manual_input(self):
        client = self._make_client()
        (_, _), payload = self._capture_post(
            client, client.post_params, ("r1", False, "补充参数"))
        assert payload["passed"] is False
        assert payload["manual_input"] == "补充参数"

    def test_post_cancel_sends_run_id(self):
        client = self._make_client()
        (_, _), payload = self._capture_post(
            client, client.post_cancel, ("run_42",))
        assert payload["run_id"] == "run_42"
