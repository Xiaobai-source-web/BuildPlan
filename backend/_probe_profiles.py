# -*- coding: utf-8 -*-
"""探针：验证多档配置的加载/切换，以及"刷新 config 后新建的 LLMClient 真的换了一家"。

在 backend/ 下跑：python -X utf8 _probe_profiles.py
只写临时目录，绝不碰真实 backend/llm_profiles.json。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

tmp = Path(tempfile.mkdtemp(prefix="llmprof_"))
os.environ["BUILDPLAN_LLM_PROFILES"] = str(tmp / "profs.json")

from pipeline import config, llm_profiles as lp  # noqa: E402
from pipeline.llm import LLMClient  # noqa: E402
from pipeline import builder  # noqa: E402

print("== 1. 空配置不许自动建档 ==")
snap = lp.snapshot()
print("  档数:", len(snap["profiles"]), "| active:", repr(snap["active_id"]))
print("  无档基线（回显用）:", snap["fallback"]["host"], "|", snap["fallback"]["model"],
      "| key", snap["fallback"]["key_masked"])
assert snap["profiles"] == [], "空配置不该有任何档位"
before = (config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL)
print("  refresh(无档) 来源:", repr(config.refresh_active_profile()))
after = (config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL)
assert before == after, "无档时 refresh 必须是 no-op（否则会拆掉测试的禁网保障）"
print("  [OK] 无档 refresh 未改动任何值")

print("== 2. 新增两档 ==")
lp.add("千问 · 主力", "https://dashscope.aliyuncs.com/compatible-mode/v1",
       "qwen-plus", "sk-aaaaaaaaaaaaaaaaaaaa1111")
lp.add("DeepSeek · 备用", "https://api.deepseek.com/v1",
       "deepseek-chat", "sk-bbbbbbbbbbbbbbbbbbbb2222")
for it in lp.snapshot()["profiles"]:
    print("   [%d] %-14s %-28s %-13s %s"
          % (it["index"], it["name"], it["host"], it["model"], it["key_masked"]))

print("== 3. 切到第 2 档：refresh 后新建的 LLMClient 必须换了一家 ==")
print("  use('2') ->", lp.use("2")["name"])
print("  refresh 来源:", config.refresh_active_profile())
print("  config:", config.describe_provider(), "| key尾:", config.LLM_API_KEY[-4:])
c = LLMClient()                      # 模拟 build_pipeline() 里新建的客户端
print("  LLMClient:", c.model, "|", c.base_url, "| key尾:", c.api_key[-4:])
assert c.model == "deepseek-chat" and c.api_key.endswith("2222"), "切档未生效"
assert any(n.name == "norm_bind" for n in builder.build_pipeline(run_id="probe").nodes), \
    "build_pipeline 应能在 refresh 之后正常构造"

print("== 4. 按名字唯一前缀切回第 1 档 ==")
print("  use('千问') ->", lp.use("千问")["name"])
config.refresh_active_profile()
c2 = LLMClient()
print("  LLMClient:", c2.model, "| key尾:", c2.api_key[-4:])
assert c2.model == "qwen-plus" and c2.api_key.endswith("1111")
print("  未知名字不许乱切:", lp.use("不存在的档"))

print("== 5. 序号只是序号：删档后 active 归零，不抛异常 ==")
print("  remove('1') ->", lp.remove("1")["name"])
snap = lp.snapshot()
print("  active:", repr(snap["active_id"]), "| 剩", len(snap["profiles"]), "档")
print("  删当前档后 refresh:", repr(config.refresh_active_profile()), "（no-op）")

print("== 6. 坏文件不许抛异常 ==")
(tmp / "profs.json").write_text("{ 这不是 JSON", encoding="utf-8")
print("  load ->", lp.load())
(tmp / "profs.json").write_text('[1,2,3]', encoding="utf-8")
print("  load(非 dict) ->", lp.load())
(tmp / "profs.json").write_text(json.dumps(
    {"active": "已删除的id", "profiles": [{"id": "x", "name": "只剩一行"}]},
    ensure_ascii=False), encoding="utf-8")
print("  active 指向已删档 ->", json.dumps(lp.load(), ensure_ascii=False))

print("== 7. 打码不泄露完整 key ==")
k = "tp-c6xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx1g56"
m = lp.mask_key(k)
print("  mask:", m)
assert k not in m and len(m) < len(k), "打码必须真的变短"
print("  mask(短key):", lp.mask_key("abc"), "| mask(空):", lp.mask_key(""))

print("\n[OK] 全部断言通过；临时目录：", tmp)
