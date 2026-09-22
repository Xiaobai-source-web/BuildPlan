# -*- coding: utf-8 -*-
"""B3 构件占比表数值编制 + 落库（幂等 · 默认 dry-run）。

口径（**用户 2026-09-21 裁定**）
-------------------------------
口径（**用户 2026-09-21 裁定 · 路线 2**）
---------------------------------------
占比表**只回答「部位/构件」**，**不回答「材料/做法/体系」与「工序」**。

1. **组层**：一个 `(结构类型 × 工种(L3))` 组只有"部位可切分"时才编。判据：
   * P1 组内每个 L4 的主维是**部位/构件**（梁/柱/板/墙/基础/楼梯/屋架…）；
   * P2 成员**互不排斥**，同一栋楼可同时存在；
   * P3 成员之间是"分"不是"串联"（不是同一主体的多道工序）；
   * P4 组内不存在占据主导的"材料/做法择一"分叉。
   不满足的组**整组不编**（连占比行都不建），逐组在送审表里给出理由 +
   该组的量"本应从哪里来"。
2. **条目层**：可切分组内 L4 分两类：
   * **切分类**（部位/构件）→ 拿占比，组内 ∑ = 100；
   * **不参与类** → **不给占比行**，分四类（见 `NON_PARTICIPATING`）：
     ① 互斥做法（材料/做法族）——**降为 OPTIONAL**，每族**代表项**留在切分类；
     ② 工序/配套；③ 附属/室外工程；④ 按需项。②③④ **不动档位**（确实要做），
     靠**豁免集合**（`exempt_activity_ids`）表达"明确不参与"。
3. 校验：`component_ratio.check_ratio_v1_v4(..., exempt_activity_ids=…)`。
   V1 只校验"有占比行的组"；V3 对 REQUIRED 要求 > 0，但**豁免条目不算缺失**；
   新增 **V5 守卫**：豁免条目**不允许再拿占比**（占比只落在切分类上）。

编数方法（AI 经验估算 + 逐行标注）
---------------------------------
1. 只对**切分类**条目给值；EXCLUDED 一律不给（V2），不参与类一律不给（V5）。
2. 组内按「构件角色权重表」给相对权重，再 **组内归一化到 ∑ = 100%**
   （分配器：每项先占 0.1% 保底 → 其余按权重最大余数法补足；保证每项 > 0 且
   合计精确 100.0）。REQUIRED 切分类项因此必然 > 0（V3）。
3. 每行落库：`source_code='ai_estimate_v1'`、`confidence='LOW'`、
   `review_state='pending'`、`notes` = 该行为什么是这个数（一句话依据）。
4. `--apply` 同时做两件写库动作（都先备份 kb.db）：
   (a) `Component_Ratio` 占比行 upsert（幂等）；
   (b) `Structure_Type_L4_Mapping` 的**互斥做法备选降档**（REQUIRED → OPTIONAL，
       幂等），降档原因写进该行 `notes` 前缀。

用法
----
    python devtools/kb_seed_component_ratio.py --db BuildPlan_KB/kb.db            # dry-run（默认）
    python devtools/kb_seed_component_ratio.py --db BuildPlan_KB/kb.db --apply    # 先备份再写库
    python devtools/kb_seed_component_ratio.py --db BuildPlan_KB/kb.db --verify   # 只读校验
    python devtools/kb_seed_component_ratio.py --db <库> --emit-md <路径>          # 只读导出分组表
    python devtools/kb_seed_component_ratio.py --db <库> --emit-exempt <路径>      # 只读导出豁免清单
"""

from __future__ import annotations

import argparse
import datetime
import math
import os
import sqlite3
import sys

SOURCE_CODE = "ai_estimate_v1"
CONFIDENCE = "LOW"
REVIEW_STATE = "pending"

#: 最小分配单位（百分点）：每项至少 1 个单位 → 必然 > 0，满足 V3
UNIT = 0.1

STRUCTURE_CN = {
    "frame": "框架", "frame_shear": "框剪", "shear_wall": "剪力墙",
    "tube": "筒体", "steel": "钢结构", "bent": "排架", "masonry_conc": "砖混",
}
WORK_TYPE_CN = {
    "concrete": "混凝土", "formwork": "模板", "rebar": "钢筋",
    "masonry": "砌体", "pile_foundation": "桩基", "steel_structure": "钢结构",
}

# ======================================================================
# 路线 2 分类表（组层：部位可切分？；条目层：切分 / 不参与）
# ======================================================================

#: 不参与类（**不给占比**）。key = L4；值 = (类别, 一句话理由, 归谁管)。
#: 类别取值：``互斥做法``（材料/做法族备选，**降档为 OPTIONAL**，代表项留在切分类）、
#: ``工序``（量派生自主体的工序/配套条目）、``附属``（按需出现的附属/室外工程）、
#: ``按需``（OPTIONAL：出现与否由项目条件决定）。
NON_PARTICIPATING = {
    # --- ① 互斥做法族：同一「部位」的不同材料/做法，条件维择一（降档为 OPTIONAL）---
    "LDT724_石墙_单面清水": (
        "互斥做法", "石墙与砌块墙/砖墙是同一「砌体墙」部位的材料择一（代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_石墙_双面混水": (
        "互斥做法", "同上，石墙为地方性做法（代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_砖墙_单面清水": (
        "互斥做法", "砖墙与砌块墙是同一「砌体墙」部位的材料择一（代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_砖墙_双面清水": (
        "互斥做法", "同上（代表项 = 砌块墙）", "由条件维「构件做法/材料类型」择一"),
    "LDT724_砖墙_混水内": (
        "互斥做法", "内墙砖砌与砌块墙择一（代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_砖墙_混水外": (
        "互斥做法", "外墙砖砌与砌块墙择一（代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_空斗墙": (
        "互斥做法", "空斗砌法是节材做法，与砌块墙择一（代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_空花墙": (
        "互斥做法", "空花墙是装饰性漏空砌体，与砌块墙择一（代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_飘砖墙": (
        "互斥做法", "飘砖墙是挑砖线脚做法，与砌块墙择一（代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_毛料石基础": (
        "互斥做法", "砌体基础的石材做法与「独立基础」择一（代表项 = AD0006 独立基础）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_毛石独立基础": (
        "互斥做法", "同上（代表项 = AD0006 独立基础）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_毛石墙基": (
        "互斥做法", "条形墙基是基础的另一种形式，与「独立基础」择一（代表项 = AD0006）",
        "由条件维「基础形式」择一"),
    "LDT724_毛料石石墙": (
        "互斥做法", "石墙备选（本就是 OPTIONAL；代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_清料石石墙": (
        "互斥做法", "石墙备选（本就是 OPTIONAL；代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_毛石墙镶砌砖": (
        "互斥做法", "石墙镶砖做法备选（本就是 OPTIONAL）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_多孔砖墙": (
        "互斥做法", "多孔砖墙备选（本就是 OPTIONAL；代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "LDT724_空心砖墙": (
        "互斥做法", "空心砖墙备选（本就是 OPTIONAL；代表项 = 砌块墙）",
        "由条件维「构件做法/材料类型」择一"),
    "MASON_ALC_PANEL": (
        "互斥做法", "ALC 墙板是条板体系，与砌块墙择一（本身 OPTIONAL）",
        "由条件维「构件做法/材料类型」择一"),
    "MASON0001": (
        "互斥做法", "砖基础与「独立基础」择一（本身 OPTIONAL；代表项 = AD0006）",
        "由条件维「构件做法/材料类型」择一"),
    # --- ② 工序 / 配套（**不动档位**，确实要做，只是不占占比）---
    "REBAR_NEW_JOINT": (
        "工序", "钢筋接头与特殊项（绑扎/机械连接/焊接）不是「部位」，量随部位钢筋派生",
        "由各部位的钢筋子目派生，不另占占比"),
    "FORM_ALU_INSTALL": (
        "工序", "铝合金模板安装是一道工序；且「铝模 vs 木模」是材料体系择一",
        "由条件维「模板体系」择一后派生"),
    "FORM_ALU_STRIP": (
        "工序", "铝合金模板拆除与安装是同一块面积上的两道工序（量重复计量）",
        "由条件维「模板体系」择一后派生"),
    "LDT724_混凝土花饰块组砌": (
        "工序", "花饰块组砌是砌筑/装饰工序，不是部位",
        "由条件维「是否有花饰块装饰」决定"),
    "LDT724_石墙勾缝": ("工序", "勾缝是砌体表面工序，量随砌体墙面派生", "随之派生的工艺子目"),
    "LDT724_砖墙勾缝": ("工序", "同上", "随之派生的工艺子目"),
    "LDT724_空斗墙勾缝": ("工序", "同上", "随之派生的工艺子目"),
    "LDT724_砌块墙勾缝": ("工序", "同上（本身 OPTIONAL）", "随之派生的工艺子目"),
    "LDT724_砖柱勾缝": ("工序", "同上（本身 OPTIONAL）", "随之派生的工艺子目"),
    "LDT724_烟囱铁箍安装": ("工序", "铁箍是烟囱的配套件安装工序", "随之派生的工艺子目"),
    "LDT724_检查井盖座安装": ("工序", "井盖座安装是配套安装工序", "随之派生的工艺子目"),
    "LDT724_混凝土沟盖板安装": ("工序", "沟盖板安装是配套安装工序", "随之派生的工艺子目"),
    "LDT724_混凝土门窗过梁安装": ("工序", "过梁安装是配套安装工序", "随之派生的工艺子目"),
    "LDT724_窗台板安装": ("工序", "窗台板是配套安装工序", "随之派生的工艺子目"),
    "LDT724_阳台栏板安装": ("工序", "栏板安装是配套安装工序（用户已列为例）", "随之派生的工艺子目"),
    "LDT724_砖砌地胎膜": ("工序", "地胎膜是垫层/胎模工序（用户已列为例）", "随之派生的临时工程子目"),
    # --- ③ 附属 / 室外工程（按项目是否含该附属工程出现；**不动档位**）---
    "AD0007": ("附属", "砌挖孔桩护壁只在采用人工挖孔桩时出现", "由条件维「桩型」决定"),
    "LDT724_护坡": ("附属", "护坡是室外附属工程", "由条件维「是否含室外护坡」决定"),
    "LDT724_排水沟": ("附属", "排水沟是室外附属工程", "由条件维「是否含室外排水」决定"),
    "LDT724_砖砌明沟": ("附属", "砖砌明沟是室外附属工程（本身 OPTIONAL）",
                    "由条件维「是否含室外排水」决定"),
    "LDT724_砌路牙": ("附属", "路牙是室外附属工程（本身 OPTIONAL）",
                  "由条件维「是否含室外道路」决定"),
    "LDT724_沟道壁": ("附属", "沟道壁是室外/地沟附属砌体", "由条件维「是否含地沟」决定"),
    "LDT724_毛料石勒脚": ("附属", "勒脚是墙脚装饰/防护做法，不是独立部位",
                    "由条件维「是否有勒脚做法」决定"),
    "LDT724_清料石勒脚": ("附属", "同上（勒脚做法备选）", "由条件维「是否有勒脚做法」决定"),
    "LDT724_毛料石挡土墙": ("附属", "挡土墙是室外附属构筑物",
                     "由条件维「是否含挡土墙」决定"),
    "LDT724_毛石挡土墙": ("附属", "同上（挡土墙材料备选）", "由条件维「是否含挡土墙」决定"),
    "LDT724_水塔": ("附属", "水塔是特殊构筑物，仅个别项目出现",
                "由条件维「是否含水塔」决定"),
    "LDT724_渗井检查井化粪池阀井": (
        "附属", "检查井/化粪池等是室外附属构筑物",
        "由条件维「是否含室外管网构筑物」决定"),
    "LDT724_烟囱内衬": ("附属", "烟囱内衬属特殊构筑物", "由条件维「是否含烟囱」决定"),
    "LDT724_烟囱基础": ("附属", "同上", "由条件维「是否含烟囱」决定"),
    "LDT724_烟囱筒身": ("附属", "同上", "由条件维「是否含烟囱」决定"),
    # --- ④ 按需项（OPTIONAL 的部位型条目，是否出现由方案决定）---
    "LDT724_零星砌体": ("按需", "零星砌体是「其他」部位，是否出现取决于项目",
                   "由条件维「是否有零星砌体」决定"),
    "LDT724_方柱_混水": ("按需", "砌体柱（部位 = 柱）是否设置取决于结构方案",
                    "由条件维「是否设砌体柱」决定"),
    "LDT724_方柱_清水": ("按需", "同上", "由条件维「是否设砌体柱」决定"),
    "LDT724_石柱_圆形": ("按需", "同上（石柱）", "由条件维「是否设砌体柱」决定"),
    "LDT724_石柱_方形": ("按需", "同上（石柱）", "由条件维「是否设砌体柱」决定"),
    "LDT724_圆多边形柱": ("按需", "同上（异形柱）", "由条件维「是否设砌体柱」决定"),
}

#: 互斥做法族 → **降档为 OPTIONAL** 的 L4（代表项不在此列，保持 REQUIRED）
DOWNGRADE_AIDS = {aid for aid, v in NON_PARTICIPATING.items() if v[0] == "互斥做法"}

#: 部位不可切分组（**整组不编**：连占比行都不建）。
#: key = (结构类型, 工种)；值 = (判据结论, 该组的量本应从哪里来)。
#: 与之并列的还有 `UNSPLITTABLE_WORK_TYPES`（按工种整类判性，覆盖该工种的全部结构类型）。
UNSPLITTABLE_GROUPS = {
    ("steel", "steel_structure"): (
        "组内 64 个 REQUIRED 的主维是「钢结构体系 × 构件型号」而非部位：同一部位"
        "（如柱）有实腹柱/空腹柱/钢管柱/H柱/刚架柱/格构柱/十字柱等 7 种互斥型号，"
        "代表项的选取完全取决于体系（门式刚架/厂房/网架/塔架）—— 组内「材料/做法"
        "择一」分叉占据主导（P4 不满足），无唯一可辩护的代表项。",
        "由条件维「钢结构体系/子类型」择一后，按其体系的定额子目组合派生"),
    ("bent", "masonry"): (
        "55 行里 1 行 EXCLUDED + 54 行 OPTIONAL，**0 个 REQUIRED**：排架结构是否"
        "需要砌体填充墙取决于使用功能；且墙材（砖/砌块/石/空斗/空心/ALC）互斥，"
        "在没有任何 REQUIRED 锚点的情况下没有唯一可辩护的代表组合。",
        "由条件维「是否有砌体填充墙 + 墙材」择一后派生"),
    ("steel", "masonry"): (
        "55 行里 18 行 EXCLUDED + 37 行 OPTIONAL，**0 个 REQUIRED**：钢结构厂房"
        "墙面以压型钢板/夹芯板为主（已在 steel_structure 组计量），砌体填充墙非"
        "必然且墙材互斥。",
        "由条件维「围护做法」择一后派生"),
}

#: 按**工种**整类判为不可切分（该工种在**全部结构类型**下都不编占比行）。
#: key = 工种；值 = (判据结论, 该组的量本应从哪里来)。
UNSPLITTABLE_WORK_TYPES = {
    "pile_foundation": (
        "**P3 不成立（是「串联」不是「分」）**：组内 7 个 REQUIRED 是**同一根桩上的"
        "串联工序** —— 成孔（桩身）→ 钢护筒埋设 / 泥浆运输（成孔配套）→ 钻孔入岩"
        "（桩端）→ 后压浆（桩端增强）→ 截（凿）桩头（桩顶）→ 检测管制安（桩身内），"
        "按施工先后**依次发生**，不是可共存的部位；名字含工序动词不是巧合，是事实。"
        "其余约 31 个 OPTIONAL 是**互斥桩型**（钻孔/旋挖/冲孔/打管桩/压管桩/CFG/沉管…），"
        "本就该由条件维择一 —— 它们不是「占比」问题。"
        "⇒ 该工种整类不编（全部 7 个结构类型），一行占比都不建。",
        "由条件维「桩型 + 桩径/桩长」择一后，按该桩型的定额子目组合派生。"
        "**不能**再拿「桩基总量」按百分比切：那样会把同一根桩的工序重复或漏算。",
    ),
}

#: 砌体「切分类」代表项（部位：墙 / 基础）
MASONRY_WALL_REP = "LDT724_砌块墙"
MASONRY_FOUND_REP = "AD0006"


# ======================================================================
# 权重表（相对权重 + 一句话依据）
# ======================================================================

_CONC = {
    "FOUND": (20.0, "基础埋深大、体量集中，约占混凝土总量的两成"),
    "COLUMN": (22.0, "柱为竖向承重主构件，截面与层高决定用量"),
    "BEAM": (26.0, "梁为水平承重主构件，数量多、跨度大"),
    "SLAB": (26.0, "楼板满铺、面积最大，虽薄但总量可观"),
    "WALL": (16.0, "墙肢为抗侧力构件，在剪力墙/框剪体系中占比高"),
    "STAIR": (4.0, "楼梯为局部构件，量级小"),
    "ROOF": (6.0, "屋架（屋面梁）为屋面承重构件"),
    "OTHER": (3.0, "其他构件为零星项，量级小"),
}
_REBAR = {
    "FOUND": (18.0, "基础配筋率低但体量大"),
    "COLUMN": (22.0, "柱纵筋+箍筋，配筋率高"),
    "BEAM": (26.0, "梁纵筋+箍筋，配筋率高、数量多"),
    "SLAB": (24.0, "板配筋率低但满铺"),
    "WALL": (16.0, "墙分布筋+边缘构件钢筋，配筋率高"),
    "STAIR": (5.0, "楼梯钢筋为局部项"),
    "ROOF": (6.0, "屋架钢筋为局部项"),
    "OTHER": (4.0, "楼梯及其他钢筋为零星项"),
    "JOINT": (6.0, "钢筋接头与特殊项按接头率与机械连接比例摊"),
}
_FORMWORK = {
    "FOUND": (14.0, "基础模板以垫层/基础侧模为主"),
    "COLUMN": (16.0, "柱模板按周长×层高展开，接触面积大"),
    "BEAM": (20.0, "梁模板按三面展开，接触面积大"),
    "ALU_INSTALL": (22.0, "铝模体系安装，对应体系模板面积的一半工序"),
    "ALU_STRIP": (20.0, "铝模体系拆除与安装对应同一面积，取对称量级"),
    "OTHER": (8.0, "其他模板为零星项"),
}

_CONC_BY_AID = {
    "CONC_NEW_FOUND": "FOUND", "CONC_NEW_COLUMN": "COLUMN",
    "CONC_NEW_BEAM": "BEAM", "CONC_NEW_SLAB": "SLAB",
    "CONC_NEW_WALL": "WALL", "CONC_NEW_STAIR": "STAIR",
    "CONC_NEW_ROOF": "ROOF", "CONC_NEW_OTHER": "OTHER",
}
_REBAR_BY_AID = {
    "REBAR_NEW_FOUND": "FOUND", "REBAR_NEW_COL": "COLUMN",
    "REBAR_NEW_BEAM": "BEAM", "REBAR_NEW_SLAB": "SLAB",
    "REBAR_NEW_WALL": "WALL", "REBAR_NEW_OTHER": "OTHER",
    "REBAR_NEW_ROOF": "ROOF", "REBAR_NEW_JOINT": "JOINT",
}
_FORM_BY_AID = {
    "FORM_NEW_FOUND": "FOUND", "FORM_NEW_COL": "COLUMN",
    "FORM_NEW_BEAM": "BEAM", "FORM_NEW_OTHER": "OTHER",
    "FORM_ALU_INSTALL": "ALU_INSTALL", "FORM_ALU_STRIP": "ALU_STRIP",
}

#: 砌体「切分类」代表项权重（路线 2：砌体的部位只有「墙」与「基础」两类）
_MASONRY = {
    MASONRY_WALL_REP: (24.0, "砌体量按墙部位计量，代表项 = 砌块墙（框架类填充墙主流做法）"),
}
#: 砖混结构砌体组的第二个部位：砌体基础
_MASONRY_CONC_EXTRA = {
    MASONRY_FOUND_REP: (8.0, "砖混结构的砌体基础（独立基础）是基础部位的代表项"),
}

#: 桩基：以钻孔灌注桩为代表桩型（各结构同一套权重）
#: ⚠️ 已废弃不用 —— 2026-09-21 父代理裁定 `pile_foundation` 整类**不可切分**
#: （见 `UNSPLITTABLE_WORK_TYPES`），该工种不再产任何占比行。本表与
#: `_OPTIONAL_PILE_REPS` / `_weight()` 里的 pile 分支**仅为追溯保留**，不再被编制路径调用。
_PILE = {
    "GD_A13_钻孔成孔": (60.0, "暂以钻孔灌注桩为代表桩型（最常见），成孔为主干工程量"),
    "GD_A13_泥浆运输": (12.0, "钻孔桩泥浆外运，按成孔量的一定比例摊"),
    "GD_A13_钢护筒": (8.0, "钢护筒埋设摊销，量小"),
    "GD_A13_钻孔入岩": (8.0, "入岩增加费按部分桩入岩计"),
    "GD_A13_截凿桩头": (5.0, "截（凿）桩头按桩数计，仅为成孔工程量的少量"),
    "GD_A13_后压浆": (5.0, "桩底（侧）后压浆为增强工艺，量小"),
    "GD_A13_检测管制安": (2.0, "声测管按桩长摊销，量小"),
}

#: 钢结构（steel × steel_structure，64 个 REQUIRED）
#: 路线 2 判定为**整组不可切分**（见 `UNSPLITTABLE_GROUPS`），因此不再编权重。

#: 允许给值的 OPTIONAL（规则 (a)/(b) 的落地清单）；不在表内的 OPTIONAL 一律不给
_OPTIONAL_PILE_REPS = {
    "GD_A13_钻孔成孔", "GD_A13_泥浆运输", "GD_A13_钢护筒",
    "GD_A13_钻孔入岩", "GD_A13_后压浆", "GD_A13_检测管制安",
}
OPTIONAL_GIVE = {
    ("masonry_conc", "concrete"): {"CONC_NEW_BEAM", "CONC_NEW_COLUMN"},
    ("masonry_conc", "rebar"): {"REBAR_NEW_BEAM", "REBAR_NEW_COL"},
    ("masonry_conc", "formwork"): {"FORM_NEW_BEAM", "FORM_NEW_COL"},
    ("shear_wall", "concrete"): {"CONC_NEW_BEAM"},
    ("shear_wall", "rebar"): {"REBAR_NEW_BEAM"},
    ("shear_wall", "formwork"): {"FORM_NEW_BEAM"},
}
for _sid in STRUCTURE_CN:
    OPTIONAL_GIVE[(_sid, "pile_foundation")] = set(_OPTIONAL_PILE_REPS)

#: 规则 (a)/(b) 的注释（写进 notes）
_OPTIONAL_REASON = {
    "CONC_NEW_BEAM": "砖混/剪力墙结构中圈梁、连梁物理上必然出现",
    "CONC_NEW_COLUMN": "砖混结构中构造柱物理上必然出现",
    "FORM_NEW_BEAM": "与同组梁混凝土配套（B5 两表不打架）",
    "FORM_NEW_COL": "与同组柱混凝土配套（B5 两表不打架）",
    "REBAR_NEW_BEAM": "梁的配筋必然存在（与混凝土梁对应）",
    "REBAR_NEW_COL": "柱的配筋必然存在（与构造柱对应）",
}
_PILE_OPT_REASON = (
    "同组 REQUIRED 只有「截（凿）桩头」，物理上必然还有成孔主干项；"
    "各类桩型互斥，此处取钻孔灌注桩为代表项"
)

#: 明确「整组不给值」的组（0 个 REQUIRED，全部 OPTIONAL，且备选项互斥、无可辩护分配）
UNALLOCATED_GROUPS = {
    ("bent", "masonry"):
        "1 行 EXCLUDED + 54 行 OPTIONAL：排架结构是否需要砌体填充墙取决于使用功能，"
        "且墙材（砖/砌块/石/空斗/空心）互斥，无可辩护的代表组合",
    ("steel", "masonry"):
        "18 行 EXCLUDED + 37 行 OPTIONAL：钢结构厂房墙面以压型钢板/夹芯板为主"
        "（已在 steel_structure 组计量），砌体填充墙非必然且墙材互斥，无可辩护分配",
}


# ======================================================================
# 分权与分配
# ======================================================================


def _weight(sid: str, wt: str, aid: str) -> "tuple[float, str]":
    """返回 (相对权重, 一句话依据)。"""
    if wt == "concrete":
        key = _CONC_BY_AID.get(aid)
        if key:
            return _CONC[key]
    elif wt == "rebar":
        key = _REBAR_BY_AID.get(aid)
        if key:
            return _REBAR[key]
    elif wt == "formwork":
        key = _FORM_BY_AID.get(aid)
        if key:
            return _FORMWORK[key]
    elif wt == "masonry":
        if aid in _MASONRY:
            return _MASONRY[aid]
        if aid in _MASONRY_CONC_EXTRA:
            return _MASONRY_CONC_EXTRA[aid]
    elif wt == "pile_foundation":
        if aid in _PILE:
            return _PILE[aid]
    return (1.0, "该行无专门角色权重，按同级构件取下限权重（待人工审核）")


def allocate(weights, unit: float = UNIT):
    """组内归一化到 ∑ = 100.0（每组每项 ≥ 1 个 `unit`，最大余数法补足）。

    >>> s = allocate([1.0, 1.0, 1.0]); round(sum(s), 6)
    100.0
    >>> all(v > 0 for v in s)
    True
    """
    total_units = int(round(100.0 / unit))
    weights = [max(float(w), 0.0) for w in weights]
    if not weights:
        return []
    if len(weights) > total_units:
        raise ValueError("组内项数 %d 超过最小单位总数 %d" % (len(weights), total_units))
    base = 1
    rest = total_units - base * len(weights)
    s = sum(weights) or float(len(weights))
    extra = [rest * (w / s) for w in weights]
    floors = [int(math.floor(x)) for x in extra]
    units = [base + f for f in floors]
    left = total_units - sum(units)
    order = sorted(range(len(weights)),
                   key=lambda i: (-(extra[i] - floors[i]), i))
    for i in order[:left]:
        units[i] += 1
    out = [round(u * unit, 1) for u in units]
    # 浮点收尾：把差额补给权重最大的项，保证合计精确 100.0
    diff = round(100.0 - sum(out), 6)
    if abs(diff) >= 1e-9:
        top = max(range(len(out)), key=lambda i: (weights[i], -i))
        out[top] = round(out[top] + diff, 1)
    return out


# ======================================================================
# 读库 → 编制计划
# ======================================================================


def load(con: sqlite3.Connection):
    l4 = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT activity_id, work_type_id, activity_name "
        "FROM L4_Activity_Dictionary")}
    mapping = con.execute(
        "SELECT structure_type_id, activity_id, applicability_level "
        "FROM Structure_Type_L4_Mapping ORDER BY structure_type_id, activity_id"
    ).fetchall()
    return l4, mapping


def build_plan(con: sqlite3.Connection):
    l4, mapping = load(con)
    groups = {}
    for sid, aid, level in mapping:
        wt = (l4.get(aid) or (None, None))[0]
        if not wt:
            continue
        groups.setdefault((sid, wt), []).append((aid, level))

    plan, skipped, nonpart, unknown_l4 = [], [], [], []
    for (sid, wt) in sorted(groups):
        items = groups[(sid, wt)]
        unsplit = UNSPLITTABLE_GROUPS.get((sid, wt)) \
            or UNSPLITTABLE_WORK_TYPES.get(wt)
        if unsplit:
            verdict, source = unsplit
            skipped.append({
                "structure_type_id": sid, "work_type_id": wt,
                "candidates": len(items), "verdict": verdict, "source": source,
                "pairs": [(sid, aid) for aid, level in sorted(items)
                          if level != "EXCLUDED"],
            })
            continue
        give = OPTIONAL_GIVE.get((sid, wt), set())
        chosen, rest = [], []
        for aid, level in sorted(items):
            if aid not in l4:
                unknown_l4.append((sid, aid))
                continue
            if level == "EXCLUDED":
                continue
            participates = (aid not in NON_PARTICIPATING
                            and (level == "REQUIRED" or aid in give))
            if participates:
                chosen.append((aid, level))
            else:
                rest.append((aid, level))
        if not chosen:
            counts = {}
            for _aid, level in items:
                counts[level] = counts.get(level, 0) + 1
            verdict = ("该结构类型下 %d 个候选在映射表中全部为 EXCLUDED"
                       "（无 REQUIRED/OPTIONAL），不参与占比表" % len(items))
            skipped.append({
                "structure_type_id": sid, "work_type_id": wt,
                "candidates": len(items), "verdict": verdict,
                "source": "映射表已把它排除在该结构类型之外（无候选）",
                "pairs": [],
            })
            continue
        # 不参与清单（送审表逐条列出：类别 + 理由 + 归谁管）
        for aid, level in rest:
            kind, why, owner = NON_PARTICIPATING.get(
                aid, ("按需", "OPTIONAL 且未命中给予规则：是否出现取决于项目条件",
                      "由条件维/项目特征决定"))
            nonpart.append({
                "structure_type_id": sid, "work_type_id": wt,
                "activity_id": aid, "activity_name": (l4.get(aid) or (None, aid))[1],
                "applicability_level": level, "kind": kind,
                "why": why, "owner": owner,
            })
        weights, reasons = [], []
        for aid, level in chosen:
            w, why = _weight(sid, wt, aid)
            weights.append(w)
            reasons.append(why)
        pcts = allocate(weights)
        group_key = "%s|%s" % (sid, wt)
        split_ids = [aid for (aid, _lv) in chosen]
        for idx, (aid, level) in enumerate(chosen):
            name = (l4.get(aid) or (None, aid))[1]
            note = ("AI 经验估算 V1（路线 2 · 占比只回答「部位」）：%s结构 %s总量中，"
                    "%s 是「切分类」部位，按「%s」取权重 %.1f；组内归一后占比 %.1f%%"
                    "（组 %s 内 ∑=100%%；其余同组条目为「不参与」（工序/做法/附属），"
                    "不给占比。confidence=LOW、review_state=pending，待人工审核）"
                    % (STRUCTURE_CN.get(sid, sid), WORK_TYPE_CN.get(wt, wt),
                       name, reasons[idx], weights[idx], pcts[idx], group_key))
            note += "；本组切分类共 %d 项：%s" % (len(split_ids), "、".join(split_ids))
            if level == "OPTIONAL":
                extra = _PILE_OPT_REASON if wt == "pile_foundation" \
                    else _OPTIONAL_REASON.get(aid, "物理上必然出现的主干项")
                note += "；OPTIONAL 判定：%s" % extra
            plan.append({
                "structure_type_id": sid, "activity_id": aid,
                "work_type_id": wt, "activity_name": name,
                "applicability_level": level, "weight": weights[idx],
                "ratio_percent": pcts[idx], "notes": note,
            })
    return plan, skipped, nonpart, unknown_l4


def exempt_ids(nonpart, skipped=None) -> "list":
    """把「不参与」清单转成校验器入参（``(结构类型, L4)`` 二元组）。

    除已编组里的「不参与」条目外，**部位不可切分组的全部非 EXCLUDED 条目**也必须
    进豁免集合 —— 否则它们的 REQUIRED 档位会被 V3 逼着要占比（整组不编正是
    "明确不参与"，必须与"表里没有"区分开）。
    """
    out = {(r["structure_type_id"], r["activity_id"]) for r in (nonpart or [])}
    for s in skipped or []:
        out |= set(s.get("pairs") or [])
    return sorted(out)


def rule_family_of(aid: str) -> str:
    """互斥做法族的族名（用于送审表按族归并）。"""
    if aid in ("LDT724_毛料石基础", "LDT724_毛石独立基础", "LDT724_毛石墙基",
               "MASON0001"):
        return "砌体基础（代表 AD0006 独立基础）"
    if aid in ("LDT724_毛料石石墙", "LDT724_清料石石墙", "LDT724_毛石墙镶砌砖",
               "LDT724_多孔砖墙", "LDT724_空心砖墙", "MASON_ALC_PANEL",
               "LDT724_空斗墙", "LDT724_空花墙", "LDT724_飘砖墙"):
        return "砌体墙（代表 LDT724_砌块墙）"
    return "砌体墙（代表 LDT724_砌块墙）"


# ======================================================================
# 落库 / 校验 / 导出
# ======================================================================


def write(con: sqlite3.Connection, plan) -> "tuple[int, int]":
    now = datetime.datetime.now().isoformat(timespec="seconds")
    keys = {(p["structure_type_id"], p["activity_id"]) for p in plan}
    staled = 0
    if plan:
        owned = [r[0] for r in con.execute(
            "SELECT rowid FROM Component_Ratio WHERE source_code=?", (SOURCE_CODE,))]
        for rowid in owned:
            row = con.execute(
                "SELECT structure_type_id, activity_id FROM Component_Ratio WHERE rowid=?",
                (rowid,)).fetchone()
            if (row[0], row[1]) not in keys:
                con.execute("DELETE FROM Component_Ratio WHERE rowid=?", (rowid,))
                staled += 1
    n = 0
    for p in plan:
        con.execute(
            """INSERT INTO Component_Ratio
                 (structure_type_id, activity_id, ratio_percent, source_code,
                  confidence, review_state, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(structure_type_id, activity_id) DO UPDATE SET
                 ratio_percent=excluded.ratio_percent,
                 source_code=excluded.source_code,
                 confidence=excluded.confidence,
                 review_state=excluded.review_state,
                 notes=excluded.notes,
                 updated_at=excluded.updated_at""",
            (p["structure_type_id"], p["activity_id"], p["ratio_percent"],
             SOURCE_CODE, CONFIDENCE, REVIEW_STATE, p["notes"], now, now))
        n += 1
    con.commit()
    return n, staled


def verify(con: sqlite3.Connection) -> int:
    total = con.execute("SELECT COUNT(*) FROM Component_Ratio").fetchone()[0]
    print("Component_Ratio 行数：%d" % total)
    if total == 0:
        print("（空表 —— 尚未落库）")
        return 1
    print("按 source_code/confidence/review_state：")
    for row in con.execute(
            "SELECT source_code, confidence, review_state, COUNT(*) "
            "FROM Component_Ratio GROUP BY 1,2,3 ORDER BY 1,2,3"):
        print("   %s | %s | %s -> %d" % tuple(row))

    rows = con.execute("""
        SELECT r.structure_type_id, COALESCE(d.work_type_id,'<NULL>'),
               COUNT(*), SUM(r.ratio_percent), MIN(r.ratio_percent), MAX(r.ratio_percent)
        FROM Component_Ratio r
        LEFT JOIN L4_Activity_Dictionary d ON d.activity_id = r.activity_id
        GROUP BY 1,2 ORDER BY 1,2""").fetchall()
    bad = 0
    print("每组（结构 × 工种）：")
    for sid, wt, n, s, lo, hi in rows:
        ok = abs(float(s) - 100.0) <= 0.01
        bad += 0 if ok else 1
        print("   %-14s %-18s 行=%-3d ∑=%8.4f  区间=[%.1f, %.1f] %s"
              % (sid, wt, n, float(s), float(lo), float(hi),
                 "OK" if ok else "!!! 不守恒"))
    print("已编组组数：%d，非 100 组数：%d" % (len(rows), bad))

    # 路线 2：「不参与」豁免清单（来自本脚本的分类表，与落库无关）
    plan, skipped, nonpart, unknown = build_plan(con)
    exempt = set(exempt_ids(nonpart, skipped))
    print("不可切分组：%d 组；不参与（豁免）条目：%d 条（pair 数）"
          % (len(skipped), len(exempt)))
    exc = con.execute("""
        SELECT COUNT(*) FROM Component_Ratio r
        JOIN Structure_Type_L4_Mapping m
          ON m.structure_type_id = r.structure_type_id AND m.activity_id = r.activity_id
        WHERE m.applicability_level='EXCLUDED' AND r.ratio_percent > 0""").fetchone()[0]
    all_req = con.execute("""
        SELECT m.structure_type_id, m.activity_id
        FROM Structure_Type_L4_Mapping m
        LEFT JOIN Component_Ratio r
          ON r.structure_type_id = m.structure_type_id AND r.activity_id = m.activity_id
        WHERE m.applicability_level='REQUIRED'
          AND (r.ratio_percent IS NULL OR r.ratio_percent <= 0)""").fetchall()
    miss_pairs = [p for p in all_req if (p[0], p[1]) not in exempt]
    excused = len(all_req) - len(miss_pairs)
    v5 = con.execute("""
        SELECT r.structure_type_id, r.activity_id FROM Component_Ratio r
        WHERE r.ratio_percent > 0""").fetchall()
    v5 = [p for p in v5 if (p[0], p[1]) in exempt]
    orph = con.execute("""
        SELECT COUNT(*) FROM Component_Ratio r
        LEFT JOIN Structure_Type_L4_Mapping m
          ON m.structure_type_id = r.structure_type_id AND m.activity_id = r.activity_id
        WHERE m.mapping_id IS NULL""").fetchone()[0]
    print("V2 冲突（EXCLUDED 却有占比）：%d" % exc)
    print("V3 缺口（REQUIRED 却无占比，**已扣除豁免**）：%d（另有 %d 条属「明确不参与」豁免）"
          % (len(miss_pairs), excused))
    print("V5 守卫（豁免条目却有占比）：%d" % len(v5))
    for p in v5[:5]:
        print("   !! %s × %s" % p)
    print("孤儿行（映射表无该结构×L4）：%d" % orph)
    ok = (bad == 0 and exc == 0 and not miss_pairs and not v5 and orph == 0)
    print("结论：%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def run_validator(con: sqlite3.Connection, plan, skipped=None, nonpart=None):
    """用 backend/pipeline/component_ratio.py 的真实校验器自检。

    注入 ``l4_to_l3``（按结构×工种分组）与 ``exempt_activity_ids``（路线 2 的
    「不参与」清单）——后者让 V3 能区分"表里没有"与"明确不参与"，并启用 V5 守卫。

    返回 ``(硬违规列表, 警告列表, stats)``。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    backend = os.path.join(root, "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from pipeline.component_ratio import check_ratio_v1_v4

    l4_to_l3 = {r[0]: r[1] for r in con.execute(
        "SELECT activity_id, work_type_id FROM L4_Activity_Dictionary")}
    ratio_rows = [{"structure_type_id": p["structure_type_id"],
                   "activity_id": p["activity_id"],
                   "ratio_percent": p["ratio_percent"]} for p in plan]
    mapping_rows = [{"structure_type_id": r[0], "activity_id": r[1],
                     "applicability_level": r[2]} for r in con.execute(
        "SELECT structure_type_id, activity_id, applicability_level "
        "FROM Structure_Type_L4_Mapping")]
    landed = [(p["structure_type_id"], p["activity_id"]) for p in plan]
    res = check_ratio_v1_v4(ratio_rows, mapping_rows, landed, l4_to_l3=l4_to_l3,
                            exempt_activity_ids=exempt_ids(nonpart or [], skipped))
    hard = [v for v in res["violations"] if v["severity"] != "warning"]
    warn = [v for v in res["violations"] if v["severity"] == "warning"]
    return hard, warn, res["stats"]


#: 降档行的 notes 前缀（幂等标记：已经带前缀的行不重复追加）
DOWNGRADE_NOTE = (
    "【路线 2 降档】互斥做法备选：占比表只回答「部位」，同一部位的不同材料/做法由"
    "条件维择一（代表项保持 REQUIRED）。本行由 REQUIRED 降为 OPTIONAL。"
)


def downgrade_mapping(con: sqlite3.Connection, apply: bool = False):
    """把 `DOWNGRADE_AIDS`（互斥做法备选）在映射表里 REQUIRED → OPTIONAL（幂等）。

    工序类条目的档位**不动**（它们确实都要做），由豁免集合表达"不参与"。

    返回 ``(待降档行 [(mapping_id, sid, aid, notes), …], 实际写入条数)``。
    """
    rows = con.execute(
        "SELECT mapping_id, structure_type_id, activity_id, notes "
        "FROM Structure_Type_L4_Mapping WHERE applicability_level='REQUIRED' "
        "ORDER BY structure_type_id, activity_id").fetchall()
    todo = [r for r in rows if r[2] in DOWNGRADE_AIDS]
    if not apply or not todo:
        return todo, 0
    for mapping_id, _sid, _aid, notes in todo:
        merged = notes or ""
        if not merged.startswith(DOWNGRADE_NOTE):
            merged = DOWNGRADE_NOTE + " " + merged
        con.execute(
            "UPDATE Structure_Type_L4_Mapping SET applicability_level='OPTIONAL', "
            "notes=? WHERE mapping_id=?", (merged, mapping_id))
    con.commit()
    return todo, len(todo)


def emit_md(con: sqlite3.Connection, plan, skipped, nonpart, path: str) -> None:
    groups = {}
    for p in plan:
        groups.setdefault((p["structure_type_id"], p["work_type_id"]), []).append(p)
    lines = []
    lines.append("### 7.1 组清单（**已编组**：有占比行的组）\n")
    lines.append("| 结构 × 工种 | 切分类 L4 数 | ∑(%) | 区间(%) |")
    lines.append("|---|---|---|---|")
    for (sid, wt) in sorted(groups):
        ps = groups[(sid, wt)]
        vals = [x["ratio_percent"] for x in ps]
        lines.append("| %s × %s | %d | %.1f | [%.1f, %.1f] |"
                     % (sid, wt, len(ps), sum(vals), min(vals), max(vals)))
    lines.append("")
    lines.append("### 7.2 逐组明细（%d 组，共 %d 行）\n" % (len(groups), len(plan)))
    for (sid, wt) in sorted(groups):
        ps = sorted(groups[(sid, wt)], key=lambda x: -x["ratio_percent"])
        s = sum(x["ratio_percent"] for x in ps)
        lines.append("#### %s（%s） × %s（%s） — %d 个切分类 L4，∑ = %.1f%%\n"
                     % (sid, STRUCTURE_CN.get(sid, sid), wt,
                        WORK_TYPE_CN.get(wt, wt), len(ps), s))
        lines.append("| L4 | 名称 | 档位 | 占比(%) | 依据（notes 摘要） |")
        lines.append("|---|---|---|---|---|")
        for p in ps:
            why = p["notes"].split("按「", 1)[1].split("」", 1)[0]
            lines.append("| `%s` | %s | %s | %.1f | %s |"
                         % (p["activity_id"], p["activity_name"],
                            p["applicability_level"], p["ratio_percent"], why))
        lines.append("")
    lines.append("### 7.3 部位不可切分组（**整组不编**：一行占比都不建）\n")
    lines.append("判据：P1 主维是部位/构件 ∧ P2 成员互不排斥 ∧ P3 是「分」不是「串联」 ∧ "
                 "P4 无主导性的材料/做法择一分叉。不满足 → 整组不编；"
                 "不给值 ≠ EXCLUDED，B3 校验不会因此报错（V1 只看「有占比行的组」）。\n")
    lines.append("| 结构 × 工种 | 候选 L4 数 | 判据结论 | 该组的量本应从哪里来 |")
    lines.append("|---|---|---|---|")
    for s in skipped:
        lines.append("| %s × %s | %d | %s | %s |"
                     % (s["structure_type_id"], s["work_type_id"],
                        s["candidates"], s["verdict"], s["source"]))
    lines.append("")
    lines.append("### 7.4 「不参与」条目清单（分类 + 逐条理由 + 归谁管）\n")
    lines.append("这些条目**不给占比行**：它们不是「部位」，而是材料/做法备选、工序/配套、"
                 "或按需出现的附属工程。\n")
    by_kind = {}
    for r in nonpart:
        by_kind.setdefault(r["kind"], []).append(r)
    for kind in ("互斥做法", "工序", "附属", "按需"):
        rs = by_kind.get(kind)
        if not rs:
            continue
        lines.append("#### %s（%d 条）\n" % (kind, len(rs)))
        lines.append("| 结构 | 工种 | L4 | 名称 | 档位 | 理由 | 归谁管 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in sorted(rs, key=lambda x: (x["activity_id"],
                                           x["structure_type_id"])):
            lines.append("| %s | %s | `%s` | %s | %s | %s | %s |"
                         % (r["structure_type_id"], r["work_type_id"],
                            r["activity_id"], r["activity_name"],
                            r["applicability_level"], r["why"], r["owner"]))
        lines.append("")

    # 附录：全量候选 L4（去重）与结构归属
    cand = {}
    allrows = con.execute("""
        SELECT m.activity_id, m.structure_type_id, m.applicability_level,
               d.activity_name, d.work_type_id
        FROM Structure_Type_L4_Mapping m
        LEFT JOIN L4_Activity_Dictionary d ON d.activity_id = m.activity_id
        WHERE m.applicability_level IN ('REQUIRED','OPTIONAL')
        ORDER BY m.activity_id, m.structure_type_id""").fetchall()
    for aid, sid, lv, name, wt in allrows:
        rec = cand.setdefault(aid, {"name": name, "wt": wt, "R": [], "O": []})
        rec["R" if lv == "REQUIRED" else "O"].append(sid)
    lines.append("### 7.5 附：全量候选 L4 清单（去重 %d 个，来自 DB）\n" % len(cand))
    lines.append("> 这是 `Structure_Type_L4_Mapping` 中所有 REQUIRED/OPTIONAL 行的"
                 "去重结果（**以 DB 为准**）。\n")
    lines.append("| L4 | 名称 | 工种 | REQUIRED 于 | OPTIONAL 于 |")
    lines.append("|---|---|---|---|---|")
    for aid in sorted(cand):
        r = cand[aid]
        lines.append("| `%s` | %s | `%s` | %s | %s |"
                     % (aid, r["name"], r["wt"], "、".join(r["R"]) or "—",
                        "、".join(r["O"]) or "—"))
    lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print("分组表已导出 → %s" % path)


#: 最终验收输入（项目样例\示例3_住宅楼_对比版.txt）+ frame_shear 结构
ACCEPT_INPUT = {
    "structure_type_id": "frame_shear",
    "label": "1 栋 / 地上 18 层 / 地下 2 层 / 15000 m² / frame_shear / residential",
    "totals": [
        ("concrete", "混凝土", "total_concrete", 8000.0, "m³"),
        ("rebar", "钢筋", "total_rebar", 1200.0, "t"),
        ("formwork", "模板", "total_formwork", 25000.0, "m²"),
        ("masonry", "砌体", "total_masonry", 3000.0, "m³"),
    ],
}


def emit_exempt(con: sqlite3.Connection, plan, skipped, nonpart, path: str) -> None:
    """导出最终验收输入下**可见结果清单**（各工种 L4 → 占比 → 量）+ 豁免清单。"""
    lines = []
    lines.append("### 7.6 最终验收输入的可见结果清单\n")
    lines.append("输入：`项目样例\\示例3_住宅楼_对比版.txt` —— %s\n"
                 % ACCEPT_INPUT["label"])
    lines.append("总量参数：`total_concrete=8000 m³`、`total_rebar=1200 t`、"
                 "`total_formwork=25000 m²`、`total_masonry=3000 m³`。\n")
    lines.append("**量 = 工种总量 × 该 L4 的占比**"
                 "（占比是「结构 × 工种」分组的百分比）。\n")
    sid = ACCEPT_INPUT["structure_type_id"]
    by_aid = {p["activity_id"]: p for p in plan
              if p["structure_type_id"] == sid}
    for wt, cn, key, total, unit in ACCEPT_INPUT["totals"]:
        ps = sorted([p for p in plan
                     if p["structure_type_id"] == sid and p["work_type_id"] == wt],
                    key=lambda x: -x["ratio_percent"])
        lines.append("#### %s（`%s`，%s = %g %s）— 拿到量的 L4：%d 条\n"
                     % (cn, wt, key, total, unit, len(ps)))
        if not ps:
            lines.append("> 该工种在当前口径下没有占比行。\n")
        else:
            lines.append("| L4 | 名称 | 档位 | 占比（%） | 量（" + unit + "） |")
            lines.append("|---|---|---|---|---|")
            for p in ps:
                lines.append("| `%s` | %s | %s | %.1f | %s |"
                             % (p["activity_id"], p["activity_name"],
                                p["applicability_level"], p["ratio_percent"],
                                ("%g" % (total * p["ratio_percent"] / 100.0))))
            lines.append("")
            lines.append("合计：%d 条，量合计 %s %s（= 总量 × 100%%）。\n"
                         % (len(ps),
                            "%g" % sum(total * p["ratio_percent"] / 100.0
                                       for p in ps), unit))
        nps = [r for r in nonpart
               if r["structure_type_id"] == sid and r["work_type_id"] == wt]
        lines.append("**因为「不参与」而拿 0 / 不出现在上面的清单里**（%d 条）：\n" % len(nps))
        lines.append("| L4 | 名称 | 档位 | 类别 | 归谁管 |")
        lines.append("|---|---|---|---|---|")
        for r in sorted(nps, key=lambda x: x["activity_id"]):
            lines.append("| `%s` | %s | %s | %s | %s |"
                         % (r["activity_id"], r["activity_name"],
                            r["applicability_level"], r["kind"], r["owner"]))
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print("可见结果清单已导出 → %s" % path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="kb.db 路径（必填，防误写真库）")
    ap.add_argument("--apply", action="store_true", help="真正写入（先备份 kb.db）")
    ap.add_argument("--verify", action="store_true", help="只读校验")
    ap.add_argument("--emit-md", default=None, help="只读导出分组表 markdown")
    ap.add_argument("--emit-exempt", default=None,
                    help="只读导出「最终验收输入可见结果 + 豁免清单」markdown")
    a = ap.parse_args()
    if not os.path.isfile(a.db):
        print("找不到库：%s" % a.db)
        return 2
    con = sqlite3.connect(a.db)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        if a.verify:
            return verify(con)
        plan, skipped, nonpart, unknown_l4 = build_plan(con)
        # 注意：豁免集合必须**同时**含不可切分组的非 EXCLUDED 条目（与 verify /
        # run_validator 的口径一致），否则这里打印的 pair 数会少报。
        exempt = exempt_ids(nonpart, skipped)
        if a.emit_md:
            emit_md(con, plan, skipped, nonpart, a.emit_md)
        if a.emit_exempt:
            emit_exempt(con, plan, skipped, nonpart, a.emit_exempt)
        by_group = {}
        for p in plan:
            by_group.setdefault((p["structure_type_id"], p["work_type_id"]), []).append(p)
        print("编制结果：%d 行 / %d 已编组（结构 × 工种）" % (len(plan), len(by_group)))
        for k in sorted(by_group):
            ps = by_group[k]
            print("   %-14s %-18s 行=%-3d ∑=%7.2f  区间=[%.1f, %.1f]"
                  % (k[0], k[1], len(ps), sum(x["ratio_percent"] for x in ps),
                     min(x["ratio_percent"] for x in ps),
                     max(x["ratio_percent"] for x in ps)))
        kinds = {}
        for r in nonpart:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        print("不可切分组（整组不编）：%d 组" % len(skipped))
        for s in skipped:
            print("   %s × %s（候选 %d）：%s"
                  % (s["structure_type_id"], s["work_type_id"], s["candidates"],
                     s["verdict"][:60] + ("…" if len(s["verdict"]) > 60 else "")))
        print("不参与（豁免）条目：%d 条 pair（%s）"
              % (len(exempt), "，".join("%s=%d" % (k, kinds[k])
                                        for k in sorted(kinds))))
        todo, _n = downgrade_mapping(con, apply=False)
        print("待降档（互斥做法备选 REQUIRED → OPTIONAL）：%d 行" % len(todo))
        for _mid, sid, aid, _notes in todo:
            print("   %s × %s" % (sid, aid))
        if unknown_l4:
            print("!! 映射表里不在 L4_Activity_Dictionary 的 L4：%s" % unknown_l4)
        hard, warn, stats = run_validator(con, plan, skipped, nonpart)
        print("自检（component_ratio.check_ratio_v1_v4 + l4_to_l3 + 豁免集合）："
              "已编组=%d 无可占比行的组=%d 豁免条目=%d 豁免入参=%d 单 L4 组=%d"
              % (stats["groups"], stats["groups_without_ratio"],
                 stats["exempt_entries"], stats["exempt_inputs"],
                 stats["single_l4_groups"]))
        print("   硬违规 %d 条，警告 %d 条" % (len(hard), len(warn)))
        for v in hard[:10]:
            print("   !! %s %s" % (v["code"], v["message"]))
        for v in warn[:10]:
            print("   ~~ %s %s" % (v["code"], v["message"]))
        if hard:
            print("自检未通过 → 不写库。")
            return 3
        if a.apply:
            bak = "%s.bak_%s_pre_component_ratio" % (
                a.db, datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
            if not os.path.exists(bak):
                import shutil
                con.close()
                shutil.copy2(a.db, bak)
                con = sqlite3.connect(a.db)
                print("已备份 → %s" % bak)
            _todo, ndown = downgrade_mapping(con, apply=True)
            print("映射表降档：REQUIRED → OPTIONAL 共 %d 行" % ndown)
            n, staled = write(con, plan)
            print("Component_Ratio 已写入/更新 %d 行；清理旧 ai_estimate 行 %d 行"
                  % (n, staled))
        else:
            print("dry-run：不写库（加 --apply 才写）。")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
