"""A-010 offline extraction for one synthetic downtime event.

The model is a ChatModelClient-compatible proposer only.  This module enforces
the strict JSON action, five fields, original-text quote uniqueness, normalized
business fields and a single timeout.  It never writes storage.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

FIELDS = ("machine_no", "start_time", "end_time", "reason", "order_id")
REQUIRED_FIELDS = ("machine_no", "start_time", "end_time", "reason")
MACHINES = ("M01", "M02", "M03")
ORDERS = ("WO01", "WO02", "WO03", "WO04", "WO05", "WO06")
MACHINE_ALIASES = {
    "M01": "M01",
    "M02": "M02",
    "M03": "M03",
    "1号机": "M01",
    "一号机": "M01",
    "2号机": "M02",
    "二号机": "M02",
    "3号机": "M03",
    "三号机": "M03",
}
REFERENCE = "2026-11-04T12:00:00+08:00"
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")
MODEL_TIMEOUT_MS = 10_000
MAX_TEXT = 2000
MAX_QUOTE = 240
CLARIFY_ISSUES = {
    "MULTIPLE_EVENTS",
    "AMBIGUOUS_CONTEXT",
    "MISSING_MACHINE_NO",
    "MISSING_START_TIME",
    "MISSING_END_TIME",
    "MISSING_REASON",
}
_MACHINE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:M0[1-3](?![A-Za-z0-9])|[123一二三]\s*号机)",
    re.IGNORECASE,
)
_TIME_TOKEN_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
_TIME_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2}):(\d{2})\s*(?:到|至|~|—|-)\s*(\d{1,2}):(\d{2})(?!\d)"
)
_END_MISSING_RE = re.compile(
    r"(?:结束|恢复|end)[^。；;\n]{0,12}(?:没记|未记|没有|不详|不知道|缺失|忘)",
    re.IGNORECASE,
)
_AMBIGUOUS_DEVICE_RE = re.compile(r"(?:可能|也许|或|还是|不确定|二选一)")


def _empty_record():
    return {field: None for field in FIELDS}


def _empty_evidence():
    return {field: None for field in FIELDS}


def _machine_tokens(text):
    result = []
    for match in _MACHINE_TOKEN_RE.finditer(text):
        token = match.group(0).replace(" ", "")
        if token.upper().startswith("M"):
            result.append(token.upper())
            continue
        number = token[0]
        result.append({"1": "M01", "一": "M01", "2": "M02", "二": "M02",
                       "3": "M03", "三": "M03"}[number])
    return result


def _time_tokens(text):
    return _TIME_TOKEN_RE.findall(text)


def classify_multi_event(text):
    """Detect at least two distinct explicit time ranges.

    A single event may mention one device repeatedly, use aliases, or repeat
    the same interval.  Two independent ranges (same or different device) are
    multi-event; device choice with one repeated interval is not.
    """
    if not isinstance(text, str):
        return False
    ranges = set()
    for match in _TIME_RANGE_RE.finditer(text):
        start = (int(match.group(1)), int(match.group(2)))
        end = (int(match.group(3)), int(match.group(4)))
        ranges.add((start, end))
    return len(ranges) >= 2


def _device_ambiguity(text):
    return len(set(_machine_tokens(text))) >= 2 and bool(
        _AMBIGUOUS_DEVICE_RE.search(text)
    )


def normalize_clarify_issue(text, issue):
    """Align generic model clarification with the frozen missing-field codes."""
    if issue == "AMBIGUOUS_CONTEXT":
        if _END_MISSING_RE.search(text):
            return "MISSING_END_TIME"
        if _device_ambiguity(text):
            return "MISSING_MACHINE_NO"
    if issue == "MULTIPLE_EVENTS" and not classify_multi_event(text):
        if _device_ambiguity(text):
            return "MISSING_MACHINE_NO"
    return issue


def _parse_time(value):
    if not isinstance(value, str) or not TIME_RE.match(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.utcoffset() != timedelta(hours=8):
        return None
    return parsed


def validate_record(record):
    """Normalize and check the five-field record; return (record, issues)."""
    normalized = _empty_record()
    issues = []
    if not isinstance(record, dict):
        return normalized, ["INVALID_RECORD"]
    for field in FIELDS:
        value = record.get(field)
        if isinstance(value, str):
            value = value.strip()
        if value == "":
            value = None
        normalized[field] = value

    machine = normalized.get("machine_no")
    if machine is not None:
        mapped = MACHINE_ALIASES.get(str(machine).strip())
        if mapped is None:
            issues.append("UNKNOWN_DEVICE")
        else:
            normalized["machine_no"] = mapped

    for field in REQUIRED_FIELDS:
        if normalized.get(field) is None:
            issues.append("MISSING_%s" % field.upper())

    for field in ("start_time", "end_time"):
        value = normalized.get(field)
        if value is not None and _parse_time(value) is None:
            issues.append("INVALID_TIME_FORMAT")

    start = _parse_time(normalized.get("start_time"))
    end = _parse_time(normalized.get("end_time"))
    if start is not None and end is not None and end <= start:
        issues.append("INVALID_TIME_ORDER")

    reason = normalized.get("reason")
    if reason is not None and not (1 <= len(reason) <= 32):
        issues.append("INVALID_REASON")

    order = normalized.get("order_id")
    if order is not None:
        order = str(order).strip().upper()
        normalized["order_id"] = order
        if order not in ORDERS:
            issues.append("UNKNOWN_ORDER")

    return normalized, _dedupe(issues)


def _dedupe(items):
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _strict_json(content):
    def reject_constant(value):
        raise ValueError("non-finite JSON")

    return json.loads(
        content,
        object_pairs_hook=_strict_pairs,
        parse_constant=reject_constant,
    )


def _quote_evidence(text, quote):
    if not isinstance(quote, str) or not quote or len(quote) > MAX_QUOTE:
        return None
    first = text.find(quote)
    if first < 0:
        return None
    if text.find(quote, first + 1) >= 0:
        return None
    return {"quote": quote, "start": first, "end": first + len(quote)}


def _parse_extract(payload):
    if not isinstance(payload, dict) or set(payload) != {"kind", "fields"}:
        raise ValueError("extract shape")
    if payload.get("kind") != "extract":
        raise ValueError("kind")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or set(fields) != set(FIELDS):
        raise ValueError("fields shape")
    result = {}
    for field in FIELDS:
        entry = fields[field]
        if not isinstance(entry, dict) or set(entry) != {"value", "quote"}:
            raise ValueError("field shape")
        value = entry.get("value")
        quote = entry.get("quote")
        if value is not None and not isinstance(value, str):
            raise ValueError("field value")
        if quote is not None and not isinstance(quote, str):
            raise ValueError("field quote")
        if (value is None) != (quote is None):
            raise ValueError("value/quote pair")
        result[field] = {"value": value, "quote": quote}
    return result


def _draft(
    state,
    *,
    record=None,
    evidence=None,
    issues=None,
    mode="model",
    draft_id=None,
    terminal=False,
    pending_fields=None,
):
    return {
        "draft_id": draft_id or uuid.uuid4().hex,
        "state": state,
        "record": _empty_record() if record is None else record,
        "evidence": _empty_evidence() if evidence is None else evidence,
        "issues": list(issues or []),
        "mode": mode,
        "terminal": terminal,
        "pending_fields": list(pending_fields or []),
    }


def _prompt_messages(text, reference_time):
    system = (
        "你是 M5 停机录入提取器。只输出严格 JSON，不得输出 SQL、代码或解释。\n"
        "允许动作：\n"
        '{"kind":"extract","fields":{"machine_no":{"value":字符串或null,"quote":字符串或null},'
        '"start_time":{...},"end_time":{...},"reason":{...},"order_id":{...}}}\n'
        '{"kind":"clarify","issue":"AMBIGUOUS_CONTEXT"|"MULTIPLE_EVENTS"|'
        '"MISSING_MACHINE_NO"|"MISSING_START_TIME"|"MISSING_END_TIME"|"MISSING_REASON"}\n'
        '{"kind":"refuse","issue":"OUT_OF_SCOPE"}\n'
        "fields 必须恰有五个键；非 null 值的 quote 必须是原文中恰好出现一次的连续片段，"
        "null 值 quote 必须为 null。\n"
        "单条事件可识别但字段缺失时，用 extract 并把缺失字段 value/quote 设为 null；"
        "结束时间缺失用 MISSING_END_TIME，设备不唯一/可能是A或B时用 MISSING_MACHINE_NO，"
        "不要用 MULTIPLE_EVENTS 代替缺项澄清。\n"
        "只有原文明确包含两条及以上独立停机事件时才用 MULTIPLE_EVENTS；"
        "同一设备的重复提及、别名或键值模板不是多事件。\n"
        "时间格式 YYYY-MM-DDTHH:MM:SS+08:00；结束必须晚于开始，不得交换；"
        "只处理一条已经结束的停机事件。\n"
        "设备仅 M01/M02/M03；工单仅 WO01–WO06 或 null；原因保留操作者原词。\n"
        "参考时间：%s；时区 Asia/Shanghai。"
        % reference_time
    )
    user = "原文：\n%s\n\n请只返回上述三个动作之一的严格 JSON。" % text
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def extract_draft(text, *, reference_time, model_client):
    """Run exactly one extraction call and return a draft (never writes)."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    stripped = text.strip()
    if not stripped or len(text) > MAX_TEXT:
        raise ValueError("text must be non-empty and <= 2000 characters")
    if not isinstance(reference_time, str) or _parse_time(reference_time) is None:
        raise ValueError("reference_time must be ISO +08:00")
    if classify_multi_event(text):
        return _draft(
            "needs_clarification",
            issues=["MULTIPLE_EVENTS"],
            terminal=True,
        )

    messages = _prompt_messages(text, reference_time)
    try:
        reply = model_client.complete(messages, timeout_ms=MODEL_TIMEOUT_MS)
    except Exception as exc:
        code = getattr(exc, "code", None)
        issue = "MODEL_TIMEOUT" if code == "MODEL_TIMEOUT" else "MODEL_FAILED"
        return _draft("error", issues=[issue], terminal=True)

    content = getattr(reply, "content", None)
    if not isinstance(content, str):
        return _draft("error", issues=["MODEL_FAILED"], terminal=True)
    try:
        payload = _strict_json(content)
    except Exception:
        return _draft("error", issues=["PARSE_INVALID"], terminal=True)

    try:
        if not isinstance(payload, dict):
            raise ValueError("action shape")
        if payload.get("kind") == "extract":
            fields = _parse_extract(payload)
            record = {}
            evidence = {}
            issues = []
            pending_fields = []
            for field in FIELDS:
                value = fields[field]["value"]
                quote = fields[field]["quote"]
                record[field] = value
                if value is not None:
                    found = _quote_evidence(text, quote)
                    if found is None:
                        evidence[field] = None
                        record[field] = None
                        pending_fields.append(field)
                        issues.append("EVIDENCE_MISMATCH")
                    else:
                        evidence[field] = found
                else:
                    evidence[field] = None
            normalized, validation_issues = validate_record(record)
            issues.extend(validation_issues)
            issues = _dedupe(issues)
            return _draft(
                "ready" if not issues else "needs_clarification",
                record=normalized,
                evidence=evidence,
                issues=issues,
                pending_fields=pending_fields,
            )
        if payload.get("kind") == "clarify":
            if set(payload) != {"kind", "issue"} or payload.get("issue") not in CLARIFY_ISSUES:
                raise ValueError("clarify shape")
            issue = normalize_clarify_issue(text, payload["issue"])
            return _draft(
                "needs_clarification",
                issues=[issue],
                terminal=issue == "MULTIPLE_EVENTS",
            )
        if payload.get("kind") == "refuse":
            if set(payload) != {"kind", "issue"} or payload.get("issue") != "OUT_OF_SCOPE":
                raise ValueError("refuse shape")
            return _draft("refused", issues=["OUT_OF_SCOPE"], terminal=True)
        raise ValueError("unknown action")
    except Exception:
        return _draft("error", issues=["PARSE_INVALID"], terminal=True)
