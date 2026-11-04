"""A-004 M1 结构化 JSONL 日志。

只写白名单字段；调用方不应把问题全文、参数、数据行、SQL、凭据或异常原文
放进事件。日志证据不是数据库取消或成功恢复的证据。
"""

import json

LOG_WHITELIST = (
    "run_id",
    "request_id",
    "query_id",
    "config_id",
    "data_version",
    "status",
    "error_code",
    "counts",
    "duration_ms",
    "review_status",
)
_COUNT_KEYS = ("model_calls", "queries", "corrections")


def _sanitized_event(event):
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    payload = {}
    for key in LOG_WHITELIST:
        if key in event:
            payload[key] = event[key]
    counts = payload.get("counts")
    if isinstance(counts, dict):
        payload["counts"] = {
            key: counts[key] for key in _COUNT_KEYS if key in counts
        }
    return payload


def write_event(stream, event: dict) -> None:
    """向流写入一行脱敏 JSON；流打开/写入失败由调用方处理。"""
    payload = _sanitized_event(event)
    line = json.dumps(payload, ensure_ascii=True, allow_nan=False)
    stream.write(line + "\n")
    flush = getattr(stream, "flush", None)
    if flush is not None:
        flush()
