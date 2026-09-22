# -*- coding: utf-8 -*-
"""计划存档模块 —— 基线 + 修订链（审计轨迹 + 任意版本回退）

核心思想（为什么不每次存全量快照）：
  一个计划档案 = 一个目录 = **1 份基线 + N 条修订（patch）**。
  每条修订只记"改了什么"（patch 是可序列化字典），不记整份计划；
  `rebuild()` 用「基线 → 按序重放前 N 条 patch」得到任意历史版本。
  这样既省空间，又天然形成审计轨迹：谁在第几轮说了什么话、改了什么、
  影响范围与重算摘要是哪几条，全都留在目录里。

目录布局（全部 UTF-8，目录不存在自动创建）：
  <root>/<plan_id>/基线.json        第一次落盘的全量计划（重建的起点，不随修订变化）
  <root>/<plan_id>/当前版本.json    当前生效的计划全量（= rebuild(upto=最新) 的结果）
  <root>/<plan_id>/修订记录/001.json 每条修订一条
                                    {时间, 用户原话, patch, 影响范围, 重算摘要}
  <root>/<plan_id>/审计记录.json     {"audit_status": "未审计",
                                      "events": [{时间, 轮次, 动作, 说明}]}

降级原则（本模块只依赖标准库）：
  - 任何损坏的 JSON 都不抛异常：跳过、记录到"损坏文件"清单、历史继续可用；
  - 找不到 target 的 patch 不算失败：`apply_patch` 标 applied=False + warning，
    返回 changed_ids=[]，让上层把"没打中"如实回报给用户；
  - 不用 tempfile.mkdtemp（本机沙箱下它创建的目录后续写入会被拒绝），
    写盘用"直接写 + 失败重试"，全部用普通目录。

Python 3.8 兼容：不使用 `X | None` 标注。
"""

import datetime
import json
import math
import os
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

CURRENT_NAME = "当前版本.json"
BASELINE_NAME = "基线.json"
REVISIONS_DIR_NAME = "修订记录"
AUDIT_NAME = "审计记录.json"

# apply_patch 支持的字段 → 作用对象
# `name`（改一条工序的名字）与 `quantity` 并列：改的是叶子自身，但改名必须**连带**
# 同步 all_tasks_schedule / critical_path_tasks 里的 task_name —— 那两张表是排程时
# 反写出来的冗余副本，改名故意不重排，若不一起改，看板/Word 里还是旧名字，
# 用户会觉得"改了却没生效"。
LEAF_FIELDS = ("quantity", "duration", "norm", "crew", "name")
# 计划级字段（第 35 轮补 `plan_title`）：改的是 plan["meta"] 与总览标题，不动树。
# 为什么补：用户实测「我想将项目名称改为 NUS 大楼」在修改模式里**改不了** ——
# 旧实现只支持工序级字段，于是这句话被当成闲聊丢给模型，模型又编了个已删除的
# `/edit` 命令。计划名称、计划细度这些是用户最自然的"我要改"对象。
# `start_date`（开工日期）/ `target_duration`（目标总工期）同理：它们是用户最常说的
# 「开工推迟两周」「总工期压到 300 天」，但都不该由本函数触发重排 —— 本函数只负责
# 把意图记进计划与 meta，要不要重算由调用方（revise 流程）决定。
META_FIELDS = ("level", "cost", "segment", "plan_title", "start_date", "target_duration")
# 结构级字段：就地增 / 删叶子，会改树与依赖图，最"重"的一类。
STRUCT_FIELDS = ("add_task", "remove_task")
SUPPORTED_FIELDS = LEAF_FIELDS + META_FIELDS + STRUCT_FIELDS
# 改"计划名"时用这个 target（不是工序编号，但预览/清单都要显示得有名字）
PLAN_TARGET = "plan"


# ==================== 小工具 ====================
def _now() -> str:
    """本地时间字符串，秒级，人类可读又可直接排序。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _ensure_dir(path) -> str:
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def _read_json(path) -> Optional[Any]:
    """读 JSON；文件不存在 / 损坏 / 不是对象 → 返回 None（不抛异常）。"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError, UnicodeDecodeError):
        return None


def _write_json(path, data) -> str:
    """UTF-8 写 JSON；先写 .tmp 再改名，避免半截文件。"""
    _ensure_dir(os.path.dirname(path))
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        os.replace(tmp, path)
    except OSError:
        # 个别文件系统跨设备 / 占用时退回直接写
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.remove(tmp)
        except OSError:
            pass
    return path


def _finite(x) -> Optional[float]:
    """数值归一：能转成有限 float 就返回，否则 None（NaN / inf / 非法都算 None）。"""
    try:
        if isinstance(x, bool):
            return None
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


# ==================== 计划树遍历 ====================
def _phases(plan) -> List[Dict]:
    if not isinstance(plan, dict):
        return []
    wbs = plan.get("wbs")
    if not isinstance(wbs, dict):
        return []
    phases = wbs.get("phases")
    return phases if isinstance(phases, list) else []


def iter_leaves(plan) -> List[Dict]:
    """列举计划里的全部叶子任务（三级 sub_packages；二级无子项时二级即叶子）。"""
    leaves = []
    for phase in _phases(plan):
        if not isinstance(phase, dict):
            continue
        for wp in phase.get("work_packages") or []:
            if not isinstance(wp, dict):
                continue
            subs = wp.get("sub_packages")
            if isinstance(subs, list) and subs:
                for sub in subs:
                    if isinstance(sub, dict):
                        leaves.append(sub)
            else:
                leaves.append(wp)
    return leaves


def _iter_places(plan):
    """产出 (阶段, 工作包, 叶子, 叶子所在列表, 下标, 叶子是否就是工作包)。

    多带一个 `叶子是否就是工作包` 是**必需的**：二级工作包自己没有 sub_packages 时，
    它是把自己的列表当容器的（见下面的 [wp]）——那是个一次性的临时列表，往里 insert
    等于把新任务扔进垃圾桶。调用方必须知道这种情况才能改插到"同级工作包"里。
    """
    for phase in _phases(plan):
        if not isinstance(phase, dict):
            continue
        for wp in phase.get("work_packages") or []:
            if not isinstance(wp, dict):
                continue
            subs = wp.get("sub_packages")
            if isinstance(subs, list) and subs:
                for i, sub in enumerate(subs):
                    if isinstance(sub, dict):
                        yield phase, wp, sub, subs, i, False
            else:
                yield phase, wp, wp, [wp], 0, True


def _find_leaf_slot(plan, target):
    """按 id 找叶子，返回 (叶子, 所在列表, 下标, 叶子是否就是工作包, 阶段, 工作包)。

    `find_leaf_place` 只是它的 3 元组视图（老调用方接口不变）。
    """
    if target is None:
        return None
    tid = str(target)
    for phase, wp, leaf, holder, idx, wp_leaf in _iter_places(plan):
        if str(leaf.get("id")) == tid:
            return leaf, holder, idx, wp_leaf, phase, wp
    return None


def find_leaf(plan, target) -> Optional[Dict]:
    """按 id 找叶子；找不到返回 None。"""
    if target is None:
        return None
    tid = str(target)
    for leaf in iter_leaves(plan):
        if str(leaf.get("id")) == tid:
            return leaf
    return None


def find_leaf_place(plan, target) -> Optional[Tuple[Dict, List, int]]:
    """按 id 找叶子的 (叶子, 所在列表, 下标)，支持 step 3 的原地增删。"""
    slot = _find_leaf_slot(plan, target)
    if slot is None:
        return None
    return slot[0], slot[1], slot[2]


def dependency_closure(dependencies, seed_ids) -> List[str]:
    """下游闭包：从 seed 出发，沿 dependencies 的 后继→后继的后继… 全量展开。

    - 入参 dependencies: [{"predecessor":..,"successor":.., "type":.., "lag_days":..}]
      也容忍 {"from"/"to"} 这类别名；
    - 返回**含 seed 自身**、去重、按发现顺序（广度优先）排列的 id 列表。
      顺序稳定 → 便于断言 "affected == [A, B, C]"。
    """
    adj = {}  # predecessor -> [successor, ...]（保序去重）
    for dep in dependencies or []:
        if not isinstance(dep, dict):
            continue
        pred = dep.get("predecessor") or dep.get("from") or dep.get("src")
        succ = dep.get("successor") or dep.get("to") or dep.get("dst")
        if pred is None or succ is None:
            continue
        pred, succ = str(pred), str(succ)
        bucket = adj.setdefault(pred, [])
        if succ not in bucket:
            bucket.append(succ)

    result = []          # list 保序 + set 去重
    seen = set()
    queue = []
    for sid in seed_ids or []:
        sid = str(sid)
        if sid not in seen:
            seen.add(sid)
            queue.append(sid)
            result.append(sid)
    head = 0
    while head < len(queue):
        cur = queue[head]
        head += 1
        for nxt in adj.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
                result.append(nxt)
    return result


# ==================== 日期 / 新编号的小工具 ====================
def _parse_date(value):
    """严格 YYYY-MM-DD → date；解析不了返回 None（不抛异常）。"""
    if not isinstance(value, str):
        return None
    try:
        time.strptime(value.strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    try:
        return datetime.date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except (ValueError, IndexError):
        return None


def _add_days(day, days):
    """日期 + 天数；非法/越界返回 None（由调用方决定怎么降级），**绝不抛异常**。

    与 `_shift_date` 的区别：`_shift_date` 失败时原样返回，调用方分不清"加成功了
    但天数是 0"和"加越界了"；这里失败就是 None，判断得清楚。
    """
    if day is None:
        return None
    try:
        return day + datetime.timedelta(days=days)
    except (OverflowError, ValueError, OSError):
        return None


def _shift_date(value, days: int):
    """把 "YYYY-MM-DD" 平移 days 天；不是合法日期就原样返回（不猜、不炸）。

    ⚠️ 越界必须吞掉：`days` 是调用方给的（可能来自计划里的 `total_duration_days`，
    实测可以是 1e9），而日期上限是 9999-12-31 —— `date + timedelta` 会抛
    OverflowError，把一次"改开工日期"变成 500。本模块的底线是"任何输入都不抛异常"。
    """
    old = _parse_date(value)
    if old is None:
        return value
    try:
        return (old + datetime.timedelta(days=days)).isoformat()
    except (OverflowError, ValueError, OSError):
        return value


def _ids_and_plan(plan):
    """(计划里**全部**编号集合, 计划对象) —— 撞号检测的唯一依据。

    ⚠️ 必须包含**工作包编号**，不能只看叶子：一个工作包一旦有了 sub_packages，它自己
    就不再是叶子（`iter_leaves` 只产出子项）。若只拿叶子比，新任务会撞上这个工作包的
    编号 —— 实测产生 `1.3` 两个（一个是既有工作包，一个是新建的工作包），排程与依赖
    表按编号认任务，会把它们当成同一条。
    """
    ids = set()
    for leaf in iter_leaves(plan):
        if isinstance(leaf, dict) and leaf.get("id") is not None:
            ids.add(str(leaf.get("id")))
    for phase in _phases(plan):
        for wp in (phase.get("work_packages") or []):
            if isinstance(wp, dict) and wp.get("id") is not None:
                ids.add(str(wp.get("id")))
    return ids


def _next_in_group(plan, prefix, group=None) -> str:
    """组内撞号时往后顺延：prefix="4.1.2" 且它已被占用 → "4.1.3"，直到空位。

    与 `_next_child_id` 的区别：这里**不动前缀**，只在同一条编号分支上加数字。
    撞号时若改用别的前缀，用户要的 "4.1.2" 会被悄悄改成 "4.1.5"（另一个小组的号）。

    group 显式给出时按它加数字（prefix 本身的数字只当起点）：工作包编号 "5.1" 的
    上级是 "5"，若拿 head 当组就会生成 "5.2" —— 那是**另一个阶段**的工作包编号。
    """
    existing = _ids_and_plan(plan)
    tail = prefix.rsplit(".", 1)[-1]
    try:
        n = int(tail)
    except (TypeError, ValueError):
        n = 0
    head = group if group is not None else prefix.rpartition(".")[0]
    while True:
        n += 1
        cand = "%s.%d" % (head, n) if head else str(n)
        if cand not in existing:
            return cand


def _next_child_id(plan, parent_id, siblings, anchor_id=None) -> str:
    """在 `siblings` 这一层里算出下一个不冲突的编号。

    前缀**不取工作包的编号**，而要取**同级任务的父编号**：真实计划里工作包是 "4.1"，
    子任务却是 "4.1.1.1"（分 4.1.1 / 4.1.2 若干组）。若按工作包编号生成 "4.1.5"，
    它在编号体系里看着就是另一个工作包，用户按编号根本找不到刚加的那条。

    anchor_id 是"插在谁后面"的那条任务：优先跟着**它这一组**（同一父编号）编号，
    这样 4.1.1.4 后面来的新任务自然是 4.1.1.5，而不是跨组跳到 4.1.4（那可能是别组的
    编号，甚至已被 4.1.4.1 占用）。同级给不出可用前缀时退回 parent_id（工作包编号）。
    """
    existing = _ids_and_plan(plan)
    first_parent, first_suffix = "", 0
    numeric = []
    for sib in siblings or []:
        if not isinstance(sib, dict):
            continue
        sid = str(sib.get("id") or "")
        if not sid:
            continue
        head, _sep, tail = sid.rpartition(".")
        try:
            n = int(tail) if tail else 0
        except (TypeError, ValueError):
            continue
        numeric.append((head or sid, n))       # 末段不是数字 → 当成顶层，n=0
        if not first_parent:
            first_parent, first_suffix = head or sid, n
    anchor_parent = ""
    if anchor_id:
        anchor_parent = str(anchor_id).rpartition(".")[0]
    if anchor_parent and any(h == anchor_parent for h, _n in numeric):
        prefix = anchor_parent
    elif first_parent:
        prefix = first_parent
    else:
        prefix = parent_id
    top = first_suffix
    for head, n in numeric:
        if head == prefix and n > top:
            top = n
    n = top + 1
    while True:
        cand = "%s.%d" % (prefix, n) if prefix else str(n)
        # 编号在整份计划里必须唯一：与任何已有叶子撞号都顺延（跨工作包也可能撞）。
        if cand not in existing:
            return cand
        n += 1


def _next_wp_sibling_id(plan, host_wp, wp_siblings) -> str:
    """工作包本身就是任务时（没有 sub_packages），给同级工作包算下一个编号。

    同级的 "1.1"、"1.2" → 新工作包 "1.3"；只有一个 "1.1" → "1.2"。
    这里的前缀是**父编号**（"1"），末段数字取最大值 —— 与子任务编号同一套规则，
    只是层级不同。
    """
    existing = _ids_and_plan(plan)
    host_id = str((host_wp or {}).get("id") or "")
    prefix = host_id.rpartition(".")[0] or host_id
    top = 0
    for sib in wp_siblings or []:
        if not isinstance(sib, dict):
            continue
        sid = str(sib.get("id") or "")
        head, _sep, tail = sid.rpartition(".")
        if (head or sid) != prefix:
            continue
        try:
            n = int(tail) if tail else 0
        except (TypeError, ValueError):
            continue
        if n > top:
            top = n
    n = top + 1
    while True:
        cand = "%s.%d" % (prefix, n) if prefix else str(n)
        if cand not in existing:
            return cand
        n += 1


def _shift_schedule_dates(plan, days: int):
    """把计划里**所有**日期表一起平移 days 天，返回 `(改动条数, 是否全部平移成功)`。

    这是"开工日期变了"的唯一外在体现方式：逐条任务的日期表（看板甘特/Word）读的
    就是它，不一起平移会出现"计划 1 月开工、甘特还画在 3 月"的自相矛盾。

    ⚠️ 早年只平了 `all_tasks_schedule`，于是同一个计划里三处日期互相打架：
    任务表已经到 3 月了，关键路径表与里程碑还写着 1 月（实测过）。凡是带日期的
    冗余表都必须一起平移：
      · all_tasks_schedule   start_date / finish_date
      · critical_path_tasks  start_date / finish_date（看板的关键路径高亮读它）
      · key_milestones       date（Word 封面与里程碑页读它）

    ⚠️ 第二，**不许"平移一半"**：日期上限是 9999-12-31，把开工日期改到 9999-12-31
    再往后平移就会越界。早先的实现吞掉溢出、原样留下旧值，于是返回的日程里
    `finish_date` 早于 `start_date`、竣工日期早于开工日期，而 `apply_patch` 照样报
    `applied=True` 且**没有任何提示** —— 用户拿到一份自相矛盾的计划还以为改成功了。
    现在把"有没有失败"如实返回，由调用方拒绝整条修改。
    """
    if not days:
        return 0, True

    def _shift_rows(rows, keys):
        if not isinstance(rows, list):
            return 0, True
        n, ok = 0, True
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in keys:
                raw = row.get(key)
                if not raw:
                    continue
                old = _parse_date(raw)
                if old is None:
                    continue            # 本来就不是合法日期：不动它，也不算失败
                new = _add_days(old, days)
                if new is None:
                    ok = False          # 越界：调用方必须据此拒绝整条修改
                    continue
                row[key] = new.isoformat()
                n += 1
        return n, ok

    n1, ok1 = _shift_rows(plan.get("all_tasks_schedule"), ("start_date", "finish_date"))
    n2, ok2 = _shift_rows(plan.get("critical_path_tasks"), ("start_date", "finish_date"))
    n3, ok3 = _shift_rows(plan.get("key_milestones"), ("date",))
    return n1 + n2 + n3, (ok1 and ok2 and ok3)


# ==================== patch 应用（纯函数：不改入参） ====================
def _reject(plan, patch, reason) -> Tuple[Dict, List[str], Dict]:
    """打不中 / 不支持 → 不抛异常，标 applied=False + warning。"""
    out = deepcopy(plan)
    p = dict(patch) if isinstance(patch, dict) else {"field": "", "value": patch}
    p["applied"] = False
    p["warning"] = reason
    if not p.get("target"):
        p["target"] = ""
    if not p.get("field"):
        p["field"] = ""
    return out, [], p


def apply_patch(plan, patch) -> Tuple[Dict, List[str], Dict]:
    """把一个 patch 应用到计划上（纯函数：返回新计划，不改传入的 plan）。

    支持的 field：
      quantity / duration  → 改叶子自身
      name                 → 改叶子名字，并同步任务表里的冗余 task_name
      norm                 → 改叶子的 norm_binding（字典则浅合并）
      crew                 → 改叶子的 norm_binding.crew（字典则合并）
      level / cost / segment / plan_title → 只记在 plan["meta"]（不动树）
      start_date           → 改开工日期：记 meta + overview，并按差值平移任务表日期
      target_duration      → 记目标总工期：写 meta 与 boundary_conditions，**不重排**
      add_task / remove_task → 就地增删叶子，并维护依赖图（不让图断链/生孤儿）

    返回 (新计划, changed_ids, patch)：
      - 成功：patch["applied"]=True，changed_ids=[目标 id]
      - 失败：patch["applied"]=False、patch["warning"]=原因，changed_ids=[]
      - 已生效但有话要说（例如新增任务时自动串了逻辑关系、开工日期改了但没有旧日期
        可算差值）：patch["note"]=说明，仍算成功。note 与 warning 二选一，不并存：
        warning 表示"没改成功"，note 表示"改成功了，但你需要知道这件事"。
    """
    if not isinstance(plan, dict):
        return plan, [], dict(patch) if isinstance(patch, dict) else {}
    if not isinstance(patch, dict):
        return _reject(plan, {"field": "", "value": patch}, "patch 不是字典，已忽略")

    field = str(patch.get("field") or "").strip()
    target = patch.get("target")
    tid = "" if target is None else str(target)

    # ---- 结构级：加 / 删任务（现在放进 SUPPORTED_FIELDS，LLM 与 revise 流程都能产出）----
    leaf = find_leaf(plan, tid) if tid else None

    if field == "add_task":
        # value 允许直接给一个任务名（"加一个 夜间浇筑" 这类口语最常见），
        # 不给就当成 {}，后面用生成的编号当默认名字 —— 宁可加一条名字朴素的空任务，
        # 也不要因为缺字段整条拒绝，让用户觉得"说了半天没加上"。
        raw_spec = patch.get("value")
        if isinstance(raw_spec, dict):
            spec = raw_spec
        elif isinstance(raw_spec, str) and raw_spec.strip():
            spec = {"name": raw_spec}
        else:
            spec = {}

        # 插入位置单独记在 insert_after 里：target 会被改成**新任务的编号**（上层要靠它
        # 算影响范围），若把插入位置也塞在 target 里，重放时这条 patch 就找不到"插在谁
        # 后面"，会静默改成塞进第一个工作包 —— 重建出来的历史版本就和当时的当前版本
        # 不是同一份计划，修订链（/undo、/goto）也就不可信了。
        ins = patch.get("insert_after") or tid
        if not str(ins).strip() or str(ins).strip() == PLAN_TARGET:
            ins = ""            # target="plan" 是"没指定位置"，不是一个任务编号
        user_position = bool(ins)           # 用户**明确**说了插在哪条后面吗

        out = deepcopy(plan)
        slot = _find_leaf_slot(out, ins) if ins else None
        insert_hit = slot is not None               # 是否真指定了"插在谁后面"

        host_leaf = None
        holder = None
        idx = -1
        host_wp = None
        phase = None
        wp_leaf = False
        if insert_hit:                              # 命中目标 → 插在它后面
            host_leaf, holder, idx, wp_leaf, phase, host_wp = slot
            host_wp_id = str((host_wp or {}).get("id") or "")
            host_wp_name = str((host_wp or {}).get("name") or "")
        else:                                       # 没给位置（或给的位置不存在）→ 第一个工作包
            host_wp_id = ""
            host_wp_name = ""
            first_slot = None                       # 第一个工作包（可能它自己就是任务）
            for phase, wp, _lf, _holder, _i, _wp_leaf in _iter_places(out):
                if first_slot is None:
                    first_slot = (phase, wp)
                if _wp_leaf:
                    # ⚠️ 这个工作包自己就是任务：`_iter_places` 给它的"所在列表"是一个
                    # **临时列表** `[wp]`，append 进去会被丢掉 —— 新增任务哪儿都不在，
                    # patch 却报 applied=True（静默丢数据）。这种工作包不能当落脚点。
                    continue
                host_wp = wp
                holder = _holder
                host_wp_id = str((wp or {}).get("id") or _lf.get("id") or "")
                host_wp_name = str((wp or {}).get("name") or "")
                break
            else:
                # 一个"有子工序"的工作包都没有（整份计划的每个工作包自己就是任务）。
                # 这时**复用"插在工作包后面"的既有逻辑**：以第一个工作包为锚点，新任务
                # 会作为它的**同级工作包**落进同一个阶段 —— 既不会掉进临时列表，
                # 也不需要另造一个阶段。
                if first_slot is not None:
                    _ph, first_wp = first_slot
                    first_id = str((first_wp or {}).get("id") or "")
                    forced = _find_leaf_slot(out, first_id) if first_id else None
                    if forced is not None:
                        host_leaf, holder, idx, wp_leaf, phase, host_wp = forced
                        ins = first_id
                        slot, insert_hit = forced, True
                        host_wp_id = first_id
                        host_wp_name = str((first_wp or {}).get("name") or "")
        if insert_hit and wp_leaf and phase is not None:
            # 目标就是一个二级工作包（它没有 sub_packages）：它给出的"所在列表"是
            # _iter_places 造的临时列表，insert 进去会被丢掉 —— 计划里根本没有这条新
            # 任务，patch 却报 applied=True，重算也就当它不存在（静默丢数据）。
            # 正确做法是往**同一个阶段的工作包列表**里插一个新工作包当兄弟。
            anchor_id = str(host_leaf.get("id") or "") if isinstance(host_leaf, dict) else ""
            siblings = phase.get("work_packages")
            if not isinstance(siblings, list):
                siblings = []
                phase["work_packages"] = siblings
        else:
            # 插在普通子工序后面时，锚点就是那条工序本身 —— 必须传下去，否则
            # `_next_child_id` 不知道"新任务要跟谁同级"，只能退到**第一个同级**的父编号，
            # 于是「插在 5.1.2 之后」会得到 5.1.1.3（另一组的编号），而不是同级的 5.1.3。
            anchor_id = str(host_leaf.get("id") or "") if (
                insert_hit and isinstance(host_leaf, dict)) else ""
            siblings = holder

        new_id = str(spec.get("id") or "").strip()
        spec_id = new_id                            # 调用方指定的编号（空 = 自动生成）
        wp_leaf_new = insert_hit and wp_leaf and phase is not None
        collision = False
        if not new_id:
            if wp_leaf_new:
                # 工作包本身就是任务：新任务也是一个工作包，编号取同级工作包的下一个。
                new_id = _next_wp_sibling_id(out, host_wp, siblings)
            elif host_wp is None and not siblings:
                # 计划里连一个工作包都没有（空计划）：退回老的兜底 —— 造一个阶段装它，
                # 否则这条新增任务无处安放，用户的"加一个任务"就彻底没结果。
                new_id = "新增.1"
            else:
                new_id = _next_child_id(out, host_wp_id, siblings, anchor_id)
        # 撞号要顺延而不是拒绝：两条不同的任务共用一个编号时，排程与依赖表会把它们
        # 当成同一条（改一条等于改两条）。这里顺着撞号的那个编号往下找空位。
        # ⚠️ 用全量编号（含工作包）判：只看叶子会漏掉"撞上某个工作包编号"的情况。
        if new_id in _ids_and_plan(out):
            collision = True
            if wp_leaf_new:
                # 工作包编号：在同级工作包里往后找（组 = 工作包的上级编号）
                host_id = str((host_wp or {}).get("id") or "")
                group = host_id.rpartition(".")[0] or host_id
                new_id = _next_in_group(out, host_id or "0", group)
            elif spec_id:
                new_id = _next_in_group(out, spec_id)
            else:
                # 重新走一遍"下一次生成"：它会跳过 group 后缀 1..top，
                # 不能改用撞上的编号那一组（那会跳到别的工作包的编号上去）
                new_id = _next_child_id(out, host_wp_id, siblings, anchor_id)

        qty = _finite(spec.get("quantity"))
        days = _finite(spec.get("duration_days"))
        leaf_new = {
            "id": new_id,
            "name": str(spec.get("name") or new_id),
            "quantity": qty if qty is not None else 0,
            "unit": str(spec.get("unit") or ""),
            "duration_days": max(1, int(math.ceil(days))) if days is not None else 1,
            "work_type": str(spec.get("work_type") or ""),
            "source": "revise",
        }
        crew = spec.get("crew")
        if isinstance(crew, dict) and crew:
            leaf_new["norm_binding"] = {
                "crew": {str(k): int(_finite(v) or 0) for k, v in crew.items()}}

        # 落位：指定了位置就插在目标后面（工作包本身是任务时插"同级工作包"），
        # 没指定位置就追加到第一个工作包末尾 —— 注意别用 idx=-1 插入，那会插到队首。
        placed = False
        if insert_hit and wp_leaf_new:
            siblings.insert(siblings.index(host_leaf) + 1, leaf_new)
            placed = True
        elif insert_hit and holder is not None:
            holder.insert(idx + 1, leaf_new)
            placed = True
        elif holder is not None:
            holder.append(leaf_new)
            placed = True

        note = ""
        if insert_hit:
            # 自动串逻辑关系：新任务若是"孤儿"（没有任何前驱后继），排程会把它排到
            # 一边、总工期与关键路径都不含它，用户就会问"我加的任务怎么没进计划"。
            # 做法是把 X 原有的后继改挂到新任务下（X → 新 → X 的老后继），
            # 再补一条 X → 新，用户想并联可以下一句话再说。
            deps = out.get("dependencies")
            if not isinstance(deps, list):
                deps = []
                out["dependencies"] = deps
            for dep in deps:
                if isinstance(dep, dict) and str(dep.get("predecessor")) == ins:
                    dep["predecessor"] = new_id
            has_edge = any(
                isinstance(d, dict)
                and str(d.get("predecessor")) == ins and str(d.get("successor")) == new_id
                for d in deps)
            if not has_edge:
                deps.append({"predecessor": ins, "successor": new_id,
                             "type": "FS", "lag_days": 0})
            note = ("新增任务 %s 已按 FS 关系串在 %s 之后（如需并联/调整逻辑关系请另说）"
                    % (new_id, ins))
            if not user_position:
                # 兜底挑的位置不能写得像用户指定的：他是"没说位置"，我们替他选了
                # 一个（第一个有子工序的工作包 / 第一个工作包之后）。
                note = ("没说要插在哪条工序后面，已放在 %s 之后；%s"
                        % (ins, note))
        elif placed:
            # 没给位置（或给的位置不存在）→ 上面已经追加进第一个工作包了。
            # ⚠️ 这条分支**必须**存在：早先"造阶段"的兜底挂在这里（`if insert_hit: … else:`
            # 的 else），于是没指定位置时会先 `holder.append(leaf_new)` 追加进第一个工作包，
            # 再走 else 把**同一个 leaf_new 对象**塞进一个新建的「修改新增」阶段 ——
            # 同一个编号在计划里出现两条（实测真实计划 209 → 211 条、"1.1.3" 重复两次，
            # 两个位置指向同一个 dict）。排程与依赖表按编号认任务，会把它们当成同一条：
            # 用户改一条等于改两条。
            note = ("没说要插在哪条工序后面，已把新增任务 %s 放进第一个工作包 %s%s；"
                    "它与现有任务没有逻辑关系（未串前后置），需要定位请下次说"
                    "「在 <工序编号> 后面增加一个工序」。"
                    % (new_id, host_wp_id or "(无编号)",
                       ("「%s」" % host_wp_name) if host_wp_name else ""))
        else:
            # 只有在**真的一个工作包都没有**（空计划）时才允许造阶段兜底，
            # 否则这条新增任务无处安放，用户的"加一个任务"就彻底没结果。
            out.setdefault("wbs", {})
            if not isinstance(out.get("wbs"), dict):
                out["wbs"] = {}
            out["wbs"].setdefault("phases", [])
            if not isinstance(out["wbs"].get("phases"), list):
                out["wbs"]["phases"] = []
            host_wp = {"id": host_wp_id or "新增", "name": "修改新增",
                       "sub_packages": [leaf_new]}
            out["wbs"]["phases"].append({"phase": "修改新增",
                                         "work_packages": [host_wp]})
            note = ("计划里原来没有任何工作包，已为新增任务 %s 建了一个「修改新增」阶段。"
                    % new_id)
        if collision:
            note = ("编号与既有任务重复，已改用 %s 以免两条任务被当成同一条；%s"
                    % (new_id, note))

        p = dict(patch)
        p["applied"] = True
        # 三样都要记全，缺一条修订链就不可重放：
        #   insert_after → 插在哪条工序后面（"plan" / 空 表示没指定位置）；
        #   value.id     → 生成的编号写死进 patch，重放时不再重新推导（否则上游一变，
        #                  编号就变，后面所有指着它的 patch 全部打空）；
        #   target       → 新任务编号，上层要靠它算影响范围与重算范围。
        p["insert_after"] = tid
        p["value"] = dict(spec, id=new_id)
        p["target"] = new_id
        p.pop("warning", None)
        p["note"] = note
        return out, [new_id], p

    if field == "remove_task":
        if not tid or leaf is None:
            return _reject(plan, patch, "计划里找不到任务 %s，无法删除" % (tid or "(空)"))
        out = deepcopy(plan)
        place = find_leaf_place(out, tid)
        holder = place[1]
        holder.pop(place[2])

        # 依赖要摘干净，还要把断开的链接上：直接删掉 tid 的前后两条边会让
        # "A → tid → B" 变成 A、B 互不相干，B 可能被重排到最前面，用户看到的是
        # "我删了一个任务，后面全乱了"。所以按 p → s 补回一条 FS。
        deps = out.get("dependencies")
        bridges = []
        if isinstance(deps, list):
            preds, succs = [], []
            for dep in deps:
                if not isinstance(dep, dict):
                    continue
                if str(dep.get("predecessor")) == tid and dep.get("successor") is not None:
                    s = str(dep["successor"])
                    if s not in succs:
                        succs.append(s)
                if str(dep.get("successor")) == tid and dep.get("predecessor") is not None:
                    pr = str(dep["predecessor"])
                    if pr not in preds:
                        preds.append(pr)
            edges = set(
                (str(d.get("predecessor")), str(d.get("successor")))
                for d in deps if isinstance(d, dict))
            out["dependencies"] = [
                d for d in deps
                if not (isinstance(d, dict)
                        and (str(d.get("predecessor")) == tid
                             or str(d.get("successor")) == tid))]
            bridges = []
            for pr in preds:
                for s in succs:
                    if pr == tid or s == tid or (pr, s) in edges:
                        continue
                    edges.add((pr, s))
                    out["dependencies"].append({"predecessor": pr, "successor": s,
                                                "type": "FS", "lag_days": 0})
                    bridges.append("%s→%s" % (pr, s))

        # 任务表也要同步删：它同样是排程反写的冗余副本，留着就会出现
        # "任务已经从计划里删了，看板/Word 的表格里还有一行"。
        for key in ("all_tasks_schedule", "critical_path_tasks"):
            rows = out.get(key)
            if isinstance(rows, list):
                out[key] = [r for r in rows
                            if not (isinstance(r, dict) and str(r.get("task_id")) == tid)]

        p = dict(patch)
        p["applied"] = True
        p["target"] = tid
        p.pop("warning", None)
        if bridges:
            p["note"] = ("已删除 %s，并把断开的逻辑关系接回：%s"
                         % (tid, "、".join(bridges)))
        else:
            p["note"] = "已删除 %s（原本没有与之相连的逻辑关系，无需补链）" % tid
        return out, [tid], p

    if field not in SUPPORTED_FIELDS:
        return _reject(plan, patch,
                       "不支持的字段 %r（可用：%s）" % (field, "/".join(SUPPORTED_FIELDS)))

    # ---- 开工日期：记 overview/meta，并把任务表日期整体平移 ----
    if field == "start_date":
        value = str(patch.get("value") or "").strip()
        new_start = _parse_date(value)
        if new_start is None:
            return _reject(plan, patch,
                           "开工日期必须是 YYYY-MM-DD 格式（收到：%r）" % (patch.get("value"),))
        out = deepcopy(plan)
        ov = out.get("overview")
        if not isinstance(ov, dict):
            ov = {}
            out["overview"] = ov
        old_start = _parse_date(ov.get("planned_start_date"))
        ov["planned_start_date"] = value
        note = ""
        if old_start is None:
            # 老计划没有（或读不出）开工日期 → 算不出差值。**不猜**：猜错就是把整个
            # 甘特图整体挪错位置，比不动更糟。所以只记新日期，任务表日期原样保留，
            # 并在 note 里说清楚"这一项只改了总览"。
            if isinstance(out.get("all_tasks_schedule"), list) and out["all_tasks_schedule"]:
                note = ("原计划没有可解析的开工日期，无法推算整体平移量："
                        "只更新了总览开工日期，逐条任务的日期未调整")
        else:
            delta = (new_start - old_start).days
            if delta:
                _hit, _ok = _shift_schedule_dates(out, delta)
                if not _ok:
                    # 越界（日期上限 9999-12-31）：与其交回一份 finish < start、
                    # 竣工早于开工的自相矛盾日程，不如整条拒绝并说清楚。
                    return _reject(plan, patch,
                                   "开工日期改成 %s 会把部分日程推到日期上限之外"
                                   "（9999-12-31 之后），已整条拒绝；请换一个更早的日期。"
                                   % value)
                total_raw = ov.get("total_duration_days")
                total = _finite(total_raw)
                end_shifted = None
                if total is not None and not isinstance(total_raw, bool):
                    # 总工日可能是天文数字（坏数据 / 被人手改成 1e9），直接加会
                    # OverflowError，整个 /revise 变成 500。加不成就退回"按老竣工
                    # 日期平移"，再不行就留着不动 —— 宁可这个字段不更新，也不能炸。
                    # 闭区间口径：竣工日 = 新开工 + (总工期 - 1)（首尾两天都算），
                    # 与 `plan_assembler` / `recompute` 的 finish_date 同口径。
                    cand = _add_days(new_start, max(0, int(total) - 1))
                    if cand is not None:
                        end_shifted = cand.isoformat()
                if end_shifted is None:
                    old_end = _parse_date(ov.get("planned_end_date"))
                    cand = _add_days(old_end, delta)
                    if cand is not None:
                        end_shifted = cand.isoformat()
                if end_shifted is not None:
                    ov["planned_end_date"] = end_shifted
        meta = out.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            out["meta"] = meta
        # meta 也镜像一份：计划参数面板读的是 meta，只写 overview 会出现
        # "总览说 3 月开工，参数页还写 1 月"。
        meta["start_date"] = value
        p = dict(patch)
        p["applied"] = True
        p.pop("warning", None)
        if note:
            p["note"] = note
        else:
            p.pop("note", None)
        return out, [tid or PLAN_TARGET], p

    # ---- 目标总工期：只记意图，绝不在这里重排 ----
    if field == "target_duration":
        num = _finite(patch.get("value"))
        if num is None or num < 1:
            return _reject(plan, patch,
                           "目标总工期必须是 >=1 的整数天数（收到：%r）" % (patch.get("value"),))
        days = int(round(num))
        out = deepcopy(plan)
        meta = out.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            out["meta"] = meta
        meta["target_duration_days"] = days
        bc = meta.get("boundary_conditions")
        if not isinstance(bc, dict):
            # 排程器读的就是这里的 project_duration_days：不建它，用户说的
            # "总工期压到 300 天" 就只是记了句话，排程时完全看不到。
            bc = {}
            meta["boundary_conditions"] = bc
        bc["project_duration_days"] = days
        # 第 40 轮：这个值是**用户明确改写**的（能走到这里就是因为有人下发补丁），
        # 来源必须同步成 "user"。否则按新约定读 `_source` 的下游（scheduler 的
        # `parse_boundary_limits` / resource 的 `parse_boundary_conditions`）会把它当
        # "模型按常见做法补的"而**忽略** —— 用户那句"总工期压到 300 天"就静默失效了。
        # 这是"来源标注"新约定引入的缺口：只加标注不同步标注 = 用户的指令被自己的规则吃掉。
        # **只在 `_source` 已存在时同步**：新流水线的计划一定有它（boundary 节点恒写，
        # 见 boundary.py:412-437）；老计划没有标注，下游按"无标注 = 旧行为"照常采纳，
        # 此时凭空造一个只有单键的 `_source` 只会让"其余键有没有标注"看起来像已知信息。
        _src = bc.get("_source")
        if isinstance(_src, dict):
            _src["project_duration_days"] = "user"
        p = dict(patch)
        p["applied"] = True
        p["target"] = tid or PLAN_TARGET
        p.pop("warning", None)
        return out, [tid or PLAN_TARGET], p

    # ---- 计划级字段：level（计划细度）/ cost（成本口径）/ segment（施工段）
    #      / plan_title（计划名称，第 35 轮）只记 meta，不动树 ----
    if field in META_FIELDS:
        out = deepcopy(plan)
        meta = out.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            out["meta"] = meta
        meta[field] = deepcopy(patch.get("value"))
        if field == "plan_title":
            # 名称要**看得见**：看板/Word 的总览、以及计划列表都读 overview，
            # 只写 meta 的话用户改了名字却到处都看不到，等于没改。
            ov = out.get("overview")
            if not isinstance(ov, dict):
                ov = {}
                out["overview"] = ov
            ov["project_name"] = str(patch.get("value") or "").strip()
            tid = tid or PLAN_TARGET
        p = dict(patch)
        p["applied"] = True
        p.pop("warning", None)
        return out, ([tid] if tid else []), p

    if not tid:
        return _reject(plan, patch, "patch 缺少 target，无法定位任务")

    value = patch.get("value")
    out = deepcopy(plan)
    place = find_leaf_place(out, tid)
    if place is None:
        return _reject(plan, patch, "计划里找不到任务 %s，已跳过" % tid)
    leaf, holder, idx = place

    if field == "quantity":
        num = _finite(value)
        if num is None or num < 0:
            return _reject(plan, patch, "quantity 取值非法：%r" % (value,))
        leaf["quantity"] = int(num) if float(num).is_integer() else num

    elif field == "name":
        new_name = str(value if value is not None else "").strip()
        if not new_name:
            return _reject(plan, patch, "任务名称不能为空")
        if len(new_name) > 40:
            # 表格 / 甘特图的工序列放不下长句，且多半是误把整句话当成了名字
            return _reject(plan, patch,
                           "任务名称过长（%d 字，最多 40 字），请精简后再改" % len(new_name))
        leaf["name"] = new_name
        # 冗余副本一起改：all_tasks_schedule / critical_path_tasks 是排程时反写的
        # 任务名，改名刻意不重排；不跟着改，看板/Word 上仍是旧名字。
        for key in ("all_tasks_schedule", "critical_path_tasks"):
            rows = out.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and str(row.get("task_id")) == tid:
                    row["task_name"] = new_name

    elif field == "duration":
        num = _finite(value)
        if num is None or num < 0:
            return _reject(plan, patch, "duration 取值非法：%r" % (value,))
        leaf["duration_days"] = int(math.ceil(num))

    elif field == "norm":
        binding = leaf.get("norm_binding")
        if not isinstance(binding, dict):
            binding = {}
            leaf["norm_binding"] = binding
        if isinstance(value, dict):
            binding.update(deepcopy(value))
        else:
            binding["norm_value"] = value

    elif field == "crew":
        binding = leaf.get("norm_binding")
        if not isinstance(binding, dict):
            binding = {}
            leaf["norm_binding"] = binding
        crew = binding.get("crew")
        if not isinstance(crew, dict):
            crew = {}
            binding["crew"] = crew
        if isinstance(value, dict):
            for k, v in value.items():
                num = _finite(v)
                if num is not None:
                    crew[str(k)] = int(num)
        else:
            num = _finite(value)
            if num is None:
                return _reject(plan, patch, "crew 取值非法：%r" % (value,))
            # 单个数字：按该叶子现有工种覆盖，否则记到 人工
            role = sorted(crew.keys())[0] if crew else "人工"
            crew[role] = int(num)

    p = dict(patch)
    p["applied"] = True
    p.pop("warning", None)
    return out, [tid], p


# ==================== 存档 ====================
class PlanStore:
    """一个计划档案 = 一个目录；基线 + 修订链 = 完整可重建的历史。"""

    def __init__(self, root=None):
        if root is None:
            try:  # 惰性导入：本模块允许完全不依赖后端其它模块
                from . import config
                root = os.path.join(str(config.PLANS_DIR), "档案")
            except Exception:
                root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "_plan_archive")
        self.root = str(root)
        _ensure_dir(self.root)

    # ---------------- 路径 ----------------
    def _plan_dir(self, plan_id, create=True) -> str:
        path = os.path.join(self.root, str(plan_id))
        if create:
            _ensure_dir(path)
        return path

    def current_path(self, plan_id) -> str:
        return os.path.join(self._plan_dir(plan_id), CURRENT_NAME)

    def baseline_path(self, plan_id) -> str:
        return os.path.join(self._plan_dir(plan_id), BASELINE_NAME)

    def revisions_dir(self, plan_id) -> str:
        return os.path.join(self._plan_dir(plan_id), REVISIONS_DIR_NAME)

    def audit_path(self, plan_id) -> str:
        return os.path.join(self._plan_dir(plan_id), AUDIT_NAME)

    # ---------------- 基线 / 当前版本 ----------------
    def save_baseline(self, plan_id, plan) -> str:
        """落基线（并同步当前版本），返回 当前版本.json 路径。"""
        if not isinstance(plan, dict):
            raise ValueError("plan 必须是字典")
        _ensure_dir(self._plan_dir(plan_id))
        _ensure_dir(self.revisions_dir(plan_id))
        base = deepcopy(plan)
        meta = base.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            base["meta"] = meta
        meta.setdefault("revision", 0)
        meta.setdefault("revision_label", "基线")
        _write_json(self.baseline_path(plan_id), base)
        path = _write_json(self.current_path(plan_id), deepcopy(base))
        self.log_audit(plan_id, "基线", "落基线", "建立计划档案，版本 v0（基线）")
        return path

    def load_current(self, plan_id) -> Optional[Dict]:
        """读当前版本；不存在（或损坏）时退回基线；都没有返回 None。"""
        data = _read_json(self.current_path(plan_id))
        if isinstance(data, dict):
            return data
        data = _read_json(self.baseline_path(plan_id))
        return data if isinstance(data, dict) else None

    def load_baseline(self, plan_id) -> Optional[Dict]:
        data = _read_json(self.baseline_path(plan_id))
        return data if isinstance(data, dict) else None

    # ---------------- 修订链 ----------------
    def _revision_files(self, plan_id) -> List[str]:
        """按文件名排序的修订文件列表（001.json, 002.json …）。"""
        folder = self.revisions_dir(plan_id)
        try:
            names = [n for n in os.listdir(folder)
                     if n.lower().endswith(".json") and not n.endswith(".tmp")]
        except OSError:
            return []
        names.sort()
        return [os.path.join(folder, n) for n in names]

    def _load_chain(self, plan_id):
        """读修订链 → (patches 列表, 损坏文件列表)。损坏的跳过并记录，不炸历史。"""
        patches, damaged = [], []
        for path in self._revision_files(plan_id):
            rec = _read_json(path)
            if not isinstance(rec, dict):
                damaged.append(os.path.basename(path))
                continue
            patch = rec.get("patch")
            if isinstance(patch, dict):
                p = dict(patch)
                # 记录里的 time/影响范围/摘要 用于回放时保留痕迹，不参与树修改
                p.setdefault("revision_index", len(patches) + 1)
                patches.append(p)
            else:
                damaged.append(os.path.basename(path))
        return patches, damaged

    def append_revision(self, plan_id, patch, raw_text, affected, summary) -> str:
        """追加一条修订（只存 patch，不存全量），返回该修订文件路径。"""
        if not isinstance(patch, dict):
            raise ValueError("patch 必须是可序列化的字典")
        _ensure_dir(self.revisions_dir(plan_id))
        index = len(self._revision_files(plan_id)) + 1
        path = os.path.join(self.revisions_dir(plan_id), "%03d.json" % index)
        record = {
            "序号": index,
            "时间": _now(),
            "用户原话": raw_text or "",
            "patch": patch,
            "影响范围": list(affected or []),
            "重算摘要": summary or "",
        }
        _write_json(path, record)
        self.log_audit(plan_id, "第%d轮" % index, "追加修订",
                       "字段 %s → %s；影响 %d 项；%s"
                       % (patch.get("field", ""), patch.get("target", ""),
                          len(record["影响范围"]), record["重算摘要"]))
        return path

    def list_revisions(self, plan_id) -> List[Dict]:
        """修订记录列表（按序）；损坏的文件以 损坏=True 占位返回，不抛异常。"""
        records = []
        for path in self._revision_files(plan_id):
            rec = _read_json(path)
            if isinstance(rec, dict):
                rec.setdefault("序号", len(records) + 1)
                rec.setdefault("文件", os.path.basename(path))
                rec["损坏"] = False
                records.append(rec)
            else:
                records.append({
                    "序号": len(records) + 1,
                    "文件": os.path.basename(path),
                    "时间": "",
                    "用户原话": "",
                    "patch": {},
                    "影响范围": [],
                    "重算摘要": "",
                    "损坏": True,
                    "损坏说明": "JSON 无法解析，已跳过（历史继续可用）",
                })
        return records

    # ---------------- 重建 / 回退 ----------------
    def rebuild(self, plan_id, upto=None) -> Optional[Dict]:
        """用 基线 + 前 N 条 patch 重建任意版本；upto=None 表示全部。

        - upto=0 → 基线本体；upto=k → 应用前 k 条修订；
        - 重建结果同时写回 当前版本.json（保证"当前生效"永远是重放结果）；
        - 重建计划里带 meta.revision / revision_label / rebuilt_damaged 便于追溯。
        """
        base = self.load_baseline(plan_id)
        if base is None:
            return None
        patches, damaged = self._load_chain(plan_id)
        if upto is not None:
            try:
                limit = max(0, int(upto))
            except (TypeError, ValueError):
                limit = len(patches)
            patches = patches[:limit]

        plan = deepcopy(base)
        meta = plan.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            plan["meta"] = meta
        meta["revision"] = len(patches)
        meta["revision_label"] = "基线" if not patches else "第%d版" % len(patches)
        if damaged:
            meta["rebuilt_damaged"] = list(damaged)

        for patch in patches:
            plan, _changed, _p = apply_patch(plan, patch)

        _write_json(self.current_path(plan_id), deepcopy(plan))
        return plan

    def _delete_from(self, plan_id, index) -> None:
        """删除序号 index 及其之后的修订文件（index 从 1 开始）。"""
        for path in self._revision_files(plan_id)[index - 1:]:
            try:
                os.remove(path)
            except OSError:
                pass

    def undo(self, plan_id) -> Optional[Dict]:
        """退回上一版（删掉最后一条修订），返回重建后的计划。"""
        patches = self._revision_files(plan_id)
        if not patches:
            return self.rebuild(plan_id)          # 已是基线，原地不动
        self._delete_from(plan_id, len(patches))
        plan = self.rebuild(plan_id)
        self.log_audit(plan_id, "回退", "撤销上一版", "已退回 %s" % self._label(plan))
        return plan

    def goto(self, plan_id, index) -> Optional[Dict]:
        """退回到第 index 版（0=基线），返回重建后的计划。"""
        try:
            target = max(0, int(index))
        except (TypeError, ValueError):
            target = 0
        total = len(self._revision_files(plan_id))
        if target < total:
            self._delete_from(plan_id, target + 1)
        plan = self.rebuild(plan_id)
        self.log_audit(plan_id, "回退", "跳转到第%d版" % target,
                       "从 %d 条修订回退到 %d 条；当前 %s"
                       % (total, min(target, total), self._label(plan)))
        return plan

    @staticmethod
    def _label(plan) -> str:
        if not isinstance(plan, dict):
            return "无"
        meta = plan.get("meta") or {}
        return str(meta.get("revision_label") or "基线")

    # ---------------- 历史 / 版本 ----------------
    def history(self, plan_id) -> List[Dict]:
        """人类可读历史 [{时间, 用户原话, 摘要}]，第一条是基线。"""
        items = [{"时间": "", "用户原话": "（基线）", "摘要": "初始计划，版本 v0"}]
        for rec in self.list_revisions(plan_id):
            if rec.get("损坏"):
                items.append({"时间": "", "用户原话": "（修订文件损坏，已跳过）",
                              "摘要": rec.get("文件", "")})
                continue
            patch = rec.get("patch") or {}
            desc = "把 %s 的 %s 改为 %s" % (patch.get("target", ""), patch.get("field", ""),
                                          patch.get("value", ""))
            summary = rec.get("重算摘要") or desc
            items.append({"时间": rec.get("时间", ""),
                          "用户原话": rec.get("用户原话", ""),
                          "摘要": summary})
        return items

    def versions(self, plan_id) -> List[Dict]:
        """可用版本列表：[{版本, 序号, 说明}]，0=基线，之后每条修订一版。"""
        result = [{"版本": "v0", "序号": 0, "说明": "基线"}]
        for rec in self.list_revisions(plan_id):
            idx = rec.get("序号", len(result))
            if rec.get("损坏"):
                result.append({"版本": "v%d" % idx, "序号": idx,
                               "说明": "修订文件损坏（%s），已跳过" % rec.get("文件", "")})
                continue
            patch = rec.get("patch") or {}
            result.append({"版本": "v%d" % idx, "序号": idx,
                           "说明": "%s → %s=%s"
                                   % (patch.get("target", ""), patch.get("field", ""),
                                      patch.get("value", ""))})
        return result

    # ---------------- 审计 ----------------
    def log_audit(self, plan_id, round_name, action, note="") -> None:
        """追加一条审计事件；审计状态一旦改过就保留（默认"未审计"）。"""
        _ensure_dir(self._plan_dir(plan_id))
        data = _read_json(self.audit_path(plan_id))
        if not isinstance(data, dict):
            data = {}
        status = data.get("audit_status") or "未审计"
        events = data.get("events")
        if not isinstance(events, list):
            events = []
        events.append({"时间": _now(), "轮次": str(round_name),
                       "动作": str(action), "说明": str(note)})
        data["audit_status"] = status
        data["events"] = events
        _write_json(self.audit_path(plan_id), data)

    def set_audit_status(self, plan_id, status) -> None:
        """改审计状态（未审计 / 已审计）；由审计流程调用。"""
        data = _read_json(self.audit_path(plan_id))
        if not isinstance(data, dict):
            data = {"events": []}
        data["audit_status"] = str(status)
        _write_json(self.audit_path(plan_id), data)
        self.log_audit(plan_id, "审计状态", "变更", "审计状态 → %s" % status)

    def audit_log(self, plan_id) -> Dict:
        """读审计记录（损坏 / 缺失时返回默认结构，不抛异常）。"""
        data = _read_json(self.audit_path(plan_id))
        if not isinstance(data, dict):
            return {"audit_status": "未审计", "events": []}
        data.setdefault("audit_status", "未审计")
        if not isinstance(data.get("events"), list):
            data["events"] = []
        return data
