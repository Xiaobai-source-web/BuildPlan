"""节点2：WBS 三层生成（LLM + 模板校验 + 项目类型模板兜底）— T-10

流程：
1. LLM 按 prompts/wbs_gen.txt（含资源标准词库注入）生成三层 WBS
2. Python 模板校验/归一化（id 唯一、三层结构、duration 缺省1、字段补全）
3. LLM 失败或校验不过 → 按项目类型（住宅/厂房/默认）加载预置模板兜底

支持引擎重跑：/retry 附加指令、/edit 改参后重跑（引擎恢复检查点后本节点重跑）。
"""

import json
import re

from .. import config, kb
from ..base import BaseNode
from ..llm import LLMClient, LLMError
from ..prompts_loader import load
from .kb_scope import normalize_level


# ---- KB 结构工种常用 L4 子集（注入 WBS 提示词）----
# 大工种（土方66/砌筑54/桩基33/架子28）取项目级进度常用的子集，避免全量撑爆 prompt。
# 材料运输与加工工程（名下 116 个 L4 全是"XX运输"、给不出可注入工序）不注入，由 LLM 按词库生成。
# 该 L3 原在 10 个建筑类型下标 REQUIRED，已降级为"可选"（A1 三档化后枚举名 = OPTIONAL，
# 历史名 = USUAL）—— 详见
# devtools/migrate_material_transport_to_usual.py：标着"必含"却无从补起，会让
# missing_kb_essentials() 每次都误报、修复选项永远修不好。
KB_STRUCT_L4 = {
    "rebar": [
        ("REBAR_NEW_COL", "柱钢筋"), ("REBAR_NEW_BEAM", "梁钢筋"),
        ("REBAR_NEW_SLAB", "板钢筋"), ("REBAR_NEW_WALL", "墙钢筋"),
        ("REBAR_NEW_FOUND", "基础钢筋"), ("REBAR_NEW_OTHER", "楼梯及其他钢筋"),
    ],
    "concrete": [
        ("CONC_NEW_FOUND", "基础浇筑"), ("CONC_NEW_COLUMN", "柱浇筑"),
        ("CONC_NEW_BEAM", "梁浇筑"), ("CONC_NEW_SLAB", "板浇筑"),
        ("CONC_NEW_WALL", "墙浇筑"), ("CONC_NEW_STAIR", "楼梯浇筑"),
    ],
    "formwork": [
        ("FORM_NEW_COL", "柱模板"), ("FORM_NEW_BEAM", "梁模板"),
        ("FORM_NEW_FOUND", "基础模板"),
    ],
    "waterproofing": [
        ("WP_NEW_ROLL", "卷材防水"), ("WP_NEW_COAT", "涂料防水"),
        ("WP_NEW_JOINT", "变形缝处理"),
    ],
    "pile_foundation": [
        ("GD_A13_压管桩", "压预制管桩"), ("GD_A13_打管桩", "打预制管桩"),
        ("GD_A13_截凿桩头", "截(凿)桩头"), ("GD_A13_接桩", "接桩"),
        ("GD_A13_旋挖成孔", "旋挖桩成孔"), ("GD_A13_钻孔成孔", "钻孔桩成孔"),
        ("GD_A13_后压浆", "桩底(侧)后压浆"),
    ],
    "earthwork": [
        ("GD_A11_机械挖土方、淤泥流砂", "机械挖土方、淤泥流砂"),
        ("GD_A11_机械挖装土方、淤泥流砂", "机械挖装土方、淤泥流砂"),
        ("GD_A11_自卸汽车运土方、淤泥流砂", "自卸汽车运土方、淤泥流砂"),
        ("GD_A11_回填土(夯实机夯实)", "回填土(夯实机夯实)"),
        ("EARTH0029", "人工挖土方"), ("EARTH0020", "回填土夯实"), ("EARTH0021", "基底钎探"),
    ],
    "masonry": [
        ("MASON0001", "砖基础"), ("LDT724_多孔砖墙", "多孔砖墙"),
        ("LDT724_砌块墙", "砌块墙"), ("LDT724_空心砖墙", "空心砖墙"),
        ("LDT724_零星砌体", "零星砌体"), ("LDT724_砖墙_混水内", "砖墙-混水内"),
        # 第 7 批（2026-09-21）：**补上 ALC 墙板安装**（用户已认可修法 C，源头修绑定）。
        # 为什么必须在这里补：本表是注入 LLM 的"结构主体任务**必须**使用下列 KB 活动"
        # 白名单（见下方 `build_kb_injection`），LLM 只能从中选。原先 masonry 一档里
        # 没有 ALC 墙板活动，于是 `N-N层 ALC墙板安装` 只能被绑到 `LDT724_砌块墙`
        # ——**单位 m³**，而任务按 **m²** 计量 ⇒ `check_unit_pair` 判不可换算 ⇒
        # 定额丢弃、工期退回 WBS（实测 11 条 6.1.1.1.1~.11 全 unusable）。
        # 而 KB 里本来就有单位一致的 `MASON_ALC_PANEL`「ALC墙板安装」（m²，
        # `L4_Norm_Default` 0.095 工日/m²，绑定层的 `_labor_candidates` 会自动回退到它）。
        # ⚠️ 这不是新增数据/新编系数：活动与定额行**都已在 kb.db 里**，这里只是让
        # 候选清单不再漏掉它（漏掉才是缺陷）。
        ("MASON_ALC_PANEL", "ALC墙板安装"),
    ],
    "scaffolding": [
        ("SCAFF0024", "外脚手架搭拆"), ("SCAFF0021", "里脚手架搭拆"),
        ("SCAFF0033", "满堂脚手架搭拆"), ("SCAFF0001", "全封闭密目网搭拆"),
        ("SCAFF0004", "整体提升架搭拆"),
    ],
}


def build_kb_injection(building_type_id):
    """构造 KB L4 注入文本（结构任务词汇 + 排除工种）。

    返回注入字符串；若该建筑类型无可用结构工种返回 None。
    """
    l3s = kb.l3_for(building_type_id)
    if not l3s:
        return None
    lines = []
    for l3 in l3s:
        wt = l3["work_type_id"]
        # A1 三档化：档位统一过 `normalize_level`（历史 USUAL → OPTIONAL），
        # 这样旧库（还是 USUAL）与新库（已是 OPTIONAL）都能正常注入。
        lv = normalize_level(l3["applicability_level"])
        if wt in KB_STRUCT_L4 and lv in ("REQUIRED", "OPTIONAL"):
            acts = KB_STRUCT_L4[wt]
            items = " ".join(f"{name}({aid})" for aid, name in acts)
            level = "必含" if lv == "REQUIRED" else "可选"
            lines.append(f"  {l3['work_type_name']}[{level}]: {items}")
    if not lines:
        return None
    excluded = [x["work_type_name"] for x in l3s
                if normalize_level(x["applicability_level"]) == "EXCLUDED"]
    ex_txt = "；".join(excluded) if excluded else ""
    return (
        "\n\n【结构主体任务必须使用下列 KB 活动（可加楼层前缀，如 1F柱浇筑），"
        "并为每个结构叶子任务写 kb_activity_id 字段】\n"
        + "\n".join(lines)
        + (f"\n\n【排除（不得出现）】{ex_txt}\n" if ex_txt else "")
        + "\n要求：结构任务按项目实际选择、不必全用；"
          "施工准备/装饰/机电/室外/验收等非结构阶段按原词库生成。"
    )


# ---------------- 校验 / 归一化 ----------------
def normalize_wbs(wbs) -> (dict, list):
    """校验并归一化 WBS。返回 (wbs, warnings)。不合规返回 (None, [原因])。"""
    if not isinstance(wbs, dict) or not isinstance(wbs.get("phases"), list) or not wbs["phases"]:
        return None, ["phases 为空"]

    warnings = []
    seen_ids = set()
    phase_no = 0
    for phase in wbs["phases"]:
        if not isinstance(phase, dict):
            return None, ["phase 不是对象"]
        phase_no += 1
        phase.setdefault("phase", f"阶段{phase_no}")
        wps = phase.get("work_packages")
        if not isinstance(wps, list) or not wps:
            return None, [f"阶段「{phase.get('phase')}」缺少 work_packages"]
        wp_no = 0
        for wp in wps:
            wp_no += 1
            sub = wp.get("sub_packages")
            if not isinstance(sub, list) or not sub:
                return None, [f"工作包「{wp.get('name')}」缺少 sub_packages"]
            leaf_no = 0
            for leaf in sub:
                leaf_no += 1
                lid = str(leaf.get("id") or "").strip()
                if not lid:
                    return None, [f"叶子任务缺 id（{wp.get('name')} 第{leaf_no}项）"]
                if lid in seen_ids:
                    return None, [f"id 重复：{lid}"]
                seen_ids.add(lid)
                leaf["id"] = lid
                leaf.setdefault("name", lid)
                try:
                    d = int(leaf.get("duration_days") or leaf.get("planned_duration_days") or 1)
                except (TypeError, ValueError):
                    d = 1
                leaf["duration_days"] = max(1, d)
                leaf.setdefault("quantity", 0)
                leaf.setdefault("unit", "")
                leaf.setdefault("work_type", "土建临建")
                # 补父级 id（如缺省）
                if not wp.get("id"):
                    wp["id"] = f"{phase_no}.{wp_no}"
                if not wp.get("name"):
                    wp["name"] = f"工作包{wp_no}"
    # 叶子 id 若含非数字段（如 LLM 给 1.1.1a），重编号为 1.x.y
    for phase in wbs["phases"]:
        for wp in phase.get("work_packages", []):
            for leaf in wp.get("sub_packages", []):
                if not re.fullmatch(r"\d+(\.\d+)+", leaf["id"]):
                    leaf["id"] = f"{wp['id']}.{leaf['id']}"

    # kb_activity_id 规范化 + 软校验（不阻断）
    #   ① LLM 常把单位拼进编号（如 "GD_A13_压管桩/m"、"EARTH0015/m²"）→ 剥掉能查到的那层后缀；
    #   ② 仍查不到的记警告，交给下游按"AI 假设"处理。
    known_ids = {aid for acts in KB_STRUCT_L4.values() for aid, _ in acts}
    for phase in wbs["phases"]:
        for wp in phase.get("work_packages", []):
            for leaf in wp.get("sub_packages", []):
                kid = leaf.get("kb_activity_id")
                if not kid:
                    continue
                kid = str(kid).strip()
                # ① 剥单位后缀：仅在剥掉后确实能在知识库里查到对应活动时才剥
                if "/" in kid:
                    head = kid.rsplit("/", 1)[0].strip()
                    if head and (head in known_ids or kb.activity_info(head)):
                        warnings.append(f"kb_activity_id 含单位后缀，已纠正：{kid} → {head}")
                        kid = head
                leaf["kb_activity_id"] = kid
                # ② 软校验：以知识库字典表为准（KB_STRUCT_L4 只是常用子集，会误报）
                if kid not in known_ids and not kb.activity_info(kid):
                    warnings.append(f"未知 kb_activity_id：{kid}（{leaf.get('name', '')}）")
    return wbs, warnings


def _pick_template(prompt, params):
    text = (prompt or "") + json.dumps(params or {}, ensure_ascii=False)
    if any(k in text for k in ("厂房", "车间", "仓库", "厂房项目")):
        return "workshop"
    if any(k in text for k in ("住宅", "商品房", "楼盘", "保障房")):
        return "residential"
    return "default"


def load_template(kind):
    path = config.SAMPLE_DIR / "wbs_templates" / f"{kind}.json"
    if not path.exists():
        path = config.SAMPLE_DIR / "wbs_templates" / "default.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------- 节点 ----------------
class WBSGenNode(BaseNode):
    name = "wbs"
    title = "WBS 工作分解"
    pause_point = True  # 默认暂停点，用户可 /retry /edit

    def __init__(self, llm=None):
        super().__init__()
        self.llm = llm or LLMClient()

    def run(self, ctx):
        prompt = ctx.get("prompt", "")
        params = ctx.get("extracted_params") or {}
        extra = ctx.get("_extra_instruction") if ctx.get("_extra_for") == self.name else None
        self.emit("node_progress", {"node": self.name, "progress": 30,
                                    "message": "调用 LLM 生成三层 WBS"})

        # KB 建筑类型 → 结构 L4 词汇注入
        injection = None
        kb_note = None
        building_type = (params or {}).get("building_type")
        if building_type:
            bt_info = kb.resolve_building_type(str(building_type))
            if bt_info:
                injection = build_kb_injection(bt_info[0])
                if injection:
                    kb_note = bt_info[1]
                    ctx["kb_building_type"] = bt_info[0]

        wbs = None
        used = "llm"
        try:
            user = f"项目描述：\n{prompt}\n\n已抽取参数：\n{json.dumps(params, ensure_ascii=False)}"
            if injection:
                user += injection
            if extra:
                user += f"\n\n用户调整指令（请执行）：{extra}"
            raw = self.llm.chat_json(load("wbs_gen.txt"), user, temperature=0.3)
            candidate, warns = normalize_wbs(raw)
            if candidate is None:
                raise LLMError(f"WBS 校验失败：{warns}")
            wbs = candidate
            if warns:
                ctx.setdefault("wbs_warnings", []).extend(warns)
        except Exception:
            wbs = None
            used = "template"

        if wbs is None:
            self.emit("node_progress", {"node": self.name, "progress": 70,
                                        "message": "LLM 不可用/输出不合规，加载项目类型模板"})
            wbs, _ = normalize_wbs(load_template(_pick_template(prompt, params)))
            if wbs is None:
                raise RuntimeError("模板兜底也失败")
            ctx["wbs_source"] = "template"
        else:
            ctx["wbs_source"] = "llm"

        self.emit("node_progress", {"node": self.name, "progress": 100, "message": "WBS 校验通过"})
        n_phase = len(wbs["phases"])
        n_wp = sum(len(p.get("work_packages", [])) for p in wbs["phases"])
        n_leaf = sum(len(wp.get("sub_packages", [])) for p in wbs["phases"]
                     for wp in p.get("work_packages", []))
        kb_tag = f"（KB L4 约束：{kb_note}）" if kb_note else "（未命中 KB，LLM 生成）"
        self.done_summary = f"WBS树已生成（{used}）：{n_phase}阶段/{n_wp}工作包/{n_leaf}叶子任务{kb_tag}"
        return {"wbs": wbs}
