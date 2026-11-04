"""A-007 production model-config factory.

One factory for UI/CLI/live driver.  It passes an explicit environment mapping
and the selected provider to B's ModelConfig, so a missing process environment
is never silently treated as an empty mapping.  It reads no key files and
prints no secret.
"""

from __future__ import annotations

import os

from mes_assistant.llm_client import ModelClientError, ModelConfig

DEFAULT_PROVIDER = "siliconflow"
DEFAULT_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
PROVIDERS = ("deepseek", "siliconflow")


def build_model_config(environ=None, *, provider=None, config_cls=None):
    """Build B ModelConfig from an explicit mapping; default SF + baseline model."""
    source = os.environ if environ is None else environ
    if not hasattr(source, "get"):
        raise ModelClientError("CONFIG_MISSING", "environment mapping is invalid")
    selected = provider if provider is not None else source.get("MES_LLM_PROVIDER")
    if not isinstance(selected, str) or not selected.strip():
        selected = DEFAULT_PROVIDER
    selected = selected.strip()
    if selected not in PROVIDERS:
        raise ModelClientError("CONFIG_MISSING", "unsupported model provider")

    mapping = dict(source)
    if selected == "siliconflow" and not mapping.get("MES_LLM_MODEL"):
        mapping["MES_LLM_MODEL"] = DEFAULT_MODEL
    cls = ModelConfig if config_cls is None else config_cls
    try:
        return cls.from_env(mapping, provider=selected)
    except TypeError:
        # B before B-007 has no provider-aware from_env; fail explicitly instead
        # of silently using an unrelated/default provider.
        raise ModelClientError(
            "CONFIG_MISSING", "provider-aware model configuration is unavailable"
        ) from None
