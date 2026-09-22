"""项目文件加载节点 —— 流水线前端读取本地项目文件，注入下游 LLM

放在 router/work_confirm（进入工作计划）之后、extractor 之前：
- 从 prompt 识别本地文件路径（文本 / .docx），用 MCP（MCPFileTools）读取并汇总到
  ctx["doc_content"]、ctx["doc_files"]，作为后续参数提取与下游生成 LLM 的资料源。
- 未检测到文件 → 人工确认门：终端问「输入是否为全部项目数据？[Y/n]」；
  Y → 无文件沿用输入；N → 用户贴文件路径，MCP 读该路径。

复用现有 `param_review` 交互通道（事件 + /params 端点），用 purpose="doc" 区分，
终端按 purpose 显示相应提示。零新增 endpoint。
"""

import os
import uuid

from ..base import BaseNode
from ..events import EV_PARAM_REVIEW
from ..registry import GATE_TIMEOUT_SECONDS
from ..mcp import MCPFileTools
from ..mcp_file_reader import _read_docx_text
from .boundary import is_abort_decision
from .extractor import detect_local_files


def _read_direct(path):
    """绕过 MCP 沙盒：用户显式给出的本地文件路径直接读取（docx 解包纯文本）。

    返回文本；路径不存在/是目录/二进制 → None。仅用于 MCP 越权或读取失败时的兜底。
    """
    if not path or not os.path.exists(path) or not os.path.isfile(path):
        return None
    try:
        if path.lower().endswith(".docx"):
            return _read_docx_text(path)
        with open(path, "rb") as fh:
            raw = fh.read(4 * 1024 * 1024)
        if raw[:4096].find(b"\x00") != -1:      # 疑似二进制
            return None
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


class DocLoadNode(BaseNode):
    name = "doc_load"
    title = "项目文件加载"

    # ---------------- 入口 ----------------
    def run(self, ctx):
        prompt = (ctx.get("prompt") or "").strip()

        # 1) 优先：从 prompt 自动识别本地文件
        files = detect_local_files(prompt)
        if files:
            content, ok_files = self._read_via_mcp(files)
            if content:
                ctx["doc_content"] = content
                ctx["doc_files"] = ok_files
                self.done_summary = f"已加载项目文件 {len(ok_files)} 个"
                self.emit("node_progress", {"node": self.name, "progress": 100,
                                            "message": "正在读你的项目文件"})
                return {"doc_content": content, "doc_files": ok_files}
            # 自动识别了但读不到 → 回退交互询问

        # 2) 未命中文件/读取失败 → 人工确认门
        # 区分两种情况（原来混在一起，导致"识别到了但读不到"被说成"未检测到"，
        # 用户看不出是自己的路径写错了还是真没给文件）：
        # 措辞已按用户反馈收敛：**先讲清"我为什么问你"，再给编号选项**——
        # 老版把 files/unreadable 这类原始字段名甩给用户，实测"看不懂"。
        unreadable = [f for f in files if f] if files else []
        if unreadable:
            self.emit("node_progress", {"node": self.name, "progress": 40,
                                        "message": "你给的路径读不到内容，问一下你"})
            gate_msg = "给了文件路径，但读不到内容"
            reason = "unreadable"
        else:
            self.emit("node_progress", {"node": self.name, "progress": 40,
                                        "message": "没找到项目文件，确认一下数据齐不齐"})
            gate_msg = "输入里没有文件路径，确认数据是否齐全"
            reason = "no_path"
        review_id = f"dl_{getattr(self, '_run_id', 'run')}_{uuid.uuid4().hex[:4]}"
        self._registry.register(review_id)
        self.emit(EV_PARAM_REVIEW, {
            "review_id": review_id,
            "purpose": "doc",
            "message": gate_msg,
            "params": {"files": files,
                       "unreadable": unreadable,
                       "reason": reason},
        })
        decision = self._registry.wait(
            review_id, cancel_evt=getattr(self, "_cancel_evt", None), timeout=GATE_TIMEOUT_SECONDS)

        # ⚠️ 门上的提示写着「③ 输入 /abort → 中止本次运行」，所以**手输 /abort 也必须真的
        # 中止**。老实现只认 `action == "abort"`，于是 `/abort` 掉进下面的 manual_input
        # 被当成**文件路径**去读，读不到就"沿用输入数据"继续跑 —— 用户以为停了，其实没停。
        if is_abort_decision(decision):
            self.done_summary = "用户在文件加载门选择中止"
            return {"_stop": "用户在项目文件加载环节中止了本次运行"
                             "（输入 /help 可看用法；重新描述项目即可再开始）"}

        if decision.get("passed") is True:
            ctx["doc_content"] = None
            ctx["doc_files"] = []
            self.done_summary = "用户确认输入为全部数据，未加载文件"
            return {"doc_content": None, "doc_files": []}

        # N → manual_input 视为文件路径
        path = (decision.get("manual_input") or "").strip()
        if path:
            catched = self._read_via_mcp([path])
            if catched[0]:
                ctx["doc_content"] = catched[0]
                ctx["doc_files"] = [path]
                self.done_summary = f"已按用户路径加载文件：{path}"
                self.emit("node_progress", {"node": self.name, "progress": 100,
                                            "message": "按你给的路径读到了项目文件"})
                return {"doc_content": catched[0], "doc_files": [path]}
        ctx["doc_content"] = None
        ctx["doc_files"] = []
        self.done_summary = "未成功读取用户指定的文件，沿用输入数据"
        return {"doc_content": None, "doc_files": []}

    # ---------------- MCP 读取 ----------------
    def _read_via_mcp(self, files):
        """批量读取本地文件（文本/.docx），返回 (汇总文本, 成功列表)。

        MCP 走沙盒根（MCP_FILE_ROOT）内的文件；用户显式给出的路径若在沙盒外
        被「越权」拒绝，则降级为直接读该路径（本地单用户工具，信任用户自给的路径）。
        """
        parts, ok = [], []
        mcp = MCPFileTools()
        try:
            for f in files:
                text = None
                try:
                    text, is_err = mcp.call("read_file", {"path": f})
                    if is_err:
                        text = None
                except Exception:
                    text = None
                if not text:
                    text = _read_direct(f)      # MCP 沙盒外/失败 → 直接读兜底
                if text:
                    parts.append(f"### 项目文件：{f}\n{text}")
                    ok.append(f)
        finally:
            mcp.close()
        if not parts:
            return None, []
        return "\n\n".join(parts), ok