"""MCP 文件读取 + LLM 工具循环测试。

运行：python -m pytest backend/tests/test_mcp.py -v
      （也可直接 python 运行本文件）

stdio 模式会拉起真实 mcp_file_reader.py 子进程（纯标准库）；inprocess 模式零子进程。
"""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import mcp as mcp_mod
from pipeline.mcp import MCPFileTools
from pipeline.llm import LLMClient

# 测试基准：后端目录下的文件（MCP_FILE_ROOT 默认=代码包，backend/ 在其内）
_CFG = "backend/config.py"
_MCP = "backend/pipeline/mcp.py"


# ==================== mcp_file_reader 核心 ====================
def test_server_inprocess_tools():
    m = MCPFileTools(mode="inprocess")
    tools = m.list()
    assert [t["function"]["name"] for t in tools] == ["read_file", "list_dir"]


def test_server_inprocess_read_and_list():
    m = MCPFileTools(mode="inprocess")
    text, err = m.call("read_file", {"path": _CFG, "limit": 80})
    assert err is False and "--- content ---" in text
    text, err = m.call("list_dir", {"path": "backend/pipeline"})
    assert err is False and "mcp.py" in text


def test_server_inprocess_out_of_root():
    m = MCPFileTools(mode="inprocess")
    text, err = m.call("read_file", {"path": "C:/Windows/win.ini"})
    assert err is True and "越权" in text


def test_server_inprocess_missing_file():
    m = MCPFileTools(mode="inprocess")
    text, err = m.call("read_file", {"path": "不存在xyz.txt"})
    assert err is True and "文件不存在" in text


# ==================== stdio 模式（真实 MCP 子进程） ====================
def test_server_stdio_same_result():
    m = MCPFileTools(mode="stdio")
    try:
        tools = m.list()
        assert [t["function"]["name"] for t in tools] == ["read_file", "list_dir"]
        text, err = m.call("read_file", {"path": _CFG, "limit": 80})
        assert err is False and "--- content ---" in text
        text, err = m.call("read_file", {"path": "C:/Windows/win.ini"})
        assert err is True and "越权" in text
    finally:
        m.close()


# ==================== llm.py chat_tools 工具循环 ====================
def test_chat_tools_loop():
    client = LLMClient(api_key="x", base_url="http://localhost:1", model="m")
    seen = []
    seq = [0]

    def fake_post(payload):
        if seq[0] == 0:
            seq[0] = 1
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file",
                              "arguments": json.dumps({"path": _MCP})}}]}}]}
        return {"choices": [{"message": {"content": '{"building_type": "residential"}'}}]}

    client._post = fake_post

    def exec_tool(name, args):
        seen.append((name, args))
        return "文件内容：某住宅项目", False

    tools = [{"type": "function", "function": {
        "name": "read_file", "description": "读文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}}]
    out = client.chat_tools("system", "读文件", tools, exec_tool)
    assert out == '{"building_type": "residential"}'
    assert seen == [("read_file", {"path": _MCP})]


def test_chat_tools_max_rounds():
    """模型一直要求调工具 → 到 max_rounds 返回空串，不死循环。"""
    client = LLMClient(api_key="x", base_url="http://localhost:1", model="m")

    def fake_post(payload):
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}]}}]}

    client._post = fake_post
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    out = client.chat_tools("s", "u", tools, lambda n, a: ("x", False), max_rounds=3)
    assert out == ""


def test_chat_tools_tool_error_fed_back():
    """exec_tool 返回 is_error=True → 错误文本喂回模型，循环继续。"""
    client = LLMClient(api_key="x", base_url="http://localhost:1", model="m")
    seq = [0]

    def fake_post(payload):
        if seq[0] == 0:
            seq[0] = 1
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path":"no.txt"}'}}]}}]}
        msgs = payload["messages"]
        assert any("工具执行失败" in m.get("content", "") for m in msgs if m.get("role") == "tool")
        return {"choices": [{"message": {"content": "ok"}}]}

    client._post = fake_post
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    assert client.chat_tools("s", "u", tools, lambda n, a: ("错误：文件不存在", True)) == "ok"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n全部 {len(tests)} 个 MCP 用例通过 ✔")
