"""模型单价表 —— 单一真源，用于把 token 用量折算成费用。

⚠️ 单价会变，也可能因促销/合约价不同：**请按官方最新价目表核对后再用于对外报价**。
   本表仅用于"让用户知道这次生成大概花了多少钱"的量级参考。

单位：元 / 千 token。
"""

# 每千 token 单价（输入 / 输出）
MODEL_PRICES = {
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
    """人类可读的单价说明（用于在终端/交付物里标注口径）。"""
    return "单价表：元/千token；默认模型 " + ", ".join(
        "{} 入{} 出{}".format(k, v["input"], v["output"])
        for k, v in list(MODEL_PRICES.items())[:2])
