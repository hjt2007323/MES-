"""Standard-library, non-streaming Chat Completions client (B-005).

Scope: explicit configuration, one request per call, a shared monotonic
deadline, bounded response size, injectable transport/clock and fixed error
codes.  It performs **no** real network call in this card: the default urllib
transport is defined but the offline verification only injects test transport.

This module returns only assistant ``content``.  It does not parse SQL, execute
tools, read ``reasoning_content`` or retry.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

__all__ = [
    "ENDPOINT",
    "ENDPOINT_DEEPSEEK",
    "ENDPOINT_SILICONFLOW",
    "DEFAULT_SILICONFLOW_MODEL",
    "PROVIDER_ENDPOINTS",
    "MAX_CALL_MS",
    "MAX_MESSAGES",
    "MAX_RESPONSE_BYTES",
    "MAX_TOTAL_CHARS",
    "ModelClientError",
    "ModelConfig",
    "ModelReply",
    "ChatModelClient",
]

ENDPOINT = "https://api.deepseek.com/chat/completions"  # legacy alias for deepseek
ENDPOINT_DEEPSEEK = ENDPOINT
ENDPOINT_SILICONFLOW = "https://api.siliconflow.cn/v1/chat/completions"
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_SILICONFLOW_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
PROVIDER_ENDPOINTS = {
    "deepseek": ENDPOINT_DEEPSEEK,
    "siliconflow": ENDPOINT_SILICONFLOW,
}
MAX_CALL_MS = 10_000
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MESSAGES = 8
MAX_TOTAL_CHARS = 16_000
MAX_TOKENS = 512
ALLOWED_ROLES = ("system", "user", "assistant")

CONFIG_MISSING = "CONFIG_MISSING"
INPUT_INVALID = "INPUT_INVALID"
MODEL_TIMEOUT = "MODEL_TIMEOUT"
HTTP_ERROR = "HTTP_ERROR"
NETWORK_ERROR = "NETWORK_ERROR"
OUTPUT_INVALID = "OUTPUT_INVALID"
RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"

ERROR_CODES = (
    CONFIG_MISSING,
    INPUT_INVALID,
    MODEL_TIMEOUT,
    HTTP_ERROR,
    NETWORK_ERROR,
    OUTPUT_INVALID,
    RESPONSE_TOO_LARGE,
)


class ModelClientError(Exception):
    """Fixed-code local failure; never contains the request, key or raw exception."""

    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError("unknown ModelClientError code")
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.message)


def _present(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _first_present(source, names):
    for name in names:
        value = source.get(name)
        if _present(value):
            return value
    return None


def _validate_siliconflow_base_url(value) -> None:
    """Allow only absent/blank or the fixed siliconflow base URL (optional trailing /)."""
    if value is None:
        return
    if not isinstance(value, str):
        raise ModelClientError(CONFIG_MISSING, "SILICONFLOW_BASE_URL is invalid")
    if not value.strip():
        return
    if value.strip().rstrip("/") != SILICONFLOW_BASE_URL:
        raise ModelClientError(
            CONFIG_MISSING, "SILICONFLOW_BASE_URL must be the fixed siliconflow base URL"
        )


class ModelConfig:
    """Explicit model configuration; the API key never appears in repr/str."""

    def __init__(self, api_key: str, model: str, *, provider: str = "deepseek"):
        if not isinstance(provider, str) or provider not in PROVIDER_ENDPOINTS:
            raise ModelClientError(CONFIG_MISSING, "provider is not supported")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ModelClientError(CONFIG_MISSING, "API key is missing")
        if not isinstance(model, str) or not model.strip():
            raise ModelClientError(CONFIG_MISSING, "model name is missing")
        self._api_key = api_key
        self.model = model
        self._provider = provider

    @property
    def provider(self) -> str:
        """Read-only provider enum; assignment is rejected."""
        return self._provider

    @property
    def endpoint(self) -> str:
        """Read-only fixed endpoint for this provider; assignment is rejected."""
        return PROVIDER_ENDPOINTS[self._provider]

    @property
    def api_key(self) -> str:
        return self._api_key

    @classmethod
    def from_env(cls, mapping=None, *, provider: str = "deepseek") -> "ModelConfig":
        if not isinstance(provider, str) or provider not in PROVIDER_ENDPOINTS:
            raise ModelClientError(CONFIG_MISSING, "provider is not supported")
        if mapping is None:
            source = os.environ
        else:
            if not hasattr(mapping, "get"):
                raise ModelClientError(CONFIG_MISSING, "environment mapping is invalid")
            source = mapping

        if provider == "deepseek":
            key = _first_present(source, ("MES_LLM_API_KEY", "DEEPSEEK_API_KEY"))
            if not key:
                raise ModelClientError(
                    CONFIG_MISSING, "no API key in MES_LLM_API_KEY or DEEPSEEK_API_KEY"
                )
            model = source.get("MES_LLM_MODEL")
            if not _present(model):
                raise ModelClientError(CONFIG_MISSING, "MES_LLM_MODEL is required")
            return cls(key, model, provider=provider)

        key = source.get("SILICONFLOW_API_KEY")
        if not _present(key):
            raise ModelClientError(
                CONFIG_MISSING, "SILICONFLOW_API_KEY is required for siliconflow"
            )
        model = source.get("MES_LLM_MODEL")
        if not _present(model):
            model = DEFAULT_SILICONFLOW_MODEL
        _validate_siliconflow_base_url(source.get("SILICONFLOW_BASE_URL"))
        return cls(key, model, provider=provider)

    def __repr__(self) -> str:
        return "ModelConfig(provider=%r, model=%r, api_key=<hidden>)" % (
            self._provider,
            self.model,
        )

    __str__ = __repr__


@dataclass(frozen=True)
class ModelReply:
    content: str
    usage: object


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib raises HTTPError instead of following


class _UrllibResponse:
    def __init__(self, response):
        self._response = response
        self.status = getattr(response, "status", None)
        if self.status is None:
            self.status = response.getcode()

    def read(self, size: int) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        self._response.close()


class _UrllibTransport:
    """Default HTTPS transport: TLS verification on, no redirects, no env proxy."""

    def __init__(self):
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect,
        )

    def open(self, *, url: str, headers: dict, body: bytes, timeout_s: float):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        return _UrllibResponse(self._opener.open(request, timeout=max(timeout_s, 0.001)))


def _close_http_error(error) -> None:
    """Close any response resource carried by urllib.error.HTTPError."""
    fp = getattr(error, "fp", None)
    if fp is None:
        fp = getattr(error, "file", None)
    for resource in (fp, error):
        close = getattr(resource, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - cleanup must not mask the error class
                pass


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constants(value):
    raise ValueError("non-finite JSON constant")


def _normalize_usage(raw) -> object:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ModelClientError(OUTPUT_INVALID, "usage field is not an object")
    usage = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw.get(name)
        if value is None:
            usage[name] = None
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ModelClientError(OUTPUT_INVALID, "usage field has an invalid value")
        else:
            usage[name] = value
    return usage


class ChatModelClient:
    """One non-streaming chat completion per ``complete`` call."""

    def __init__(self, config, *, transport=None, clock=None):
        if config is None:
            raise ModelClientError(CONFIG_MISSING, "model configuration is required")
        if not isinstance(config, ModelConfig):
            raise ModelClientError(INPUT_INVALID, "config must be a ModelConfig")
        self._config = config
        self._transport = _UrllibTransport() if transport is None else transport
        self._clock = time.monotonic_ns if clock is None else clock
        self._attempts = 0

    @property
    def attempts(self) -> int:
        """Transport open attempts actually made (never a server-side confirmation)."""
        return self._attempts

    # -- validation -----------------------------------------------------
    @staticmethod
    def _validate_messages(messages) -> list:
        if not isinstance(messages, list):
            raise ModelClientError(INPUT_INVALID, "messages must be a list")
        if not 1 <= len(messages) <= MAX_MESSAGES:
            raise ModelClientError(INPUT_INVALID, "messages must contain 1..8 items")
        total_chars = 0
        normalized = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise ModelClientError(INPUT_INVALID, "message %d is not an object" % index)
            if set(message) != {"role", "content"}:
                raise ModelClientError(
                    INPUT_INVALID, "message %d must contain only role and content" % index
                )
            role = message["role"]
            content = message["content"]
            if role not in ALLOWED_ROLES:
                raise ModelClientError(INPUT_INVALID, "message %d has an unsupported role" % index)
            if not isinstance(content, str):
                raise ModelClientError(INPUT_INVALID, "message %d content is not a string" % index)
            total_chars += len(content)
            normalized.append({"role": role, "content": content})
        if total_chars > MAX_TOTAL_CHARS:
            raise ModelClientError(INPUT_INVALID, "messages exceed the total character limit")
        return normalized

    # -- request/response ----------------------------------------------
    def complete(self, messages: list, *, timeout_ms: int) -> ModelReply:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise ModelClientError(INPUT_INVALID, "timeout_ms must be an integer")
        normalized = self._validate_messages(messages)
        if timeout_ms <= 0:
            raise ModelClientError(
                MODEL_TIMEOUT, "no time remains; nothing was dispatched and no request was sent"
            )

        effective_ms = min(timeout_ms, MAX_CALL_MS)
        deadline_ns = self._clock() + effective_ms * 1_000_000
        if self._clock() >= deadline_ns:
            raise ModelClientError(
                MODEL_TIMEOUT, "deadline had already elapsed before dispatch"
            )

        payload = {
            "model": self._config.model,
            "messages": normalized,
            "stream": False,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + self._config.api_key,
        }

        response = None
        # R1: serialization itself consumes budget; re-check before open.
        if self._clock() >= deadline_ns:
            raise ModelClientError(
                MODEL_TIMEOUT, "deadline elapsed while serializing; nothing was dispatched"
            )
        # The last remaining-time check must happen before the attempt counter:
        # an expired budget is not an actual transport attempt.
        remaining_s = (deadline_ns - self._clock()) / 1_000_000_000
        if remaining_s <= 0:
            raise ModelClientError(
                MODEL_TIMEOUT, "deadline elapsed before the transport was opened"
            )
        self._attempts += 1
        try:
            try:
                response = self._transport.open(
                    url=PROVIDER_ENDPOINTS[self._config.provider],
                    headers=headers,
                    body=body,
                    timeout_s=min(remaining_s, MAX_CALL_MS / 1000.0),
                )
            except urllib.error.HTTPError as exc:
                _close_http_error(exc)
                raise ModelClientError(HTTP_ERROR, "HTTP status was not 200") from None
            except (socket.timeout, TimeoutError):
                raise ModelClientError(
                    MODEL_TIMEOUT, "local wait stopped while opening the connection"
                ) from None
            except Exception:  # noqa: BLE001 - raw exception text must not leak
                raise ModelClientError(NETWORK_ERROR, "transport failed before a response") from None

            if self._clock() >= deadline_ns:
                raise ModelClientError(
                    MODEL_TIMEOUT,
                    "local wait stopped after the response headers and the response was closed",
                )

            status = getattr(response, "status", None)
            if status != 200:
                raise ModelClientError(HTTP_ERROR, "HTTP status was not 200")

            data = bytearray()
            while True:
                if self._clock() >= deadline_ns:
                    raise ModelClientError(
                        MODEL_TIMEOUT,
                        "local wait stopped while reading; the response was closed",
                    )
                room = MAX_RESPONSE_BYTES + 1 - len(data)
                if room <= 0:
                    raise ModelClientError(
                        RESPONSE_TOO_LARGE, "response exceeded the 1 MiB limit"
                    )
                try:
                    chunk = response.read(min(65536, room))
                except (socket.timeout, TimeoutError):
                    raise ModelClientError(
                        MODEL_TIMEOUT, "local wait stopped while reading the response"
                    ) from None
                except Exception:  # noqa: BLE001
                    raise ModelClientError(NETWORK_ERROR, "transport failed while reading") from None
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ModelClientError(
                        RESPONSE_TOO_LARGE, "response exceeded the 1 MiB limit"
                    )
            if self._clock() >= deadline_ns:
                raise ModelClientError(
                    MODEL_TIMEOUT, "local wait stopped after the body was read"
                )

            try:
                text = bytes(data).decode("utf-8")
            except UnicodeDecodeError:
                raise ModelClientError(OUTPUT_INVALID, "response was not valid UTF-8") from None

            try:
                document = json.loads(
                    text,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_constants,
                )
            except (ValueError, TypeError):
                raise ModelClientError(OUTPUT_INVALID, "response was not valid JSON") from None

            if self._clock() >= deadline_ns:
                raise ModelClientError(
                    MODEL_TIMEOUT, "local wait stopped after parsing the response"
                )

            reply = self._parse_reply(document)
            # R1: reply validation is part of the same deadline; late replies are not ok.
            if self._clock() >= deadline_ns:
                raise ModelClientError(
                    MODEL_TIMEOUT, "local wait stopped after validating the reply"
                )
            return reply
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:  # noqa: BLE001 - close must not change the result
                    pass

    def _parse_reply(self, document) -> ModelReply:
        if not isinstance(document, dict):
            raise ModelClientError(OUTPUT_INVALID, "response was not a JSON object")
        choices = document.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelClientError(OUTPUT_INVALID, "response has no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelClientError(OUTPUT_INVALID, "first choice is not an object")
        message = first.get("message")
        if not isinstance(message, dict):
            raise ModelClientError(OUTPUT_INVALID, "first choice has no message object")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelClientError(OUTPUT_INVALID, "message content is empty")
        usage = _normalize_usage(document.get("usage"))
        return ModelReply(content=content, usage=usage)
