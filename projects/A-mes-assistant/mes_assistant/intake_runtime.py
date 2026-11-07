"""A-011 unified offline/real intake entry point.

The page and the batch driver both call ``run_intake_request``; extraction is
always the formal ``intake_extraction.extract_draft``.  The wrapper only tracks
calls/usage/error codes and never exposes credentials.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from mes_assistant.intake_extraction import (
    FIELDS,
    REQUIRED_FIELDS,
    extract_draft,
)
from mes_assistant.llm_client import (
    ChatModelClient,
    ModelClientError,
    ModelConfig,
    ModelReply,
)

OFFLINE_SOURCE = "offline_template"
TEST_SOURCE = "test_double"
HTTP_SOURCE = "http_model"
CONFIG_MISSING = "CONFIG_MISSING"
CONFIG_MODEL_MISMATCH = "CONFIG_MODEL_MISMATCH"
INPUT_INVALID = "INPUT_INVALID"
MODEL_FAILED = "MODEL_FAILED"
STOP_CODES = frozenset(
    {
        "CONFIG_MISSING",
        "CONFIG_MODEL_MISMATCH",
        "HTTP_ERROR",
        "NETWORK_ERROR",
        "BUDGET_INVALID",
        "INPUT_BINDING_FAILED",
    }
)


class OfflineIntakeModel:
    """Tiny explicit key=value parser; no frozen answer knowledge."""

    def __init__(self):
        self.calls = 0

    def complete(self, messages, *, timeout_ms):
        self.calls += 1
        text = ""
        for message in messages:
            if message.get("role") == "user":
                content = message.get("content", "")
                match = re.search(r"原文：\n(.*?)\n\n请只返回", content, re.DOTALL)
                if match:
                    text = match.group(1)
        values = {}
        for field in FIELDS:
            matches = re.findall(
                r"(?<![A-Za-z0-9_])%s\s*=\s*([^；;\n]*)"
                % re.escape(field),
                text,
            )
            if len(matches) > 1:
                return ModelReply(
                    content=json.dumps(
                        {"kind": "clarify", "issue": "MULTIPLE_EVENTS"},
                        ensure_ascii=False,
                    ),
                    usage=None,
                )
            if matches:
                values[field] = matches[0].strip()
        if not all(values.get(field) for field in REQUIRED_FIELDS):
            payload = {"kind": "clarify", "issue": "AMBIGUOUS_CONTEXT"}
        else:
            fields = {}
            for field in FIELDS:
                value = values.get(field) or None
                fields[field] = {"value": value, "quote": value}
            payload = {"kind": "extract", "fields": fields}
        return ModelReply(
            content=json.dumps(payload, ensure_ascii=False),
            usage=None,
        )


class _TrackingModel:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0
        self.usage = None
        self.error_code = None

    def complete(self, messages, *, timeout_ms):
        self.calls += 1
        try:
            reply = self.delegate.complete(messages, timeout_ms=timeout_ms)
        except Exception as exc:
            self.error_code = getattr(exc, "code", MODEL_FAILED)
            raise
        self.usage = getattr(reply, "usage", None)
        return reply


def _empty_draft(mode, code):
    return {
        "draft_id": uuid.uuid4().hex,
        "state": "error",
        "record": {field: None for field in FIELDS},
        "evidence": {field: None for field in FIELDS},
        "issues": [code],
        "mode": mode,
        "terminal": True,
        "pending_fields": [],
    }


def _error_result(mode, code, started, *, client_calls=0, source=TEST_SOURCE,
                  provider=None, model=None, usage=None):
    return {
        "draft": _empty_draft(mode, code),
        "run": {
            "mode": mode,
            "execution_source": source,
            "provider": provider,
            "model": model,
            "client_calls": client_calls,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "usage": usage,
            "error_code": code,
        },
    }


def _usage_dict(usage):
    if not isinstance(usage, dict):
        return None
    result = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        result[key] = value if isinstance(value, int) and not isinstance(value, bool) else None
    return result


def check_real_config(env=None, *, expected_provider=None, expected_model=None):
    """Check the real config without constructing a client or dispatching.

    The returned dict never includes the key.  Batch uses this before creating
    a result directory so a model/provider mismatch is a zero-dispatch failure.
    """
    try:
        config = ModelConfig.from_env(env, provider="siliconflow")
    except ModelClientError as exc:
        return {
            "ok": False,
            "error_code": exc.code if exc.code in STOP_CODES else CONFIG_MISSING,
            "provider": "siliconflow",
            "model": None,
        }
    except Exception:
        return {
            "ok": False,
            "error_code": CONFIG_MISSING,
            "provider": "siliconflow",
            "model": None,
        }
    if expected_provider is not None and config.provider != expected_provider:
        return {
            "ok": False,
            "error_code": CONFIG_MODEL_MISMATCH,
            "provider": config.provider,
            "model": config.model,
        }
    if expected_model is not None and config.model != expected_model:
        return {
            "ok": False,
            "error_code": CONFIG_MODEL_MISMATCH,
            "provider": config.provider,
            "model": config.model,
        }
    return {
        "ok": True,
        "error_code": None,
        "provider": config.provider,
        "model": config.model,
    }


def run_intake_request(
    text,
    *,
    reference_time,
    mode="offline",
    model_client=None,
    env=None,
    expected_provider=None,
    expected_model=None,
):
    """Run one intake request; offline by default and never writes storage."""
    started = time.perf_counter()
    if mode not in ("offline", "real"):
        return _error_result(mode, INPUT_INVALID, started)

    provider = None
    model_name = None
    if model_client is not None:
        if mode == "offline" and isinstance(model_client, OfflineIntakeModel):
            source = OFFLINE_SOURCE
        else:
            source = TEST_SOURCE
        base = model_client
        provider = None
        model_name = getattr(base, "model", None)
    elif mode == "offline":
        source = OFFLINE_SOURCE
        base = OfflineIntakeModel()
    else:
        checked = check_real_config(
            env,
            expected_provider=expected_provider,
            expected_model=expected_model,
        )
        if not checked["ok"]:
            return _error_result(
                mode,
                checked["error_code"],
                started,
                source=HTTP_SOURCE,
                provider=checked.get("provider"),
                model=checked.get("model"),
            )
        try:
            config = ModelConfig.from_env(env, provider="siliconflow")
        except ModelClientError as exc:
            return _error_result(
                mode,
                exc.code if exc.code in STOP_CODES else CONFIG_MISSING,
                started,
                source=HTTP_SOURCE,
                provider="siliconflow",
            )
        except Exception:
            return _error_result(
                mode, CONFIG_MISSING, started, source=HTTP_SOURCE, provider="siliconflow"
            )
        provider = config.provider
        model_name = config.model
        try:
            base = ChatModelClient(config)
        except ModelClientError as exc:
            return _error_result(
                mode,
                exc.code if exc.code in STOP_CODES else CONFIG_MISSING,
                started,
                source=HTTP_SOURCE,
                provider=provider,
                model=model_name,
            )
        source = HTTP_SOURCE

    tracker = _TrackingModel(base)
    try:
        draft = extract_draft(
            text,
            reference_time=reference_time,
            model_client=tracker,
        )
    except ValueError:
        return _error_result(
            mode,
            INPUT_INVALID,
            started,
            client_calls=tracker.calls,
            source=source,
            provider=provider,
            model=model_name,
        )
    except Exception:
        return _error_result(
            mode,
            MODEL_FAILED,
            started,
            client_calls=tracker.calls,
            source=source,
            provider=provider,
            model=model_name,
        )

    error_code = None
    if draft.get("state") == "error":
        issues = draft.get("issues") or []
        error_code = tracker.error_code or (issues[0] if issues else MODEL_FAILED)
    usage = None if source == OFFLINE_SOURCE else _usage_dict(tracker.usage)
    return {
        "draft": draft,
        "run": {
            "mode": mode,
            "execution_source": source,
            "provider": provider,
            "model": model_name,
            "client_calls": tracker.calls,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "usage": usage,
            "error_code": error_code,
        },
    }
