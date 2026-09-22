# -*- coding: utf-8 -*-
"""节点：自然语言修改（AI 只负责翻译，代码负责执行，只重算受影响的部分）

定位（产品核心功能）：
  用户一句大白话 → ① LLM 翻译成**规范指令 patch 列表** → ② 代码校验（清单/字段/数值/容量）
  → ③ 代码执行（`plan_store.apply_patch`）→ ④ 沿依赖做**下游闭包**，只把这些任务交给
  `recompute` 回调重算 → ⑤ 汇总成可回退的修订（patch 落库，可重建任意版本）。

分工边界（刻意为之）：
  - LLM/提示词只做"翻译"，不许发明任务 id、不许算工期、不许判断可行性；
  - 校验与执行、越界回报、受影响范围、重算触发全在代码里，可测试、可复现；
  - 本节点**不认识排程器**：重算通过可注入回调 `recompute(ctx, affected_ids) -> dict`
    完成，默认实现只按 quantity/norm 重算这几个叶子自身的 duration_days，不做排程，
    等排程节点就绪后由接线方把它注入进来即可（不进本文件）。

输入 ctx：
  ctx["user_instruction"]  用户原话（没有就退回 ctx["prompt"] / ctx["user_text"]）
  ctx["plan_json"] 或 ctx["wbs"]   当前计划（plan_json 优先）
  ctx["revise_targets"]（可选）当前允许修改的项清单；不给就由计划自动生成
  ctx["dependencies"]（或 ctx["deps"]）依赖边，用于下游闭包
输出 ctx：
  ctx["revision"] = {"raw_text", "patches", "applied", "rejected",
                     "affected", "summary", "warnings"}

Python 3.8 兼容：不使用 `X | None` 标注。
"""

import json
import math
import os
import re
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from ..base import BaseNode
from ..plan_store import (LEAF_FIELDS, META_FIELDS, PLAN_TARGET, STRUCT_FIELDS,
                          SUPPORTED_FIELDS, apply_patch, dependency_closure,
                          iter_leaves)

# 允许驱动修改的字段（与 plan_store 支持集一致）
ALLOWED_FIELDS = list(SUPPORTED_FIELDS)
# 清单里"工序项"能改的字段（第 36 轮 Phase 3）：叶子字段 + 删掉这道工序。
# 不给工序项列 plan_title/start_date/target_duration —— 让模型在工序上改项目名，
# 校验阶段必然被拒，白费一次调用（旧清单给每条都列了全字段，就是这么浪费的）。
ITEM_LEAF_FIELDS = list(LEAF_FIELDS) + ["remove_task"]
# 清单里"计划项"能改的字段：计划级 + 新增工序（add_task 的 target 指"插在哪条之后"，
# 缺省 plan = 追加到第一个工作包）。
PLAN_FIELDS = list(META_FIELDS) + ["add_task"]
# 规则解析一旦命中这些字段就**不再调模型**：它们的句式里已经包含了全部信息
# （新名字 / 日期 / 天数 / 改成什么），模型插手只会加字或改口径。
# 增删工序不在此列 —— 那句话里可能还带着工期、工程量，交给模型能多抠出一些。
RULE_FIRST_FIELDS = ("plan_title", "start_date", "target_duration", "name")


def _single_intent(text) -> bool:
    """原话是不是只有一个分句（没有"，/；/。"这类分隔）。

    给 `_translate` 用：规则层对"改名 / 开工日期 / 总工期目标"这几类句式解析得比模型
    准，命中就不该再花一次调用；但一句话里若有别的意图，规则只返回其中一条，
    早返回会吞掉另外半句。多分句时交给模型（模型不可用时仍会退回规则解析）。
    """
    return not re.search(r"[，。；,;]", str(text or ""))
# **不会向上下游传播**的字段：改它们只影响目标自身（或整份日历），
# 不该被当成"受影响的下游任务"。用于 `ReviseNode.run` 里算受影响范围。
_NON_PROPAGATING = ("name", "plan_title", "level", "cost", "segment",
                    "start_date", "target_duration")

# 中文口语 → 规范字段名
FIELD_ALIASES = [
    ("工期", "duration"), ("持续时间", "duration"), ("天数", "duration"),
    ("施工天数", "duration"), ("天", "duration"),
    ("工程量", "quantity"), ("数量", "quantity"), ("方量", "quantity"),
    ("定额", "norm"), ("产能", "norm"), ("工效", "norm"),
    ("人数", "crew"), ("劳动力", "crew"), ("人工", "crew"),
    ("工人", "crew"), ("人力", "crew"), ("班组", "crew"),
    ("细度", "level"), ("计划细度", "level"),
    ("成本", "cost"), ("造价", "cost"),
    ("施工段", "segment"), ("分区", "segment"),
    # 计划级（第 35 轮）：用户实测「我想将项目名称改为 NUS 大楼」原本改不了
    ("项目名称", "plan_title"), ("工程名称", "plan_title"), ("计划名称", "plan_title"),
    ("项目名", "plan_title"), ("名称", "plan_title"), ("改名", "plan_title"),
    # 工序改名（第 36 轮 Phase 3）：**必须带限定词**（工序/任务/子项）。
    # 刻意不收裸"名字/名" —— 在「把项目名字改成…」里它指的是**项目**，
    # 收下来会让计划改名落到工序字段上，等于把项目名写到某条工序上。
    ("工序名称", "name"), ("任务名称", "name"), ("子项名称", "name"),
    ("工序名", "name"), ("任务名", "name"),
    # 开工日期（第 36 轮 Phase 3）：计划级，改的是 overview 的日期与全部日程平移。
    ("开工日期", "start_date"), ("开始日期", "start_date"), ("开工时间", "start_date"),
    ("开始时间", "start_date"), ("开工日", "start_date"), ("进场日期", "start_date"),
    # 总工期目标（第 36 轮 Phase 3）：`_field_of` 是最长别名优先，
    # 所以"总工期"（3 字）不会被"工期"（2 字）抢走。
    ("总工期", "target_duration"), ("总天数", "target_duration"),
    ("计划工期", "target_duration"), ("目标工期", "target_duration"),
]

# "把项目名称改为NUS大楼" / "项目名称改成 XXX" / "改名为 XXX" / "这个项目叫 XXX"
# 值里允许字母数字与中文，遇到标点或句尾停；至少要 2 个字符，避免"改为X"这种噪声。
#
# ⚠️ 名词组**必须**保留（名称/名字/标题/名号/名）。把它做成可选会让本模式匹配到
# 任何「改为/为 …」的句子，例如「把 5.1.1.1 的工期改为 20」会被误判成"改计划名"。
# 第 36 轮补的两个口子（都是用户实测原话）：
#   · 「我想修改这个项目名为 NUS 大楼」——名词后有"为"，旧动词表里没有裸"为"；
#   · 「这个项目叫 NUS 大楼」——名词被省略，只能靠"叫/名为"这类命名动词识别。
_PLAN_NOUN = r"(?:名称|名号|名字|标题|名)"
_PLAN_VERB = (r"(?:改成|改叫|改为|改到|变更为|变更成|变更|设置为|设置成|设定为|设为|"
              r"命名为|取名为|定名为|名为|叫做|叫作|叫|换成|替换成|替换为|为)")
_PLAN_VALUE = r"(?P<value>[\w\u4e00-\u9fff][\w\u4e00-\u9fff\-\. ]{1,39})"

_PAT_PLAN_TITLE = re.compile(
    r"(?:把|将)?\s*(?:这个|该|本)?\s*(?:项目的?|工程的?|计划的?)?\s*"
    + _PLAN_NOUN + r"\s*(?:改)?\s*" + _PLAN_VERB + r"\s*" + _PLAN_VALUE
)
# 没有"名称/名字"这类名词、只有命名动词的句式：「这个项目叫 XXX」「工程名为 XXX」
_PAT_PLAN_TITLE2 = re.compile(
    r"(?:这个|该|本)?\s*(?:项目的?|工程的?|计划的?)\s*"
    r"(?:叫做|叫作|叫|命名为|名为)\s*" + _PLAN_VALUE
)

# ==================== 第 36 轮 Phase 3：这一批句式**必须排在通用句式之前** ====================
# 通用句式 `_PAT_TARGET_FIELD/_PAT_TARGET_ONLY/_PAT_NOBA` 的模型是
# "把 <任务> 的 <字段> 改成 <数字>"，它对下面四类句子要么认错字段、要么把值截断。
_TASK_ID = r"(?P<target>[0-9]+(?:\.[0-9]+)+)"
# 工序改名：「把 5.1.1.1 的名字改成 XXX」「5.1.1.2 改名为 XXX」
# ⚠️ 必须最先判：本句式与计划改名**完全同形**（都有"名字/名称"），不先判的话
#    「把 5.1.1.1 的名字改成 X」会被 `_PAT_PLAN_TITLE` 抓去当**项目**改名 ——
#    用户想改一条工序，结果项目名变了。有显式点号编号 → 一定是工序。
_PAT_TASK_RENAME = re.compile(
    r"(?:把|将)?\s*" + _TASK_ID + r"\s*(?:这[条道个项]|该|此)?\s*"
    r"(?:工序|任务|子项|工作项)?\s*的?\s*(?:名称|名字|名|标题)\s*"
    r"(?:改成|改叫|改为|改到|变更为|变更成|设为|设置成|设置为|设定为"
    r"|命名为|取名为|叫做|叫作|叫|为)\s*" + _PLAN_VALUE)
# 工序改名的第二种写法：**名词融进了动词**里，前面没有独立的"名称/名字"。
# 实测漏掉的句子：「4.1.1.2 改名为 模板加固」—— `_PAT_TASK_RENAME` 要求名词在动词之前，
# 而这里只有动词「改名为」，于是整句掉进 `_PAT_PLAN_TITLE`（它能把"名为"拆成名词"名"
# + 动词"为"），结果**用户想改一条工序，项目名却变了**。
_PAT_TASK_RENAME2 = re.compile(
    r"(?:把|将)?\s*" + _TASK_ID + r"\s*(?:这[条道个项]|该|此)?\s*"
    r"(?:工序|任务|子项|工作项)?\s*的?\s*"
    r"(?:改名为|改名成|改叫|改称|改名为|更名为|命名为|取名为|定名为|叫做|叫作|叫)\s*"
    + _PLAN_VALUE)
# 工序改名的第三种写法：编号**没有点号**（"把 5 的名字改成 X"）。
# 计划里若有顶级编号（或用户把编号写成了光秃秃一个数字），前两个模式都要求点号，
# 会漏掉；而漏掉的后果是掉进 `_PAT_PLAN_TITLE` —— 用户想改一条工序，**项目名被改了**。
# 安全性：本模式必须同时出现"名称/名字/名"这个名词，所以「把 5 改成 20」
# 与「把 5 的工期改成 20」都不会命中。
_PAT_TASK_RENAME3 = re.compile(
    r"(?:把|将)?\s*(?P<target>[0-9]+)\s*(?:这[条道个项]|该|此)?\s*"
    r"(?:工序|任务|子项|工作项)?\s*的?\s*(?:名称|名字|名|标题)\s*"
    r"(?:改成|改叫|改为|改到|变更为|设为|设置成|设置为|设定为"
    r"|命名为|取名为|叫做|叫作|叫|为)\s*" + _PLAN_VALUE)
# 两套写法都要在"计划改名"之前判：详见 `_special_patches` 的说明。
_TASK_RENAME_PATS = (_PAT_TASK_RENAME, _PAT_TASK_RENAME2, _PAT_TASK_RENAME3)
# 开工日期：「把开工日期改到 2026-07-01」「开工日期设为 2026-7-1」
# ⚠️ 必须排在通用句式之前：`_NUM_TOKEN` 只认 `-?[0-9]+`，会把 "2026-07-01" 截成
#    "2026"（日期只剩一个年份），于是要么被当成天数、要么整句被丢。
_DATE_TOKEN = r"(?P<value>[0-9]{4}\s*[-/.]\s*[0-9]{1,2}\s*[-/.]\s*[0-9]{1,2})"
_PAT_START_DATE = re.compile(
    r"(?:把|将)?\s*(?:这个|该|本)?\s*(?:项目|工程|计划)?\s*的?\s*"
    r"(?:开工|开始|动工|进场|启动)\s*(?:日期|时间|日子)?\s*"
    r"(?:改成|改为|改到|改至|调到|调成|设为|设置成|设置为|设定为"
    r"|调整到|调整成|定在|订在|放在|为)\s*" + _DATE_TOKEN)
# 总工期目标：「总工期改成 306 天」「把总工期压到 300 天」
# ⚠️ 必须排在通用句式之前：否则「总工期改成 306 天」走 `_PAT_TARGET_ONLY` 会解析成
#    "任务名叫『总工期』、字段为空"，再因为清单里没有这个任务而被丢掉 ——
#    这正是用户说的"基本什么都改不了"里最典型的一条。
_PAT_TARGET_DURATION = re.compile(
    r"(?:把|将)?\s*(?:这个|该|本)?\s*(?:项目|工程|计划)?\s*的?\s*"
    r"(?:总工期|总天数|计划工期|目标工期|总日历天|总时长)\s*"
    r"(?:改成|改为|改到|改至|设为|设置成|设置为|设定为|压到|压缩到|压缩成"
    r"|降到|缩短到|缩短成|减少到|调整到|调整成|控制[在到]|定[在为]|限制[在到])\s*"
    r"(?P<value>[0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十]{1,3})\s*(?:天|日|个?日历天)?")
# 增工序：「增加一个工序：地下室防水」「新增工序：X」「再加一道工序 X」
# 只认**带"工序/任务"字样**的说法，否则「把工程量增加到 1200」会被当成新增任务。
_PAT_ADD_TASK = re.compile(
    r"(?:再|另外|额外)?\s*(?:增加|新增|添加|补充|加|补)\s*"
    r"(?:一[个道条项]|1[个道条项])?\s*(?:工序|任务|子项|工作项)\s*[:：]?\s*"
    r"(?P<value>[\u4e00-\u9fff\w][\u4e00-\u9fff\w\-\. ]{1,39})")
# 指定插入位置的新增：「在 4.1.1.1 后面增加一个工序：X」「4.1.1.1 之后加一道工序 X」
# 为什么值得单独一条：不指定位置时新任务只能塞进**第一个工作包**（施工准备），
# 用户多半找不着。既然能说清位置，就该认下来 —— 这里的 target 就是"插在它后面"。
_PAT_ADD_TASK_AT = re.compile(
    r"(?:在)?\s*" + _TASK_ID + r"\s*(?:之?后|后面|之后|下面|下方|后头)\s*"
    r"(?:再|另外|额外)?\s*(?:增加|新增|添加|补充|加|补)\s*"
    r"(?:一[个道条项]|1[个道条项])?\s*(?:工序|任务|子项|工作项)\s*[:：]?\s*"
    r"(?P<value>[\u4e00-\u9fff\w][\u4e00-\u9fff\w\-\. ]{1,39})")
# 删工序：「删除 5.1.1.1 这条工序」「把 5.1.1.1 删掉」「去掉 5.1.1.2」
_PAT_REMOVE_TASK = re.compile(
    r"(?:把|将)?\s*" + _TASK_ID + r"\s*(?:这[条道个项]|该|此)?\s*"
    r"(?:工序|任务|子项|工作项)?\s*(?:删掉|删除|去掉|移除|取消|拿掉)")
_PAT_REMOVE_TASK2 = re.compile(
    r"(?:删掉|删除|去掉|移除|取消|拿掉)\s*(?:第)?\s*" + _TASK_ID + r"\s*"
    r"(?:这[条道个项]|该|此)?\s*(?:工序|任务|子项|工作项)?")

# 常见工种（用于识别"把地下室混凝土工加到30人"里的工种）
CREW_ROLES = [
    "混凝土工", "钢筋工", "模板工", "普工", "架子工", "防水工", "砌筑工", "瓦工",
    "抹灰工", "泥工", "油漆工", "保温工", "装修工", "安装工", "管道工", "电工",
    "通风工", "测量工", "灌浆工", "装配式安装工", "桩机工", "铺装工", "水泥工",
    "绿化工", "桩基工", "焊工", "电焊工", "司机", "机械工", "起重工", "架子班组",
]

# 中文数字 → 阿拉伯数字（规则兜底用；只处理常见小数字）
_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9}

# 数值捕获组：阿拉伯数字（含负数），或"十五 / 二十 / 三十六"这类中文数字
_NUM_TOKEN = r"(?P<value>-?[0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十]{1,3})"
_ACTION = (r"(?:改成|改为|改到|调到|调成|设为|设置成|设置为|加到|增加到|提高[到至]"
           r"|提升[到至]|压到|压缩到|降到|减少到|削减到|变成|变为|调整为|调整到|调整成)")
# 任务说法：用 . 兜住（"把"前缀、"的"后缀在 _clean_name 里剥），
# 字段名不允许含数字，这样"…加到30人"里的 30 只会落到数值组，不会粘进字段
_NAME = r"(?P<name>.{1,40}?)"
_FIELD = r"(?P<field>[^，。；,;:：0-9]{1,12}?)"
# "把 X 的 Y 改成 Z"
_PAT_TARGET_FIELD = re.compile(
    r"(?:把|将|给)?\s*" + _NAME + r"\s*的\s*" + _FIELD + r"\s*" + _ACTION + r"\s*" + _NUM_TOKEN)
# "X 加到 Z"（没有"把/将"，字段也省了）—— 例如"主体结构钢筋工加到60人"。
# ⚠️ 解析出来但**没有启用**：实测"主体结构钢筋工加到60人"里"钢筋工"是**工种**，
# 实体是"主体结构"，实体名提取不出来就只能猜对象（猜错就是改错中间数据）。
# 因此这条句式一律**留给模型去理解**（`_llm_patches`），规则层不猜。
_PAT_LOOSE = re.compile(
    r"(?P<name>[\u4e00-\u9fff]{2,20}?)\s*" + _ACTION + r"\s*" + _NUM_TOKEN)
# "把 X 改成 Z"（无"的 字段"）
_PAT_TARGET_ONLY = re.compile(
    r"(?:把|将|给)\s*" + _NAME + r"\s*" + _ACTION + r"\s*" + _NUM_TOKEN)
# "X 的 Y 改成 Z"（无"把"）
_PAT_NOBA = re.compile(
    _NAME + r"\s*的\s*" + _FIELD + r"\s*" + _ACTION + r"\s*" + _NUM_TOKEN)


def _cn_to_int(token) -> Optional[int]:
    """中文数字 → 整数：十五=15、二十=20、三十六=36；识别不了返回 None。"""
    s = str(token or "")
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if len(s) == 1:
        return _CN_DIGIT.get(s)
    if s.startswith("十"):
        tail = _CN_DIGIT.get(s[1:]) if len(s) > 1 else 0
        return None if tail is None else 10 + tail
    if "十" in s:
        head, _, tail = s.partition("十")
        h = _CN_DIGIT.get(head)
        t = _CN_DIGIT.get(tail) if tail else 0
        if h is None or t is None:
            return None
        return h * 10 + t
    # "二三"这类连写不合法 → 放弃
    return None


def _num(value) -> Optional[float]:
    """从任意值里抠出一个有限数值；抠不到返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s in _CN_DIGIT or "十" in s and re.fullmatch(r"[一二两三四五六七八九十]+", s):
        cn = _cn_to_int(s)
        return float(cn) if cn is not None else None
    m = re.search(r"-?[0-9]+(?:\.[0-9]+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _norm_text(text) -> str:
    """全角转半角 + 去空白，便于规则匹配。"""
    s = str(text or "")
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r"\s+", "", "".join(out))


def _field_of(token) -> Optional[str]:
    """把"工期/人数"这类说法映射到规范字段名。

    ⚠️ 第 35 轮修：旧实现按 `FIELD_ALIASES` 的**声明顺序**返回第一个"包含"的别名，
    于是"钢筋工程量"里的"钢筋"先命中（别名表里"钢筋"不在，但"人工/人力"这类短词
    会先撞上），实测把"将钢筋工程量改为1000立方米"误判成 crew（人数）字段。
    改成**最长别名优先**：一个短语里同时出现多个别名时，"工程量"（3 字）胜过"天"（1 字）。
    """
    t = _norm_text(token)
    if t in ALLOWED_FIELDS:
        return t
    hits = [(len(alias), field) for alias, field in FIELD_ALIASES if alias and alias in t]
    if not hits:
        return None
    hits.sort(key=lambda x: -x[0])
    return hits[0][1]


def _role_in(text, roles) -> str:
    """在文本里找工种；优先匹配叶子已有的工种，其次通用工种表（长词优先）。"""
    t = _norm_text(text)
    pool = [str(r) for r in (roles or []) if r] + list(CREW_ROLES)
    for role in sorted(set(pool), key=len, reverse=True):
        if role and _role_hit(role, t):
            return role
    return ""


# 工种名后面紧跟这些字时，它不是工种，而是**材料/构件**的一部分：
# "钢筋工程量" 里的"钢筋工"、"模板工程量"里的"模板"…（"钢筋工"与"钢筋工程量"
# 只差一个字，实测把"将钢筋工程量改为1000立方米"整句误判成"改人数"）。
_ROLE_TAIL_BLOCK = ("程", "量", "数", "米", "方", "吨", "平方", "立方", "单", "价", "总")


def _role_hit(role, text) -> bool:
    """在 text 里找 role，但排除"…工"其实是材料词的情况。"""
    start = 0
    while True:
        i = text.find(role, start)
        if i < 0:
            return False
        nxt = text[i + len(role):i + len(role) + 1]
        if nxt not in _ROLE_TAIL_BLOCK:
            return True
        start = i + 1


def _blank_revision(raw_text) -> Dict:
    return {"raw_text": str(raw_text or ""), "patches": [], "applied": [],
            "rejected": [], "affected": [], "summary": "", "warnings": [],
            "hint": ""}


# ==================== 可修改项清单（喂给 LLM 的"小清单"，不是整份计划）====================
def build_revise_items(plan, max_items=200) -> List[Dict]:
    """由计划生成可修改项清单：target / name / fields / current。

    只抽叶子任务，且只带 LLM 真正需要的字段（含 workface_capacity 上限），
    避免把整份计划塞进提示词。

    第 36 轮 Phase 3 两处改动（都是为了"能改的东西真的能被改到"）：
      · 清单**开头补一条 `plan` 伪项**：项目名称 / 开工日期 / 总工期目标 / 新增工序
        这些计划级修改，原来在清单里**根本没有任何合法 target 可写** —— 模型只能
        硬编一个 target，然后在校验阶段被"清单里没有任务 X"拒掉。
      · 每条只列**对它有意义**的字段：工序项不给 plan_title/start_date，
        计划项不给 quantity/duration。旧实现给每条都挂上全部字段，等于在诱导
        模型把"改项目名"写到某条工序上。
    """
    overview = plan.get("overview") if isinstance(plan.get("overview"), dict) else {}
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    items = [{
        "target": PLAN_TARGET,
        "name": "整份计划",
        "fields": list(PLAN_FIELDS),
        "current": {
            "project_name": overview.get("project_name"),
            "total_duration_days": overview.get("total_duration_days"),
            "planned_start_date": overview.get("planned_start_date"),
            "planned_end_date": overview.get("planned_end_date"),
            "plan_title": meta.get("plan_title"),
            "level": meta.get("level"),
            "cost": meta.get("cost"),
            "segment": meta.get("segment"),
        },
    }]
    for leaf in iter_leaves(plan)[:max_items]:
        tid = leaf.get("id")
        if tid is None:
            continue
        binding = leaf.get("norm_binding")
        binding = binding if isinstance(binding, dict) else {}
        current = {
            "name": leaf.get("name"),
            "quantity": leaf.get("quantity"),
            "unit": leaf.get("unit", ""),
            "duration_days": leaf.get("duration_days"),
            "norm": {
                "norm_value": binding.get("norm_value"),
                "unit": binding.get("unit", ""),
                "crew": binding.get("crew") or {},
                "source_code": binding.get("source_code", ""),
            },
        }
        if isinstance(leaf.get("workface_capacity"), dict):
            current["workface_capacity"] = leaf["workface_capacity"]
        items.append({
            "target": str(tid),
            "name": str(leaf.get("name") or tid),
            "fields": list(ITEM_LEAF_FIELDS),
            "current": current,
        })
    return items


def _normalize_items(items, plan) -> List[Dict]:
    """外部注入的 revise_targets 归一：至少要有 target，其余字段补默认。"""
    result = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        target = it.get("target") or it.get("id")
        if target is None:
            continue
        fields = it.get("fields")
        if not isinstance(fields, list) or not fields:
            # 没给字段表就按 target 猜一套（第 36 轮 Phase 3）：计划项给计划级字段，
            # 工序项给工序字段。旧实现一律给全集，等于允许"在某条工序上改项目名"。
            fields = list(PLAN_FIELDS) if str(target) == PLAN_TARGET \
                else list(ITEM_LEAF_FIELDS)
        result.append({
            "target": str(target),
            "name": str(it.get("name") or target),
            "fields": [str(f) for f in fields],
            "current": it.get("current") if isinstance(it.get("current"), dict) else {},
        })
    if not result:
        result = build_revise_items(plan)
    return result


# ==================== 规则兜底解析（LLM 不可用 / 返回非法时）====================
def _clean_name(name) -> str:
    """任务说法归一：剥掉"把/将/给"前缀与结尾的"的/字段"残留。"""
    s = _norm_text(name)
    s = re.sub(r"^[把将给]", "", s)
    s = re.sub(r"的[^的]{0,6}$", "", s)
    s = s.strip("的")
    return s


def _match_item(name_text, items) -> Tuple[Optional[Dict], str]:
    """把用户话里的"任务说法"落到清单里的某一项。

    返回 (item 或 None, 原因)。优先精确 id / 精确名字，再退到包含匹配（长名优先）。
    """
    t = _clean_name(name_text)
    if not t:
        return None, "用户原话里没点名要改哪个任务"
    for it in items:                                  # 精确 id / 精确名字
        if t == _norm_text(it["target"]) or t == _norm_text(it["name"]):
            return it, ""
    hits = []
    for it in items:                                  # 包含匹配：清单名出现在说法里
        nm = _norm_text(it["name"])
        if nm and nm in t:
            hits.append((len(nm), it))
    if not hits:                                      # 反向：说法出现在清单名里
        for it in items:
            nm = _norm_text(it["name"])
            if t and t in nm:
                hits.append((len(t), it))
    if not hits:
        return None, "在可修改项清单里没找到「%s」对应的任务" % name_text
    hits.sort(key=lambda x: -x[0])
    best_len, best = hits[0]
    same = [it for ln, it in hits if ln == best_len]
    if len(same) > 1:
        ids = "、".join(str(it["target"]) for it in same[:4])
        return None, "「%s」同时匹配到多个任务（%s），说清楚是哪一个再改" % (name_text, ids)
    return best, ""


# ==================== 能力边界：说清楚"能改什么 / 改不了什么" ====================
# 第 36 轮 Phase 3。用户的原话是「基本什么都改不了」——这里有两层原因：
#   ① 确实有一部分改不了（项目参数、资源机械、逻辑关系）；
#   ② 更伤的是**系统从不说明自己支持什么**：一句「层数改成 5 层」只换来
#      "这句话我没看出要改哪一项"，用户无从知道该换个什么说法。
# 所以：一条都没生效时，必须回一句"做不到什么 + 能做的是…"，而不是沉默。
CAPABILITY_TEXT = (
    "我能直接改的是：① 某条工序的工程量 / 工期 / 定额 / 班组 / 名字；"
    "② 增删一条工序；③ 项目名称；④ 开工日期；⑤ 总工期目标。"
    "改不了的（那属于重新生成、不是修订）：栋数 / 层数 / 建筑面积这类项目参数、"
    "资源限额与机械投入、工序之间的逻辑关系。"
)
UNSUPPORTED_HINTS = [
    (("层数", "层高", "栋数", "建筑面积", "占地面积", "结构形式", "结构类型",
      "建筑类型", "工程类型", "业态", "总建筑面积"),
     "改「%s」属于**项目参数**，会牵动 WBS 与全部定额绑定，不是修订能承担的 —— "
     "要改请按新参数重新生成一份计划。"),
    (("塔吊", "施工电梯", "机械", "设备投入", "资源限额", "人力上限", "劳动力上限",
      "用工人数上限"),
     "「%s」属于资源 / 机械配置，修订目前不支持；可以先改工序的工程量或班组。"),
    (("紧前", "紧后", "前置", "依赖", "逻辑关系", "搭接", "流水段", "流水"),
     "「%s」（工序之间的逻辑关系）修订目前不支持；可以先增删工序。"),
]


def capability_hint(text) -> str:
    """针对用户原话给一句"做不到 + 能做的是…"。

    只在**一条修改都没生效**时使用（调用方保证），所以不会出现"改动成功了却
    被告知做不到"这种自相矛盾。
    """
    t = _norm_text(text)
    for keys, tpl in UNSUPPORTED_HINTS:
        for k in keys:
            if k and k in t:
                return (tpl % k) + " " + CAPABILITY_TEXT
    return CAPABILITY_TEXT


def _special_patches(seg) -> List[Dict]:
    """工序改名 / 开工日期 / 总工期目标 / 增删工序 —— 必须排在通用句式之前的那批。

    返回 [] 表示"这句不是特殊句式"，交回通用解析。命中就只返回这一条（一个分句
    只做一件事，与 `_rule_patches` 里 `break` 的既有约定一致）。

    为什么这批要专门抽出来：通用句式 `_PAT_TARGET_FIELD` / `_PAT_TARGET_ONLY` /
    `_PAT_NOBA` 的模型是「把 <任务> 的 <字段> 改成 <数字>」，对这批句子会
      · 把「把 5.1.1.1 的名字改成 X」认成**项目**改名（名词组同形）；
      · 把「把开工日期改到 2026-07-01」的值截成 "2026"（`_NUM_TOKEN` 不认日期）；
      · 把「总工期改成 306 天」解析成"任务名叫『总工期』、字段为空"，然后在清单里
        找不到这个任务而被丢掉（用户看到的就是"什么都没改"）。
    """
    # ① 工序改名：有显式点号编号 + 命名动词 → 一定是工序（两种写法都试）
    m = None
    for _pat in _TASK_RENAME_PATS:
        m = _pat.search(seg)
        if m:
            break
    if m:
        tid = (m.group("target") or "").strip()
        value = (m.group("value") or "").strip(" 。，,.;；")
        if tid and value:
            return [{"target": tid, "field": "name", "value": value, "scope": "auto",
                     "reason": "规则解析：工序 %s 改名为「%s」" % (tid, value)}]

    # ② 开工日期（计划级）
    m = _PAT_START_DATE.search(seg)
    if m:
        value = (m.group("value") or "").strip()
        if value:
            # 「把 5.1.1.1 的开工日期改到 X」是在改**某一条工序**的开工日期，而这里
            # 动的是整份计划的日历。默默把整个计划平移过去是"改错对象"——比改不动更糟。
            # 直接拒掉并说清楚只能改计划级开工日期，让用户改口径。
            tid_m = re.search(_TASK_ID, seg[:m.start("value")])
            if tid_m:
                return [{"target": tid_m.group("target"), "field": "start_date",
                         "value": value, "scope": "auto", "rejected": True,
                         "reason": "单条工序的开工日期改不了（改的只能是整份计划的："
                                   "「把开工日期改到 %s」会整体平移）。这条可以改的是"
                                   "工程量 / 工期 / 定额 / 班组 / 名字。" % value}]
            return [{"target": PLAN_TARGET, "field": "start_date", "value": value,
                     "scope": "auto", "reason": "规则解析：开工日期 → %s" % value}]

    # ③ 总工期目标（计划级）
    m = _PAT_TARGET_DURATION.search(seg)
    if m:
        raw = m.group("value")
        days = _num(raw)
        if days is None:                       # "十五天"这种中文数字
            ci = _cn_to_int(raw)
            days = float(ci) if ci is not None else None
        if days is not None and days >= 1:
            return [{"target": PLAN_TARGET, "field": "target_duration",
                     "value": int(days), "scope": "auto",
                     "reason": "规则解析：总工期目标 → %d 天" % int(days)}]

    # ④ 增工序。先说清位置的说法优先（target = 插在它后面），
    #    否则新任务只能落进第一个工作包，用户多半找不到。
    m = _PAT_ADD_TASK_AT.search(seg)
    if m:
        name = (m.group("value") or "").strip(" 。，,.;；")
        at = (m.group("target") or "").strip()
        if len(name) >= 2 and at:
            return [{"target": at, "field": "add_task",
                     "value": {"name": name}, "scope": "auto",
                     "reason": "规则解析：在 %s 之后新增工序「%s」" % (at, name)}]
    m = _PAT_ADD_TASK.search(seg)
    if m:
        name = (m.group("value") or "").strip(" 。，,.;；")
        if len(name) >= 2:
            return [{"target": PLAN_TARGET, "field": "add_task",
                     "value": {"name": name}, "scope": "auto",
                     "reason": "规则解析：新增工序「%s」" % name}]

    # ⑤ 删工序（目标就是那条工序）
    m = _PAT_REMOVE_TASK.search(seg) or _PAT_REMOVE_TASK2.search(seg)
    if m:
        tid = (m.group("target") or "").strip()
        if tid:
            return [{"target": tid, "field": "remove_task", "value": None,
                     "scope": "auto", "reason": "规则解析：删除工序 %s" % tid}]
    return []


def _rule_patches(text, items) -> List[Dict]:
    """规则兜底：解析"把 <任务名或id> 的 <字段> 改成/加到 <数值>"这类句式。

    支持两种写法：
      ① 把 5.1.1.1 的工期改成 20            → duration
      ② 把地下室混凝土工加到30人，工期压到15天 → crew + duration（同一任务的多处修改）
    解析不出的部分静默跳过（宁缺勿错），由上层用 warnings 回报。
    """
    norm = _norm_text(text)
    patches = []
    if not norm:
        return patches

    # ⓪ 工序改名先于计划改名（第 36 轮 Phase 3）：两者句式同形（都带"名字/名称"），
    #    有显式工序编号的一定是**工序**改名。不先判就会把「把 5.1.1.1 的名字改成 X」
    #    写成项目名 —— 用户想改一条工序，结果整份计划换了名字。
    if not any(p.search(norm) for p in _TASK_RENAME_PATS):
        # 计划级字段先判（第 35 轮）：改的是计划名，不是某条工序，所以**不走**任务匹配。
        # 放在最前面是因为句式里没有任务名，后面的 `_match_item` 必然匹配失败。
        mt = _PAT_PLAN_TITLE.search(norm) or _PAT_PLAN_TITLE2.search(norm)
        if mt:
            value = (mt.group("value") or "").strip(" 。，,.;；")
            if len(value) >= 2:
                return [{"target": PLAN_TARGET, "field": "plan_title", "value": value,
                         "scope": "auto", "reason": "规则解析：计划名称 → %s" % value}]

    segments = [s for s in re.split(r"[，。；,;]", norm) if s]
    all_roles = _roles_of_items(items)
    # 记住"把 X 的…"里的 X，供后续分句（"工期压到15天"）继续用
    last_item = None
    last_role = ""

    for seg in segments:
        matched = False
        # 特殊句式先行（第 36 轮 Phase 3）：详见 `_special_patches` 的说明。
        special = _special_patches(seg)
        if special:
            patches.extend(special)
            # 工序改名 / 删工序都点名了具体任务，后续分句（"工期压到15天"）可以接着用它
            if special[0]["field"] in ("name", "remove_task"):
                last_item = _find_item(items, special[0]["target"]) or last_item
            continue
        for pat in (_PAT_TARGET_FIELD, _PAT_TARGET_ONLY, _PAT_NOBA):
            m = pat.search(seg)
            if not m:
                continue
            name = m.group("name") or ""
            field_token = m.groupdict().get("field") or ""
            field = _field_of(field_token)
            # ⚠️ 第 35 轮修：`_PAT_TARGET_ONLY` 这类句式**有** field 组但没匹配到
            # （返回 ""），于是"字段名被写进 name 里"的情况（"钢筋工程**量**改为1000"）
            # 从来没走到过兜底 —— 实测表现为"钢筋工程量"整句解析不出字段而被丢弃。
            # 无 field 组的句式才从 name 里再认一次字段。
            if field is None and "field" not in m.re.groupindex:
                field = _field_of(name)
            value = _num(m.group("value"))
            role = ""
            if field is None:
                # "把地下室混凝土工加到30人" → 点名的是工种，字段按 crew 处理；
                # 名字里只有工种、没有任务名时，沿用上一句点名的任务。
                # 先在名字里找，找不到再拿**整句**找一次（宽松句式"主体结构钢筋工加到60人"
                # 里工种被夹在名字中间，只看名字组会漏）。
                role = _role_in(name, all_roles) or _role_in(seg, all_roles)
                if role:
                    field = "crew"
                    if last_item is not None:
                        name = last_item["name"]
            elif field == "crew":
                role = _role_in(seg, all_roles)
            if field is None or value is None:
                continue
            item, _why = _match_item(name, items)
            # 第 35 轮：`name` 里同时含**字段名**时（"钢筋工程量"里的"工程量"），
            # 说明任务名只是其中一部分，前面的包含匹配很可能绑到同名短工序（如"钢筋"）。
            # 这种情况一律走关键字搜索，"唯一命中才改、多条要求说编号"。
            weak = bool(item is not None and field
                        and _norm_text(item.get("name")) and
                        _norm_text(item.get("name")) in _norm_text(name) and
                        _norm_text(item.get("name")) != _norm_text(name))
            if item is None or weak:
                hits = _match_by_keyword(name, items)
                if len(hits) == 1:
                    item = hits[0]
                elif len(hits) > 1:
                    preview = "、".join(str(h.get("target")) for h in hits[:4])
                    patches.append({"target": name, "field": field, "value": value,
                                    "scope": "auto", "rejected": True,
                                    "reason": "「%s」同时匹配到 %d 条工序（%s…），"
                                              "说清楚要改哪一条（请用编号）"
                                              % (name, len(hits), preview)})
                    matched = True
                    break
                else:
                    item = None
            if item is None:
                continue
            last_item = item
            last_role = role or _role_in(seg, _roles_of_item(item) + all_roles) or last_role
            patches.append(_make_patch(item, field, value, last_role, seg))
            matched = True
            break
        if matched:
            continue

        # 分句里只出现"字段 + 数值"（如"工期压到15天"）→ 挂到最近一次点名的任务
        m2 = re.search(r"(?P<field>[^0-9]{0,8}?)\s*" + _ACTION + r"\s*" + _NUM_TOKEN, seg)
        if m2 and last_item is not None:
            field = _field_of(m2.group("field"))
            value = _num(m2.group("value"))
            if field and value is not None:
                role = _role_in(seg, _roles_of_item(last_item) + all_roles) or last_role
                patches.append(_make_patch(last_item, field, value, role, seg))

    # 完全解析不出来时，若整句话正好点中唯一的一项，至少把它认下来（只认目标不改值）
    if not patches:
        only, _why = _match_item(norm, items)
        if only is not None:
            patches.append({"target": only["target"], "field": "",
                            "value": None, "scope": "auto",
                            "reason": "规则解析：只认出了任务，没能识别要改的字段"})
    return patches


def _roles_of_item(item) -> List[str]:
    """某清单项现有的工种名（用于优先匹配用户嘴里的工种）。"""
    if not isinstance(item, dict):
        return []
    crew = ((item.get("current") or {}).get("norm") or {}).get("crew")
    if isinstance(crew, dict):
        return [str(k) for k in crew.keys()]
    return []


def _roles_of_items(items) -> List[str]:
    """全部清单项出现过的工种名。"""
    roles = []
    for it in items or []:
        for role in _roles_of_item(it):
            if role not in roles:
                roles.append(role)
    return roles


def _match_by_keyword(name_text, items) -> List[Dict]:
    """按关键字（材料/工种/部位）找候选工序 —— 用户不知道编号时用。

    例："钢筋" → 所有名字里含"钢筋"的工序。返回候选列表（可能 0 / 1 / 多条），
    由调用方决定"唯一命中才改，多条要求说清楚编号"。

    为什么不直接改全部候选：改工程量会连带重算工期与资源，一次改十几条工序
    用户根本无从核对；宁可让他补一个编号，也不要悄悄改一大片中间数据。
    """
    t = _clean_name(name_text)
    if len(t) < 2:
        return []
    hits = [it for it in items if t in _norm_text(it.get("name"))]
    # 退一步：按两字切片（"钢筋混凝土" → "钢筋"）再找一次，提高召回
    if not hits and len(t) > 2:
        for n in range(2, min(len(t), 5)):
            hits = [it for it in items if t[:n] in _norm_text(it.get("name"))]
            if hits:
                break
    return hits


def _make_patch(item, field, value, role, reason) -> Dict:
    """把"字段+数值"组装成规范 patch（crew 组装成 {工种: 人数}）。"""
    if field == "crew":
        roles = _roles_of_item(item)
        chosen = role or (roles[0] if roles else "人工")
        val = {chosen: int(value)}
    elif field in ("quantity", "duration", "norm"):
        num = float(value)
        val = int(num) if num.is_integer() else num
    else:
        val = value
    return {"target": item["target"], "field": field, "value": val,
            "scope": "auto", "reason": "规则解析：%s" % reason}


# ==================== LLM 翻译 ====================
def _load_prompt() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "prompts", "revise_intent.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "把用户原话翻译成 JSON：{\"patches\":[{\"target\":..,\"field\":..,\"value\":..}]}，只输出 JSON。"


def _coerce_norm_value(value) -> Any:
    """norm 字段值归一：数字 → 数字；字典 → 只保留认识的键。"""
    if isinstance(value, dict):
        keep = {}
        for k in ("norm_value", "unit", "source_code", "condition_text",
                  "quantity_basis", "match_type", "mode"):
            if k in value:
                keep[k] = value[k]
        if "norm_value" in keep:
            num = _num(keep["norm_value"])
            if num is not None:
                keep["norm_value"] = num
        return keep or value
    num = _num(value)
    return value if num is None else (int(num) if float(num).is_integer() else num)


def _normalize_patch(raw, items) -> Optional[Dict]:
    """把 LLM 吐出来的一条 patch 归一成规范字典；结构不对返回 None。"""
    if not isinstance(raw, dict):
        return None
    patch = {
        "target": str(raw.get("target") or "").strip(),
        "field": str(raw.get("field") or "").strip(),
        "value": raw.get("value"),
        "scope": str(raw.get("scope") or "auto").strip() or "auto",
        "reason": str(raw.get("reason") or "").strip(),
    }
    for alias, field in FIELD_ALIASES:              # 容忍模型写成中文
        if patch["field"] == alias:
            patch["field"] = field
            break
    if patch["field"] == "norm":
        patch["value"] = _coerce_norm_value(patch["value"])
    elif patch["field"] in ("quantity", "duration"):
        num = _num(patch["value"])
        patch["value"] = patch["value"] if num is None else (
            int(num) if float(num).is_integer() else num)
    elif patch["field"] == "crew":
        val = patch["value"]
        if isinstance(val, dict):
            patch["value"] = {str(k): v for k, v in val.items()}
        else:
            num = _num(val)
            if num is not None:
                roles = _roles_of_item(_find_item(items, patch["target"]), items)
                patch["value"] = {roles[0] if roles else "人工": int(num)}
    if not patch["target"]:
        return None
    return patch


def _find_item(items, target) -> Optional[Dict]:
    for it in items or []:
        if str(it.get("target")) == str(target):
            return it
    return None


# ==================== 校验 ====================
def _workface_limit(leaf) -> Optional[float]:
    """取该叶子的工作面容量上限（**与排程 / 资源节点同一个公式**）。

    第 39 轮：原来读 `leaf.workface_capacity["max_labor"]` —— 那是 crew_bind 写进
    叶子的**旧表常数**，与 scheduler 的 v2 标定公式不是同一个数（实测 1.5.1
    混凝土运输：旧表 4 人 vs 公式 15 人）。修订路径若按旧表卡，用户改上去的班组
    会被悄悄压回旧口径 —— 一条计划两套上限。
    """
    if not isinstance(leaf, dict):
        return None
    try:
        from . import scheduler as _sched
        cap_l, _cap_m = _sched.workface_limits_from_rule(
            leaf, _num(leaf.get("quantity")), str(leaf.get("unit") or ""))
        if cap_l is not None and cap_l > 0:
            return cap_l
    except Exception:
        pass
    # 公式算不出来（该 L4 没有 v2 标定行）→ 才退回计划里带的静态键
    cap = leaf.get("workface_capacity")
    if isinstance(cap, dict):
        limit = _num(cap.get("max_labor"))
        if limit is not None and limit > 0:
            return limit
    # 域 1.6（第 6 批）：原先这里还有一步 `kb.workface_capacity(act)` 回查
    # `Workface_Capacity_Rule` —— 该表已删除，回查恒定返回 None，只会掩盖
    # "数据源已不存在"。已删除：上限只认叶子自带的 `workface_capacity` 键。
    return None


def _clamp_crew(plan, patch, warnings) -> Dict:
    """crew 越界校验：超过工作面容量上限 → 按上限执行，并把原值如实记进 warnings。"""
    # 只处理 crew（第 36 轮 Phase 3）：add_task 的值也是个 {名字: ...} 形状的字典，
    # 不挡一下就会把它当成班组人数去套工作面容量上限。
    if str(patch.get("field") or "") != "crew":
        return patch
    leaf = None
    for item in iter_leaves(plan):
        if str(item.get("id")) == str(patch.get("target")):
            leaf = item
            break
    if leaf is None:
        return patch
    limit = _workface_limit(leaf)
    if limit is None:
        return patch
    value = patch.get("value")
    if not isinstance(value, dict):
        return patch
    clamped = dict(value)
    hit = {}
    for role, val in value.items():
        num = _num(val)
        if num is not None and num > limit:
            clamped[str(role)] = int(limit)
            hit[str(role)] = int(num)
    if not hit:
        return patch
    new_patch = dict(patch)
    new_patch["value"] = clamped
    for role, original in hit.items():
        warnings.append(
            "%s 的%s %d 人超过工作面容量上限 %d 人，已按上限执行并标注"
            % (patch.get("target"), role, original, int(limit)))
    return new_patch


def _validate(plan, items, patch) -> Tuple[bool, str, Dict]:
    """一条 patch 的准入校验：返回 (是否可用, 拒绝原因, 归一后的 patch)。"""
    if not isinstance(patch, dict):
        return False, "patch 不是字典", patch
    # 规则层已判定"这条不该执行"的（例如"改单条工序的开工日期"这种会改错对象的请求）：
    # 标记就是结论，直接拦下并把它写好的理由交给用户。
    # ⚠️ 必须放在最前面：否则一个 `field="start_date"` 的拒绝项会落进下面的计划级分支，
    # 被归一成 target="plan" 后**照样执行** —— 拦下的东西又跑出去了。
    if patch.get("rejected"):
        return False, str(patch.get("reason") or "这条修改被拦下了"), patch
    target = str(patch.get("target") or "").strip()
    field = str(patch.get("field") or "").strip()
    if not target:
        return False, "patch 缺少 target（没点名要改哪个任务）", patch
    # 计划级字段（第 35 轮）：改的是计划本身（如改名），没有任务 id，**不走**任务清单校验。
    # 放在最前面是因为 `target="plan"` 必然在清单里找不到，否则改名会被误拒。
    if field == "plan_title":
        value = str(patch.get("value") or "").strip()
        if len(value) < 1:
            return False, "新名称是空的（例如：把项目名称改为 NUS 大楼）", patch
        if len(value) > 60:
            return False, "新名称太长（最多 60 字）：%s…" % value[:20], patch
        out = dict(patch)
        # 计划级字段的 target 一律归一到 "plan"：模型/规则可能塞一个乱编号进来
        # （实测 target="9.9.9" 也放行了），留着会让"受影响任务"里出现一个不存在的
        # 任务编号，下游按它去找任务全部落空。
        out["target"] = PLAN_TARGET
        out["value"] = value
        return True, "", out
    # 计划级字段（第 36 轮 Phase 3）：开工日期 / 总工期目标 / 新增工序。
    # 三个都是"改计划本身"，没有工序 id，所以同样**不走**任务清单校验 —— 与改名同理：
    # 放进 `_find_item` 必然找不到 target 而被误拒。
    if field == "start_date":
        raw = str(patch.get("value") or "").strip().replace("/", "-").replace(".", "-")
        try:
            dt = time.strptime(raw, "%Y-%m-%d")
        except (ValueError, TypeError):
            return False, ("开工日期格式不对（要写成 2026-07-01 这样）：%r"
                           % (patch.get("value"),)), patch
        out = dict(patch)
        out["target"] = PLAN_TARGET
        out["value"] = "%04d-%02d-%02d" % (dt.tm_year, dt.tm_mon, dt.tm_mday)
        return True, "", out
    if field == "target_duration":
        num = _num(patch.get("value"))
        if num is None or num < 1:
            return False, "总工期目标要是个正数天数：%r" % (patch.get("value"),), patch
        out = dict(patch)
        out["target"] = PLAN_TARGET
        out["value"] = int(num)
        return True, "", out
    if field == "add_task":
        spec = patch.get("value")
        if isinstance(spec, str):
            spec = {"name": spec}
        if not isinstance(spec, dict):
            return False, "新增工序要给出名字（例如：增加一个工序：地下室防水）", patch
        name = str(spec.get("name") or "").strip()
        if not name:
            return False, "新增工序没有名字（例如：增加一个工序：地下室防水）", patch
        if len(name) > 40:
            return False, "新增工序名太长（最多 40 字）：%s…" % name[:20], patch
        out = dict(patch)
        tgt = str(out.get("target") or "").strip()
        if tgt and tgt != PLAN_TARGET and _find_item(items, tgt) is None:
            # 位置必须是清单里真实存在的工序：否则 `apply_patch` 会静默退回
            # "塞进第一个工作包"，用户明明说了插在哪，结果东西出现在别处。
            return False, ("可修改项清单里没有工序 %s，不知道要插在哪条后面"
                           "（不许发明编号）" % tgt), patch
        spec = dict(spec)
        spec["name"] = name
        # 显式编号要校验占用：撞上已有工序**或工作包**都拒（不给编号就让系统自动生成）。
        # 早先 `_validate` 直接透传 spec，撞上工作包编号也放行 —— 与"LLM 不许发明任务 id"
        # 的约定冲突，落盘后同一个编号指向两个不同对象。
        sid = str(spec.get("id") or "").strip()
        if sid and sid in _existing_ids(plan):
            return False, ("编号 %s 已被占用（已有工序或工作包用了它），换个编号，"
                           "或者不给编号让系统自动生成" % sid), patch
        out["target"] = tgt or PLAN_TARGET
        out["value"] = spec
        return True, "", out
    item = _find_item(items, target)
    if item is None:
        return False, "可修改项清单里没有任务 %s（不许发明任务 id）" % target, patch
    if not field:
        return False, "没能识别出要改的字段（清单允许：%s）" % "/".join(item["fields"]), patch
    if field not in SUPPORTED_FIELDS:
        return False, "字段 %s 不受支持（可用：%s）" % (field, "/".join(SUPPORTED_FIELDS)), patch
    if field not in item["fields"]:
        return False, "任务 %s 不允许改字段 %s（允许：%s）" % (
            target, field, "/".join(item["fields"])), patch

    value = patch.get("value")
    if field in ("quantity", "duration"):
        num = _num(value)
        if num is None:
            return False, "%s 的取值不是数字：%r" % (field, value), patch
        if num < 0:
            return False, "%s 不能是负数：%r" % (field, value), patch
        patch = dict(patch)
        patch["value"] = int(num) if float(num).is_integer() else num
    elif field == "crew":
        if isinstance(value, dict):
            if not value:
                return False, "crew 是空字典，没有可执行的工种人数", patch
            for role, val in value.items():
                num = _num(val)
                if num is None or num < 0:
                    return False, "crew 里 %s 的人数非法：%r" % (role, val), patch
        else:
            num = _num(value)
            if num is None or num < 0:
                return False, "crew 的人数非法：%r" % (value,), patch
    elif field == "norm":
        if value is None:
            return False, "norm 的取值为空", patch
        if isinstance(value, dict):
            num = _num(value.get("norm_value"))
            if num is not None and num < 0:
                return False, "定额不能是负数：%r" % (value.get("norm_value"),), patch
        else:
            num = _num(value)
            if num is None:
                return False, "norm 的取值不是数字也不是字典：%r" % (value,), patch
            if num < 0:
                return False, "定额不能是负数：%r" % (value,), patch
    elif field == "name":
        # 工序改名（第 36 轮 Phase 3）：值必须是 1..40 字的文本。
        # 不做"看起来像工序名"的额外猜测 —— 名字是用户的自由，猜只会猜错。
        text = str(value or "").strip()
        if not text:
            return False, "新工序名是空的（例如：把 5.1.1.1 的名字改成 地下室防水）", patch
        if len(text) > 40:
            return False, "新工序名太长（最多 40 字）：%s…" % text[:20], patch
        patch = dict(patch)
        patch["value"] = text
    return True, "", patch


# ==================== 默认重算（不依赖排程器）====================
def _accepts_two_args(fn) -> bool:
    """回调是否收两个位置参数（用于兼容只收 ctx 的旧签名）。"""
    try:
        import inspect
        sig = inspect.signature(fn)
        params = [p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values()):
            return True
        return len(params) >= 2
    except (TypeError, ValueError):
        return True


def _leaf_productivity(leaf) -> Optional[float]:
    """单个工人的日产能（单位 / 工日），取不到返回 None。

    第 37 轮修正：`labor_norm_value` 落库时**已经归一**为「工日 / 1×单位」
    （留档不变式 `raw_value / raw_quantity_basis == labor_norm_value`），
    所以产能 = `1 / norm_value`；`quantity_basis`（= `raw_quantity_basis`）
    只作溯源、**不参与乘法**。

    旧写法 `norm_value / quantity_basis` 把产能**缩小** basis 倍（工期放大 basis 倍），
    与对称的另一处旧写法 `basis / norm_value`（产能放大 basis 倍）互为镜像错误；
    两者都已在第 37 轮改掉（scheduler.py / resource.py / recompute.py 同批）。
    机械台班定额未归一，仍须乘 basis —— 那条路径不经过本函数。
    """
    binding = leaf.get("norm_binding")
    if not isinstance(binding, dict):
        return None
    prod = _num(binding.get("productivity_value"))
    if prod is not None and prod > 0:
        return prod
    norm_value = _num(binding.get("norm_value"))
    if norm_value is None or norm_value <= 0:
        return None
    prod = 1.0 / norm_value
    return prod if prod > 0 else None


def default_recompute(ctx, affected_ids) -> Dict:
    """默认重算回调：只按 quantity / norm 重算这些叶子自身的 duration_days。

    duration = ceil(quantity / 产能)；产能 = norm_value/quantity_basis × 投入人数。
    取不到产能（没有 norm_binding）就**保持原值**，不做任何猜测；
    **不做排程**（排程由别的节点负责，接线时把这个回调换掉即可）。

    用户**直接点名的任务不重算**：ctx["recompute_locked_ids"] 里的是本轮 patch 直接改过的
    目标（比如"工期改成 20"），它们的值就是用户要的，重算只作用于下游闭包里的其它任务。
    """
    plan = (ctx or {}).get("plan_json") or (ctx or {}).get("wbs")
    plan = plan if isinstance(plan, dict) else {}
    locked = set(str(i) for i in ((ctx or {}).get("recompute_locked_ids") or []))
    wanted = [str(i) for i in (affected_ids or [])]
    changed = []
    for leaf in iter_leaves(plan):
        tid = str(leaf.get("id"))
        if tid not in wanted or tid in locked:
            continue
        quantity = _num(leaf.get("quantity"))
        prod = _leaf_productivity(leaf)
        if quantity is None or quantity <= 0 or prod is None:
            continue
        binding = leaf.get("norm_binding") or {}
        crew = binding.get("crew") if isinstance(binding.get("crew"), dict) else {}
        workers = 0.0
        for val in crew.values():
            num = _num(val)
            if num and num > 0:
                workers += num
        if workers > 0:
            prod = prod * workers
        days = int(math.ceil(quantity / prod))
        if days < 1:
            days = 1
        old = _num(leaf.get("duration_days"))
        if old is None or int(old) != days:
            leaf["duration_days"] = days
            changed.append({"target": tid, "old_duration_days": old,
                            "new_duration_days": days})
    return {"changed": changed, "duration_changes": changed,
            "summary": "按工程量/定额重算了 %d 项任务的工期" % len(changed)}


# ==================== 节点 ====================
def _existing_ids(plan):
    """计划里已占用的编号：叶子 + **工作包**。

    新增任务的显式编号必须跟这个集合比：工作包一旦有了子工序，它自己就不再是叶子
    （`iter_leaves` 只产出子项），只跟叶子比会漏掉"编号撞上某个工作包"，计划里于是
    出现两个同号对象。模块头写着"LLM 不许发明任务 id"，这里就是那道闸门。
    """
    ids = set()
    for leaf in iter_leaves(plan):
        if isinstance(leaf, dict) and leaf.get("id") is not None:
            ids.add(str(leaf.get("id")))
    wbs = (plan or {}).get("wbs") if isinstance(plan, dict) else None
    phases = wbs.get("phases") if isinstance(wbs, dict) else None
    for phase in phases if isinstance(phases, list) else []:
        if not isinstance(phase, dict):
            continue
        for wp in (phase.get("work_packages") or []):
            if isinstance(wp, dict) and wp.get("id") is not None:
                ids.add(str(wp.get("id")))
    return ids


def _merge_rule_first(llm_patches, rule_patches) -> List[Dict]:
    """把规则层高置信度的结果并进模型结果，避免"一句话两个意图丢一半"。

    规则层对"计划改名 / 开工日期 / 总工期目标 / 工序改名"是确定性解析，比模型可靠；
    但一句话里可能还有别的意图（那部分归模型）。两边都保留，按 (target, field) 去重，
    模型已经给出的以模型为准（它可能读到更细的补充信息）。被拒的规则项不并进来 ——
    那类是要如实报给用户的，不是要执行的。
    """
    out = list(llm_patches or [])
    seen = set((str(p.get("target") or ""), str(p.get("field") or "")) for p in out)
    for p in rule_patches or []:
        if not isinstance(p, dict) or p.get("rejected"):
            continue
        if str(p.get("field") or "") not in RULE_FIRST_FIELDS:
            continue
        key = (str(p.get("target") or ""), str(p.get("field") or ""))
        if key not in seen:
            out.append(p)
            seen.add(key)
    return out


class ReviseNode(BaseNode):
    name = "revise"
    title = "自然语言修改"

    def __init__(self, llm=None, store=None, recompute=None):
        BaseNode.__init__(self)
        self.llm = llm
        self.store = store
        self.recompute = recompute or default_recompute

    # ---------- 输入归一 ----------
    @staticmethod
    def _get_plan(ctx) -> Dict:
        plan = ctx.get("plan_json")
        if isinstance(plan, dict) and plan:
            return plan
        wbs = ctx.get("wbs")
        if isinstance(wbs, dict):
            return {"wbs": wbs, "dependencies": ctx.get("dependencies") or ctx.get("deps") or []}
        return {}

    @staticmethod
    def _get_text(ctx) -> str:
        for key in ("user_instruction", "prompt", "user_text", "instruction", "raw_text"):
            val = ctx.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    @staticmethod
    def _get_deps(plan, ctx) -> List[Dict]:
        deps = ctx.get("dependencies")
        if not isinstance(deps, list) or not deps:
            deps = ctx.get("deps")
        if not isinstance(deps, list) or not deps:
            deps = plan.get("dependencies") if isinstance(plan, dict) else []
        return deps if isinstance(deps, list) else []

    # ---------- 主流程 ----------
    def run(self, ctx):
        plan = self._get_plan(ctx)
        text = self._get_text(ctx)
        revision = _blank_revision(text)

        if not isinstance(plan, dict) or not plan:
            revision["warnings"].append("没有可修改的计划（ctx 里既无 plan_json 也无 wbs）")
            ctx["revision"] = self._persist(ctx, revision)
            self.done_summary = "没有可修改的计划，已跳过"
            return {"revision": ctx["revision"]}

        items = _normalize_items(ctx.get("revise_targets"), plan)
        if not text:
            revision["warnings"].append("用户原话为空，没有可执行的修改")
            ctx["revision"] = self._persist(ctx, revision)
            self.done_summary = "用户原话为空"
            return {"revision": ctx["revision"]}

        self.emit("node_progress", {"node": self.name, "progress": 20,
                                    "message": "把自然语言翻译成规范修改指令"})
        candidates, warnings = self._translate(text, items)
        revision["warnings"].extend(warnings)
        revision["patches"] = candidates

        # ---- 校验（不静默通过：target / 字段 / 数值 / 工作面容量）----
        self.emit("node_progress", {"node": self.name, "progress": 45,
                                    "message": "校验修改指令（清单 / 字段 / 数值 / 容量）"})
        usable = []
        for patch in candidates:
            ok, reason, norm_patch = _validate(plan, items, patch)
            if not ok:
                revision["rejected"].append({"patch": patch, "reason": reason})
                continue
            norm_patch = _clamp_crew(plan, norm_patch, revision["warnings"])
            usable.append(norm_patch)

        # ---- 执行：只重放 patch，得到新的计划副本 ----
        self.emit("node_progress", {"node": self.name, "progress": 65,
                                    "message": "代码执行修改"})
        working = deepcopy(plan)
        for patch in usable:
            working, changed_ids, applied = apply_patch(working, patch)
            if applied.get("applied"):
                revision["applied"].append(applied)
            else:
                revision["rejected"].append({
                    "patch": patch, "reason": applied.get("warning") or "执行失败"})

        # ---- 受影响范围：下游闭包（后继的后继…）----
        seeds = [str(p.get("target")) for p in revision["applied"] if p.get("target")]
        # 第 36 轮 Phase 3：**不是所有字段都会往上下游传播**。改个工序名不可能影响
        # 任何别的任务的工期/日期，但闭包照样沿着依赖网展开 —— 实测一句"给 4.1.1.1
        # 改名"会显示"受影响任务 199 项"，用户据此以为动了半份计划。
        # 只有真的进了排程的字段才算传播；计划级字段的影响范围就是它自己。
        applied_fields = set(str(p.get("field") or "") for p in revision["applied"])
        if applied_fields and not (applied_fields - set(_NON_PROPAGATING)):
            affected = list(seeds)
        else:
            affected = dependency_closure(self._get_deps(plan, ctx), seeds)
        revision["affected"] = affected

        # ---- 只重算受影响的部分（通过可注入回调，默认实现不排程）----
        result = {}
        duration_before = self._total_days(plan)
        if affected:
            self.emit("node_progress", {"node": self.name, "progress": 85,
                                        "message": "只重算受影响任务（%d 项）" % len(affected)})
            payload = dict(ctx)
            payload["plan_json"] = working
            payload["dependencies"] = self._get_deps(plan, ctx)
            # 本轮 patch 直接点名的任务不允许被重算覆盖（用户说的值就是最终值）
            payload["recompute_locked_ids"] = seeds
            # 其中**改的是工期**的那些：重算不许把它们排回去，而要反解班组
            # （定额工日不变，要在 N 天内干完就得加人）。改量/改班组的任务不在此列 ——
            # 它们的工期本来就该随工程量重新算出来。
            payload["recompute_duration_locks"] = [
                str(p.get("target")) for p in revision["applied"]
                if str(p.get("field") or "") in ("duration", "duration_days") and p.get("target")]
            # 本次修订真正改了哪些字段（第 36 轮）。计划级字段（项目名称/细度/成本口径/
            # 施工段）跟工期无关，重算回调据此**直接跳过排程** —— 旧实现不管改什么都
            # 一路重排，一句"改个名字"就把 209 条里 98 条工期换了一套口径。
            payload["recompute_touched"] = [
                {"target": str(p.get("target") or ""), "field": str(p.get("field") or "")}
                for p in revision["applied"]]
            result = self._recompute(payload, affected) or {}

        # 重算回调的告警也要让用户看见（第 36 轮）：跳过重排、缺边界条件、
        # 按指定工期反解班组……这些原来只躺在 result 里，`_compose_summary` 数不到，
        # 于是"计划没重排"和"重排口径存疑"对用户完全不可见。
        for _w in (result.get("warnings") or []):
            _w = str(_w or "").strip()
            if _w and _w not in revision["warnings"]:
                revision["warnings"].append(_w)

        # 总工期目标：只说"记下了"是没用的，用户要知道差多少（第 36 轮 Phase 3）。
        # 这一项**刻意不触发重排** —— 想压到目标工期靠的是加人，不是把数字写小；
        # 所以这里如实地把"目标 vs 当前"摆出来，并把"怎么才能更快"讲清楚。
        for _p in revision["applied"]:
            if str(_p.get("field") or "") != "target_duration":
                continue
            _target = _num(_p.get("value"))
            _now = self._total_days(working)
            if _target and _now:
                _now = int(_now)
                _target = int(_target)
                if _target < _now:
                    revision["warnings"].append(
                        "总工期目标已记下（%d 天），但当前排程是 %d 天，差 %d 天。"
                        "修订不会替你压缩工期 —— 定额工日不变，想更快只能加人："
                        "说「把 <工序> 的班组加到 N 人」，或在重新生成时给出 crew_design。"
                        % (_target, _now, _now - _target))
                else:
                    revision["warnings"].append(
                        "总工期目标已记下（%d 天），当前排程 %d 天，留有 %d 天余量。"
                        % (_target, _now, _target - _now))

        # 一条都没生效时，必须给"做不到什么 + 能做的是…"（第 36 轮 Phase 3）。
        # 只看 applied：被 rejected 拦下的不算生效，用户同样需要知道为什么。
        if not revision["applied"]:
            revision["hint"] = capability_hint(text)

        revision["summary"] = self._compose_summary(working, revision, result, duration_before)

        ctx["revision"] = self._persist(ctx, revision)
        ctx["plan_json"] = working
        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": revision["summary"]})
        self.done_summary = revision["summary"]
        return {"revision": ctx["revision"], "plan_json": working}

    # ---------- 翻译 ----------
    def _translate(self, text, items) -> Tuple[List[Dict], List[str]]:
        """LLM 优先；不可用 / 返回非法 → 规则兜底，并如实写 warnings。

        例外（第 35 轮）：**计划级字段**（改名之类）先走确定性规则，命中就**不再调模型**。
        理由有二：① 这类句式（"我想将项目名称改为 NUS 大楼"）没有任务名，模型只会
        在清单里找不到而在 warnings 里报"没点名清单中的任务"，白花一次调用还答不对；
        ② 改名是纯文本，规则解析比模型更可靠（不会自作主张加字或改口径）。
        """
        warnings = []
        pre = _rule_patches(text, items)
        # 命中"信息完整"的句式就不再调模型 —— 但**只限单分句**（第 36 轮 Phase 3）。
        # 一句话里有多个意图时（「把项目名称改为X，工期改成20」），规则层只会返回它先
        # 认出来的那一条，早返回等于把另外半句吞掉（实测只改了名字、工期那半句没了）。
        # 多分句交给模型；模型不可用会退回规则解析，不会更差。
        if any(p.get("field") in RULE_FIRST_FIELDS for p in pre) and _single_intent(text):
            return pre, warnings
        if self.llm is not None:
            try:
                patches = self._llm_patches(text, items)
                if patches is not None:
                    # 规则层确认过的计划级意图不能被模型漏掉（多分句时两边都要保留）
                    patches = _merge_rule_first(patches, pre)
                    if not patches:
                        warnings.append("LLM 未给出任何修改指令（原话里可能没点名清单中的任务）")
                    return patches, warnings
                warnings.append("LLM 返回的内容不是合法 JSON 指令，已用规则解析")
            except Exception as e:                       # LLM 网络/结构异常一律兜底
                warnings.append("LLM 不可用（%s），已用规则解析" % str(e)[:120])
        else:
            warnings.append("LLM 不可用，已用规则解析")

        patches = _rule_patches(text, items)
        if not patches:
            warnings.append("规则解析也没能识别出可执行的修改，请换一种说法（如：把 5.1.1.1 的工期改成 20）")
        return patches, warnings

    def _llm_patches(self, text, items) -> Optional[List[Dict]]:
        system = _load_prompt()
        payload = {
            "用户原话": text,
            "可修改项清单": items,
        }
        user = json.dumps(payload, ensure_ascii=False)
        if hasattr(self.llm, "chat_json"):
            data = self.llm.chat_json(system, user, temperature=0.1)
        else:
            raw = self.llm.chat_text(system, user, temperature=0.1)
            data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        raw_patches = data.get("patches")
        if raw_patches is None:
            return None
        if not isinstance(raw_patches, list):
            return None
        out = []
        for raw in raw_patches:
            patch = _normalize_patch(raw, items)
            if patch is not None:
                out.append(patch)
        return out

    # ---------- 重算 ----------
    def _recompute(self, payload, affected) -> Dict:
        """调用可注入回调 recompute(ctx, affected_ids)；也兼容只收一个参数的旧签名。"""
        fn = self.recompute
        if fn is None or not callable(fn):
            return {}
        try:
            return fn(payload, affected) or {}
        except TypeError as e:
            if not _accepts_two_args(fn):
                try:
                    return fn(payload) or {}
                except Exception as e2:
                    return {"error": "重算回调失败：%s" % str(e2)[:120]}
            return {"error": "重算回调失败：%s" % str(e)[:120]}
        except Exception as e:
            return {"error": "重算回调失败：%s" % str(e)[:120]}

    # ---------- 摘要 ----------
    @staticmethod
    def _total_days(plan) -> Optional[float]:
        if not isinstance(plan, dict):
            return None
        for src in (plan.get("overview"), plan.get("cpm_result")):
            if isinstance(src, dict):
                num = _num(src.get("total_duration_days"))
                if num is not None:
                    return num
        return None

    def _compose_summary(self, after, revision, result, duration_before=None) -> str:
        parts = []
        if revision["applied"]:
            names = "；".join(
                "%s 的 %s → %s" % (p.get("target"), p.get("field"), p.get("value"))
                for p in revision["applied"][:5])
            more = "…等 %d 项" % len(revision["applied"]) if len(revision["applied"]) > 5 else ""
            parts.append("生效 %d 项修改：%s%s" % (len(revision["applied"]), names, more))
        else:
            parts.append("没有修改生效")

        # 总工期新旧对比：重算回调若给了新值优先采信，否则读重算后的计划
        if isinstance(result, dict) and _num(result.get("total_duration_days")) is not None:
            d_after = _num(result.get("total_duration_days"))
        else:
            d_after = self._total_days(after)
        if duration_before is not None and d_after is not None:
            if int(duration_before) == int(d_after):
                parts.append("总工期 %d 天（未变）" % int(d_after))
            else:
                parts.append("总工期 %d → %d 天" % (int(duration_before), int(d_after)))
        elif d_after is not None:
            parts.append("总工期 %d 天" % int(d_after))

        if revision["affected"]:
            parts.append("受影响任务 %d 项" % len(revision["affected"]))
        if revision["rejected"]:
            parts.append("其中 %d 项被拦下（见 rejected）" % len(revision["rejected"]))
        if revision["warnings"]:
            parts.append("另有 %d 条提示（越界/降级，见 warnings）" % len(revision["warnings"]))
        head = "改完：" + "；".join(parts)
        # 一条都没生效 → 把"能改什么"直接写进摘要（第 36 轮 Phase 3）。
        # 只塞进 warnings 是不够的：终端在"什么都没改"这条路上更该讲清楚下一步怎么说。
        if not revision["applied"] and revision.get("hint"):
            head += "\n" + str(revision["hint"])
        return head

    # ---------- 存档 ----------
    def _persist(self, ctx, revision) -> Dict:
        """把本轮修改落进修订链（store 为空就只回结果，不落盘）。"""
        out = deepcopy(revision)
        if self.store is None:
            return out
        plan = self._get_plan(ctx)
        plan = plan if isinstance(plan, dict) else {}
        plan_id = None
        # ctx["plan_id"] 优先：引擎/终端传进来的当前档案 id；
        # 退回计划自带的 plan_id，再退回 run_id
        for key in ("plan_id", "run_id"):
            val = ctx.get(key)
            if val:
                plan_id = str(val)
                break
        if not plan_id and plan.get("plan_id"):
            plan_id = str(plan["plan_id"])
        if not plan_id:
            out["warnings"].append("没有 plan_id，本轮修改未落修订链")
            return out

        applied = out.get("applied") or []
        try:
            if self.store.load_baseline(plan_id) is None:
                self.store.save_baseline(plan_id, plan)
            paths = []
            for patch in applied:
                paths.append(self.store.append_revision(
                    plan_id, patch, out["raw_text"], out["affected"], out["summary"]))
            out["revision_files"] = paths
            out["plan_id"] = plan_id
        except Exception as e:                            # 存档失败不影响修改本身
            out["warnings"].append("修订链落盘失败：%s" % str(e)[:120])
        return out
