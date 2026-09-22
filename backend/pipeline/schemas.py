"""统一数据契约（Pydantic）— §5.4 plan_json 的单一真源

所有节点产物、plan_assembler 汇总、落盘、契约测试都以此为准。
字段对齐：技术说明文档 附录A/B + Dify 导出 3.2 进度方案 Schema + v1.1 评审修正
（resources 嵌套、total_duration_days 统一、critical_path_length 自动计算）。
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    """所有契约模型的统一基类：**多出来的字段一律保留**（extra="allow"）。

    为什么必须这样（这是一个真实缺陷的回归护栏）：
    plan_json 落盘前会过一遍 `PlanJson.model_validate(plan).model_dump()`，
    而 pydantic v2 默认 `extra="ignore"` —— 任何没在契约里声明的字段会被**静默丢掉**。
    实测被丢掉的恰恰是产品最要紧的几样东西：

      · 叶子的 `workface_capacity` / `_crew_design` → 排程输入没了
      · 叶子的 `_qty_source` / `_qty_formula`      → **逐值溯源没了**（核心卖点）
      · 定额的 `productivity_value`                 → 修订重算算不出工期
      · meta 的编制口径 / 审计链 / 参数 / 边界条件   → 交付物与 /revise 都受影响

    丢得**不报错、不警告**，落盘的计划看起来完全正常 —— 这正是最危险的一类错误。
    声明式契约仍然有用（类型与默认值），但不该变成"没声明就等于没有"。
    """

    model_config = ConfigDict(extra="allow")


# ==================== v2.1 新增：溯源 / 定额锚定 / 用量 / 元数据 / 修改指令 ====================
class Provenance(_Base):
    """一个数值的来源记录 —— 回答"这个数从哪来"。"""
    value: Any = None
    origin: str = Field(default="unknown",
                        description="user(用户提供) | kb(数据库) | ai(AI假设) | default(默认)")
    ref: str = Field(default="", description="来源标识：规范编号 / 表名 / 用户原话")
    confidence: str = Field(default="", description="高 | 中 | 低")
    note: str = ""


class NormBinding(_Base):
    """一条 L4 锚定的定额（人工或机械）。"""
    task_id: str
    mode: str = Field(default="labor", description="labor(人工主导) | machine(机械主导)")
    norm_value: Optional[float] = None
    unit: str = ""
    condition_text: str = ""
    quantity_basis: float = 1.0
    source_code: str = Field(default="", description="规范/来源代码，如 LD/T 72.8、GD_2018_A1_5")
    match_type: str = Field(default="ai", description="exact(精确命中) | default(典型值) | ai(AI假设)")
    crew: Dict[str, int] = Field(default_factory=dict)
    provenance: Optional[Provenance] = None


class Usage(_Base):
    """一次运行的 token 用量与费用。"""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_cny: float = 0.0
    by_node: Dict[str, int] = Field(default_factory=dict)
    model: str = ""
    note: str = ""


class PlanMeta(_Base):
    """计划元数据：审计链 / 编制口径 / 版本 / 品牌 / 用量 / 可信度 / 两版工期。

    计划必须**自包含**：修订（自然语言改计划）时要按同样口径重排，就得拿得到
    `extracted_params` 与 `boundary_conditions`；交付物要印编制口径与审计链。
    这些字段全部显式声明（基类还允许额外字段，双重保险）。
    """
    audit_status: str = Field(default="未审计", description="未审计 | 已审计")
    audit_rounds: List[Dict[str, Any]] = Field(default_factory=list,
                                               description="三轮回审逐轮结论")
    audit_comments: List[Dict[str, Any]] = Field(default_factory=list,
                                                 description="用户退回时写下的审计意见")
    plan_level: str = Field(default="L4", description="L3 | L4")
    plan_mode: str = Field(default="", description="theory_min(理论最短) | resource_ok(资源不超额)")
    version: str = ""
    brand: Dict[str, str] = Field(default_factory=dict)
    usage: Optional[Usage] = None
    data_sources: List[str] = Field(default_factory=list)
    credibility: Dict[str, float] = Field(default_factory=dict)   # user / kb / ai 占比
    # ---- 编制口径（多栋 / 层数 / 单栋说明）----
    building_count: int = 1
    floors: Optional[int] = None
    caliber_note: str = ""
    total_duration_days: Optional[float] = None
    # ---- 让计划自包含：修订重算要用 ----
    extracted_params: Dict[str, Any] = Field(default_factory=dict)
    boundary_conditions: Dict[str, Any] = Field(default_factory=dict)
    schedule_versions: Dict[str, Any] = Field(default_factory=dict)
    norm_coverage: Dict[str, Any] = Field(default_factory=dict)
    machine_labor_demand: Dict[str, Any] = Field(default_factory=dict)
    # ---- 知识库范围装配（kb_scope）的警告原文 ----
    # 终端默认只打归并后的摘要（同类不刷屏），逐条原文落在这里，交付物可核对。
    kb_warnings: List[str] = Field(default_factory=list,
                                   description="kb_scope 的知识库范围警告（逐条原文）")
    # ---- 修订链（plan_store 会写）----
    revision: int = 0
    revision_label: str = ""
    rebuilt_damaged: List[str] = Field(default_factory=list)


class RevisionPatch(_Base):
    """自然语言修改 → 规范指令（AI 只负责翻译，代码负责执行）。"""
    patch_id: str = ""
    raw_text: str = Field(default="", description="用户原话")
    target: str = Field(default="", description="目标任务 id / 阶段名")
    field: str = Field(default="", description="quantity|duration|norm|crew|dependency|segment|level|cost")
    value: Any = None
    scope: str = Field(default="auto", description="auto | this_node | downstream | full")
    reason: str = ""
    applied: bool = False
    warning: str = ""


# ==================== WBS / 依赖（附录 A） ====================
class SubPackage(_Base):
    id: str
    name: str
    duration_days: int = Field(default=1, ge=1)
    quantity: float = 0
    unit: str = ""
    work_type: str = ""
    kb_activity_id: Optional[str] = None  # KB 结构活动ID（主体结构任务）
    location: Optional[str] = None        # 楼层/部位（如 1F / B1F）
    # ---- v2.1：溯源与定额（均可选，旧产物不受影响）----
    norm_binding: Optional[NormBinding] = None
    provenance: Dict[str, Provenance] = Field(default_factory=dict)


class WorkPackage(_Base):
    id: str
    name: str
    sub_packages: List[SubPackage] = Field(default_factory=list)


class Phase(_Base):
    phase: str
    work_packages: List[WorkPackage] = Field(default_factory=list)


class WBS(_Base):
    phases: List[Phase] = Field(default_factory=list)


class Dependency(_Base):
    predecessor: str
    successor: str
    type: str = Field(default="FS", pattern="^(FS|SS)$")
    lag_days: int = 0


# ==================== CPM（附录 B.1） ====================
class ScheduleItem(_Base):
    task_id: str
    es: int
    ef: int
    ls: int
    lf: int


class CpmResult(_Base):
    total_duration_days: int
    critical_path: List[str] = Field(default_factory=list)
    schedule: List[ScheduleItem] = Field(default_factory=list)


# ==================== 资源定额（v1.1 嵌套结构） ====================
class ResourceQty(_Base):
    per_day: int
    total_days: float


class ResourceTask(_Base):
    task_id: str
    task_name: str
    quantity: float
    planned_duration_days: int
    resources: Dict[str, ResourceQty] = Field(default_factory=dict)
    _matched_keyword: Optional[str] = None
    _warning: Optional[str] = None


class ResourceWarnings(_Base):
    unmatched_tasks: List[dict] = Field(default_factory=list)
    count: int = 0
    message: str = ""


class ResourceDemand(_Base):
    tasks: List[ResourceTask] = Field(default_factory=list)
    _warnings: Optional[ResourceWarnings] = None


# ==================== 进度方案（对齐 Dify 3.2 Schema） ====================
class Overview(_Base):
    project_name: str
    total_duration_days: int = Field(description="= cpm_result.total_duration_days，统一无二义")
    planned_start_date: str
    planned_end_date: str
    critical_path_length: int = Field(description="= len(cpm_result.critical_path)，自动计算")


class Milestone(_Base):
    name: str
    date: str
    task_id: str
    description: str = ""


class ScheduleTask(_Base):
    task_id: str
    task_name: str
    start_date: str
    finish_date: str
    duration_days: int
    assigned_resources: Dict[str, int] = Field(default_factory=dict)


class ResourcePlan(_Base):
    total_manpower_days: float = 0
    peak_manpower: int = 0
    equipment_peak: Dict[str, int] = Field(default_factory=dict)
    # 【第 2 批 · 域 2 / 2.6】`material_summary`（"主要材料"表）**字段已删除** ——
    # 材料清单不再要求 / 不再接受 / 不再展示（交付物改印
    # 「本计划不含材料计划。材料按"管够"处理，不参与工期与资源计算。」）。
    #
    # ⚠️ 为什么删字段是**安全**的、老计划不会被弄坏：`_Base` 是 `extra="allow"`
    # （见文件头那段真实缺陷记录）—— 已落盘的历史计划 JSON 里仍带着
    # `material_summary`，`model_validate` 会把它当**额外字段原样保留**，
    # 因此老计划的重新校验 / 重新出交付物**不受影响**；新计划不再产生这个键。
    # （交付物侧的 `delivery._normalized_resource_plan` 仍会读它做 U+33A1 归一，
    #  那是 G5 的硬要求，与"要不要展示材料清单"是两件事。）


class Risk(_Base):
    risk_name: str
    mitigation: str = ""


class PlanJson(_Base):
    plan_id: str
    overview: Overview
    wbs: WBS
    dependencies: List[Dependency] = Field(default_factory=list)
    cpm_result: CpmResult
    resource_demand: ResourceDemand
    key_milestones: List[Milestone] = Field(default_factory=list)
    critical_path_tasks: List[ScheduleTask] = Field(default_factory=list)
    all_tasks_schedule: List[ScheduleTask] = Field(default_factory=list)
    resource_plan: ResourcePlan = Field(default_factory=ResourcePlan)
    risks: List[Risk] = Field(default_factory=list)
    report: str = ""
    # ---- v2.1：元数据（默认值保证旧产物仍可校验）----
    meta: PlanMeta = Field(default_factory=PlanMeta)
