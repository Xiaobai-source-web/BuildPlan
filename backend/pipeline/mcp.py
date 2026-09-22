"""MCP 文件读取客户端 — 仿 qwen3.8-27b本地部署/chat_mcp.py。

给流水线 LLM 提供 read_file / list_dir 两个文件工具：
- **stdio 模式**（默认，源码运行）：子进程拉起 mcp_file_reader.py，走真实 MCP stdio 协议；
- **in-process 模式**（打包/frozen 或 MCP_MODE=inprocess 时）：不拉子进程，直接进程内
  调用文件读取逻辑 —— 打包成 exe/zip 无需自带 Python 解释器跑 .py，部署最简。

零第三方依赖，纯标准库。服务器不可用一律优雅降级（list()→[]、call()→错误文本）。
"""

import importlib.util
import json
import os
import subprocess
import sys

from . import config

_SERVER_NAME = "mcp_file_reader.py"
DEFAULT_ROOT = str(config.KB_DIR.parent)  # 代码包目录（安全边界）

_MODE = os.environ.get("MCP_MODE", "auto").strip().lower()


def _server_path():
    """定位 mcp_file_reader.py；兼容 PyInstaller 打包（数据文件在 sys._MEIPASS）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, _SERVER_NAME),
        os.path.join(getattr(sys, "_MEIPASS", ""), "pipeline", _SERVER_NAME),
        os.path.join(getattr(sys, "_MEIPASS", ""), _SERVER_NAME),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return candidates[0]


class McpClient:
    """极简 MCP stdio 客户端（覆盖 initialize / tools/list / tools/call）。"""

    def __init__(self, cmd, cwd, env=None):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  cwd=cwd, env=env)
        self._id = 0

    def _write(self, obj):
        self.p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def _req(self, method, params=None):
        self._id += 1
        self._write({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("MCP 服务器提前退出")
            msg = json.loads(line)
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"MCP 错误：{msg['error']}")
                return msg["result"]

    def notify(self, method, params=None):
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def init(self):
        self._req("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                 "clientInfo": {"name": "haizhizi-mcp", "version": "0.1"}})
        self.notify("notifications/initialized")

    def list_tools(self):
        return self._req("tools/list").get("tools", [])

    def call_tool(self, name, arguments):
        r = self._req("tools/call", {"name": name, "arguments": arguments})
        text = "".join(c.get("text", "") for c in r.get("content", []))
        return text, bool(r.get("isError"))

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=3)
        except Exception:
            self.p.kill()


def to_openai_tools(mcp_tools):
    """MCP tools 定义 -> OpenAI function 参数（qwen-plus / llama.cpp 通用）。"""
    out = []
    for t in mcp_tools:
        schema = t.get("inputSchema") or {"type": "object", "properties": {}}
        out.append({"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": schema,
        }})
    return out


class MCPFileTools:
    """文件读取工具（read_file / list_dir），双模式：stdio 子进程 or 进程内直调。

    mode: "auto"（默认：frozen/打包 → inprocess，否则 stdio）/ "stdio" / "inprocess"
    用法：
        mcp = MCPFileTools()
        tools = mcp.list()            # OpenAI function 定义
        text, is_err = mcp.call("read_file", {"path": "..."})
        mcp.close()
    """

    def __init__(self, root=None, mode="auto"):
        self.root = root or DEFAULT_ROOT
        self.mode = mode or _MODE
        self._client = None     # stdio 子进程客户端
        self._server = None     # in-process 模块
        self._tools = None

    # ---------------- 懒加载 ----------------
    def _ensure(self):
        if self._tools is not None:
            return
        mode = self.mode
        if mode == "auto":
            mode = "inprocess" if getattr(sys, "frozen", False) else "stdio"
        if mode == "stdio":
            try:
                self._client = McpClient(
                    [sys.executable, _server_path()], cwd=config.KB_DIR.parent,
                    env={**os.environ, "MCP_FILE_ROOT": self.root})
                self._client.init()
                self._tools = self._client.list_tools()
                return
            except Exception:
                self._close_stdio()
                mode = "inprocess"
        # in-process：直接把服务器模块当库调用（打包场景无需子进程）
        try:
            os.environ["MCP_FILE_ROOT"] = self.root
            spec = importlib.util.spec_from_file_location("_mcp_file_reader", _server_path())
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._server = mod
            self._tools = mod.TOOLS
        except Exception:
            self._tools = []

    # ---------------- 对外接口 ----------------
    def list(self):
        """OpenAI function 定义列表；服务器不可用返回 []。"""
        self._ensure()
        return to_openai_tools(self._tools or [])

    def call(self, name, args):
        """执行工具，返回 (text, is_error)。业务错误经 isError 返回给模型自行调整。"""
        self._ensure()
        known = {t.get("name") for t in (self._tools or [])}
        if not self._tools or name not in known:
            return f"未知工具：{name}（可用：{', '.join(sorted(known)) or '无'}）", True
        if self._server is not None:
            r = self._server.tool_result(name, args)
            text = "".join(c.get("text", "") for c in r.get("content", []))
            return text, bool(r.get("isError"))
        return self._client.call_tool(name, args)

    def close(self):
        self._close_stdio()
        self._server = None
        self._tools = None

    def _close_stdio(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
