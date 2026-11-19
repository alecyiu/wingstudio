"""Model calls through LiteLLM, shared by the command-line agent and the studio."""


def completion():
    """litellm.completion, imported on first use (it is slow to import) and quiet."""
    import litellm
    litellm.suppress_debug_info = True
    return litellm.completion


def usage(response, model):
    """(input tokens, output tokens, cached input tokens, cost in USD) of one response.
    The cost is 0.0 when LiteLLM does not know the model's price."""
    u = getattr(response, "usage", None)
    tokens_in = getattr(u, "prompt_tokens", 0) or 0
    tokens_out = getattr(u, "completion_tokens", 0) or 0
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    try:
        import litellm
        cost = litellm.completion_cost(completion_response=response, model=model)
    except Exception:  # noqa: BLE001 - unknown model or a test double
        hidden = getattr(response, "_hidden_params", None) or {}
        cost = hidden.get("response_cost") if isinstance(hidden, dict) else None
    return tokens_in, tokens_out, cached, float(cost or 0.0)
