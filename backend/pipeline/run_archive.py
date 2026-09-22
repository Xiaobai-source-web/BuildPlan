# -*- coding: utf-8 -*-
"""运行档案：输入留档 · WBS 树留档 · 计划列表（第 33 轮）

为什么要有这个模块（用户实测提出的三件事）：

1. **用户输入要留档、要编号** —— 用户原话：「用户的输入也要保存下来，如果是文件就保存为
   文件路径，如果是终端直接输入就保存为文本。并编号，给每一份 plan 和 wbs 都标清楚，
   这些内容来自哪份输入。」
   → `save_input()` 把这次运行的原话/文件路径存成 `输入/<input_id>.json`，
     plan 与 wbs 都带 `input_id`，随时能回答"这份计划是哪次输入跑出来的"。

2. **WBS 树要单独留档** —— 用户原话：「WBS树保存也是要做的，同时配套命令能够显示自己
   目前保存了哪些 wbs 树」。
   → `save_wbs()` 把每次运行产出的树存成 `WBS/<run_id>.json`，`/wbs` 能列出来。

3. **第二次打开也要能改计划** —— 需要"列出已有计划"的只读入口。
   → `list_plans()` **纯只读**：不建任何目录。
     ⚠️ 不能用 `PlanStore.load_current()` 之类去枚举：`PlanStore._plan_dir(create=True)`
     会在枚举过程中**创建空档案目录**（实测 `plans/档案/` 下已经有几个空目录就是这么来的）。

目录布局（都在 `backend/plans/` 下，属于运行产物、不进交付包）：

    plans/
      档案/<plan_id>/{基线.json, 当前版本.json, 修订/, 审计记录.json, 模式.json}
      plan_<run_id>.json            # 交付物本体（PlanDeliverNode 落盘）
      输入/<input_id>.json           # 每次运行的输入留档
      WBS/<run_id>.json              # 每次运行的 WBS 树留档

设计原则：**任何一步失败都不许影响主流程**（留档是旁路），所以全部包了 try/except，
只返回状态，不抛异常。
"""

import hashlib
import json
import os
import time
from pathlib import Path

from . import config

def _plans_dir() -> Path:
    """**每次调用都重新读** `config.PLANS_DIR`，绝不按值缓存。

    为什么（实测，2026-09-20）：本模块原先写的是
        `PLANS_DIR: Path = Path(config.PLANS_DIR)`
    —— import 时按值绑定一次。于是 `backend/tests/conftest.py` 里那句
    `monkeypatch.setattr(config, "PLANS_DIR", tmp)` 对本模块**完全无效**：
    每个走 `/chat` 的用例都往**真实**的 `backend/plans/输入/` 写档，
    累积出 **719 个测试垃圾文件**（`test_chat_*` / `t_llm_*`），把用户的真实输入历史淹掉。
    改为按需读取后，"测试隔离"与将来任何"运行时改 PLANS_DIR"的用法都能生效。
    本模块是旁路（全部 try/except、失败不影响主流程），改动零风险。
    """
    return Path(config.PLANS_DIR)
ARCHIVE_NAME = "档案"
INPUTS_NAME = "输入"
WBS_NAME = "WBS"
MODE_NAME = "模式.json"


# ======================================================================
# 基础
# ======================================================================
def _write_json(path, data):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(p))
        return str(p)
    except Exception:
        return ""


def _read_json(path):
    try:
        p = Path(path)
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _stamp():
    return time.strftime("%Y%m%d_%H%M%S")


def input_id_of(text, file_path=None):
    """输入指纹：`in_<时间戳>_<内容哈希前 6 位>`。

    同一段输入重复跑会得到**不同**编号（时间戳不同）——这是有意的：一次运行一份档，
    便于"这份计划是哪次跑的"逐次追溯。
    """
    basis = ("file:" + str(file_path)) if file_path else ("text:" + str(text or ""))
    digest = hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:6]
    return "in_%s_%s" % (_stamp(), digest)


def new_run_id():
    return "run_%d" % int(time.time() * 1000)


# ======================================================================
# ① 输入留档
# ======================================================================
def save_input(text, file_path=None, run_id="", extra=None):
    """把这次运行的输入存成一份档，返回 `input_id`（失败返回 ""）。

    `file_path` 非空 → 按"文件输入"记录（存路径，不复制文件内容）；否则存文本。
    """
    text = str(text or "")
    file_path = str(file_path or "").strip()
    iid = input_id_of(text, file_path or None)
    record = {
        "input_id": iid,
        "run_id": str(run_id or ""),
        "时间": _now(),
        "类型": "文件" if file_path else "文本",
        "文件路径": file_path,
        "文本": text if not file_path else "",
        "字数": len(text),
    }
    if isinstance(extra, dict) and extra:
        record["补充"] = extra
    path = _write_json(_plans_dir() / INPUTS_NAME / ("%s.json" % iid), record)
    return iid if path else ""


def list_inputs(limit=50):
    """最近的输入档（新的在前）。"""
    out = []
    folder = _plans_dir() / INPUTS_NAME
    try:
        files = sorted(folder.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return out
    for p in files[:max(1, int(limit))]:
        rec = _read_json(p)
        if rec:
            out.append(rec)
    return out


def get_input(input_id):
    return _read_json(_plans_dir() / INPUTS_NAME / ("%s.json" % str(input_id or "")))


# ======================================================================
# ② WBS 树留档
# ======================================================================
def _tree_stats(wbs):
    phases = (wbs or {}).get("phases") or []
    wps = sum(len(ph.get("work_packages") or []) for ph in phases)
    leaves = sum(len(wp.get("sub_packages") or [])
                 for ph in phases for wp in (ph.get("work_packages") or []))
    return {"阶段": len(phases), "工作包": wps, "工序": leaves}


def save_wbs(wbs, run_id="", input_id="", params=None, source=""):
    """把一次运行产出的 WBS 树存成一份档，返回保存路径（失败返回 ""）。

    `source`：这棵树是从哪个环节拿到的（`wbs_agent` 组装后 / `beat_build` 分段后 /
    管线结束时的最终树），写进档案便于分辨。
    """
    if not isinstance(wbs, dict) or not (wbs.get("phases") or []):
        return ""
    rid = str(run_id or new_run_id())
    p = _tree_stats(wbs)
    record = {
        "run_id": rid,
        "wbs_id": "wbs_%s" % rid,
        "时间": _now(),
        "来源环节": str(source or ""),
        "input_id": str(input_id or ""),
        "统计": p,
        "参数摘要": _param_digest(params),
        "树": wbs,
    }
    return _write_json(_plans_dir() / WBS_NAME / ("%s.json" % rid), record)


def _param_digest(params):
    """只留几个关键参数，用来判断"这份树是哪套参数跑的"。"""
    p = params if isinstance(params, dict) else {}
    keep = ("building_type", "structure_type", "floors", "building_count",
            "total_area", "planned_start_date")
    return {k: p.get(k) for k in keep if p.get(k) not in (None, "", 0)}


def list_wbs(limit=50):
    """最近的 WBS 档（新的在前，**不含树本体**，只给概要，避免响应过大）。"""
    out = []
    folder = _plans_dir() / WBS_NAME
    try:
        files = sorted(folder.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return out
    for p in files[:max(1, int(limit))]:
        rec = _read_json(p)
        if not rec:
            continue
        out.append({k: rec.get(k) for k in
                    ("run_id", "wbs_id", "时间", "来源环节", "input_id", "统计", "参数摘要")})
    return out


def get_wbs(run_id):
    return _read_json(_plans_dir() / WBS_NAME / ("%s.json" % str(run_id or "")))


# ======================================================================
# ③ 计划列表（**纯只读**：绝不创建目录）
# ======================================================================
def _plan_row(plan_id, source, plan, mtime=None):
    ov = (plan or {}).get("overview") or {}
    meta = (plan or {}).get("meta") or {}
    return {
        "plan_id": plan_id,
        "来源": source,
        "项目": ov.get("project_name") or "",
        "总工期": ov.get("total_duration_days"),
        "工序数": len((plan or {}).get("all_tasks_schedule") or []),
        "版本": meta.get("revision_label") or ("第%d版" % meta.get("revision")
                                              if meta.get("revision") is not None else ""),
        "审计状态": meta.get("audit_status") or "",
        "输入编号": meta.get("input_id") or "",
        "修改时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else "",
    }


def list_plans(limit=30):
    """已有计划概览（新的在前）。**只读**：不 mkdir、不写任何文件。

    两个来源都扫：
      · `plans/档案/<plan_id>/当前版本.json`（有修订链的，优先）；
      · `plans/plan_*.json`（交付物本体，没有档案目录时用）。
    """
    rows = {}
    # 档案目录
    try:
        for d in (_plans_dir() / ARCHIVE_NAME).iterdir():
            if not d.is_dir():
                continue
            for fname in ("当前版本.json", "基线.json"):
                f = d / fname
                if not f.is_file():
                    continue
                plan = _read_json(f)
                if plan:
                    rows[d.name] = _plan_row(d.name, "档案", plan, f.stat().st_mtime)
                    break
    except Exception:
        pass
    # 交付物本体
    try:
        for f in _plans_dir().glob("plan_*.json"):
            pid = f.stem
            if pid in rows:
                continue
            plan = _read_json(f)
            if plan:
                rows[pid] = _plan_row(pid, "交付物", plan, f.stat().st_mtime)
    except Exception:
        pass
    out = list(rows.values())
    out.sort(key=lambda r: r.get("修改时间") or "", reverse=True)
    return out[:max(1, int(limit))]


def load_plan(plan_id):
    """按编号取一份计划（档案当前版优先，其次交付物本体，最后基线）。只读。"""
    pid = str(plan_id or "").strip()
    if not pid:
        return None
    for path in (_plans_dir() / ARCHIVE_NAME / pid / "当前版本.json",
                 _plans_dir() / ("%s.json" % pid),
                 _plans_dir() / ARCHIVE_NAME / pid / "基线.json"):
        plan = _read_json(path)
        if plan:
            return plan
    return None


# ======================================================================
# ④ 终端模式（跨会话记住"我在改哪份计划"）
# ======================================================================
def mode_path():
    """模式文件的路径。

    `BUILDPLAN_MODE_FILE` 可覆盖（第 34 轮加）：模式是**跨会话**状态，测试里如果读真文件，
    前一个用例写的模式会漏进后一个用例（实测踩到：套件里某些用例把模式留成了 plan）。
    测试通过这个环境变量指到临时路径，就互不干扰了。
    """
    override = os.environ.get("BUILDPLAN_MODE_FILE", "").strip()
    if override:
        return Path(override)
    return _plans_dir() / ARCHIVE_NAME / "_session" / MODE_NAME


def save_mode(mode, plan_id="", note=""):
    return _write_json(mode_path(), {"mode": str(mode or "normal"),
                                     "plan_id": str(plan_id or ""),
                                     "note": str(note or ""),
                                     "时间": _now()})


def load_mode():
    rec = _read_json(mode_path()) or {}
    mode = str(rec.get("mode") or "normal")
    if mode not in ("normal", "plan", "revise", "import"):
        mode = "normal"
    return {"mode": mode, "plan_id": str(rec.get("plan_id") or "")}
