"""模型单价表 —— 单一真源，用于把 token 用量折算成费用。

⚠️ 单价会变，也可能因促销/合约价不同：**请按官方最新价目表核对后再用于对外报价**。
   本表仅用于"让用户知道这次生成大概花了多少钱"的量级参考。

单位：元 / 千 token。
"""

# 汇率：mimo 官方价目以**美元 / 百万 token**计价，本表单位是 元/千token，只在这里
# 折算一次。量级参考（见文件头告警），要对齐当日汇率改这一个数即可。
USD_CNY = 7.15


def usd_per_mtok(usd):
    """美元 / 百万 token → 元 / 千 token。"""
    return round(float(usd) / 1000.0 * USD_CNY, 6)


# ---- 小米 mimo 官方价目（用户提供价目截图，2026-09-22 逐行核对）：美元 / 百万 token ----
#   Input (cache hit)   $0.0028
#   Input (cache miss)  $0.14
#   Output              $0.28
# 记账只拿得到 prompt_tokens / completion_tokens，**分不出缓存命中与否**
# （见 usage.py `record()` 没读 prompt_tokens_details），所以输入一律按 cache miss
# 那档计 —— 沿用本表"宁可高估，不要给用户一个过于乐观的数字"的口径。
MIMO_USD_PER_MTOK = {"input_cache_hit": 0.0028,
                     "input_cache_miss": 0.14,
                     "output": 0.28}
MIMO_PRICE = {"input": usd_per_mtok(MIMO_USD_PER_MTOK["input_cache_miss"]),
              "output": usd_per_mtok(MIMO_USD_PER_MTOK["output"])}

# 每千 token 单价（输入 / 输出）
MODEL_PRICES = {
    # 前缀匹配兜住 mimo-v2.5 / mimo-v2.6 / mimo-v2.6-flash …（price_for 走 startswith）
    "mimo":             MIMO_PRICE,
    "qwen-plus":       {"input": 0.0008, "output": 0.002},
    "qwen-plus-latest": {"input": 0.0008, "output": 0.002},
    "qwen-turbo":      {"input": 0.0003, "output": 0.0006},
    "qwen-max":        {"input": 0.0024, "output": 0.0096},
    "qwen-long":       {"input": 0.0005, "output": 0.002},
}

# 未登记模型时的保守估计（宁可高估，不要给用户一个过于乐观的数字）
DEFAULT_PRICE = {"input": 0.001, "output": 0.002}


def price_for(model):
    """取某模型的单价；未登记时返回默认值。"""
    model = (model or "").strip()
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    # 前缀匹配：qwen-plus-2025-xx 之类
    for key, val in MODEL_PRICES.items():
        if model.startswith(key):
            return val
    return DEFAULT_PRICE


def cost_of(model, prompt_tokens, completion_tokens):
    """按 token 数算费用（元），保留 4 位小数。"""
    p = price_for(model)
    try:
        pt = max(0, int(prompt_tokens or 0))
        ct = max(0, int(completion_tokens or 0))
    except (TypeError, ValueError):
        return 0.0
    cost = pt / 1000.0 * p["input"] + ct / 1000.0 * p["output"]
    return round(cost, 4)


def describe():
    """人类可读的单价说明（用于在终端/交付物里标注口径，终端 `/cost` 与 Word 交付物都会打它）。"""
    sample = ", ".join(
        "{} 入{} 出{}".format(k, v["input"], v["output"])
        for k, v in list(MODEL_PRICES.items())[:2])
    return ("单价表：元/千token（{}）；mimo 为官方美元价 ×{} 折算，"
            "输入按未命中缓存计".format(sample, USD_CNY))
