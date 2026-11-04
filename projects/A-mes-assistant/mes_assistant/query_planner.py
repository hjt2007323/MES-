"""Strict query planner for the M2 controlled data-QA loop (B-006).

The model may only produce one of three JSON actions.  This module builds the
prompt and parses/validates the action; it never executes SQL, touches the
database or runs tools.  Detailed calendar/range checks stay in A's public
``validate_query_input``.

Public API: ``build_messages``, ``parse_plan``, ``PlanError``.
"""

from __future__ import annotations

import json

from .query_catalog import list_queries as get_query_catalog
from .semantics import get_semantic_policy, select_context

__all__ = ["PlanError", "build_messages", "parse_plan"]

PLAN_INVALID = "PLAN_INVALID"
PLAN_FORBIDDEN = "PLAN_FORBIDDEN"

MAX_CONTENT_CHARS = 8192
MAX_MESSAGE_CHARS = 512
MAX_PROMPT_CHARS = 16_000
MAX_STRING_PARAM_CHARS = 64

_BUSINESS_QUERY_IDS = ("line_snapshot", "work_order_progress", "downtime_details")
_KINDS = ("query", "clarify", "refuse")
_CONTEXT_MODES = ("full", "focused")
_ALLOWED_FEEDBACK = frozenset({PLAN_INVALID})

_QUERY_EXAMPLES = {
    "line_snapshot": '{"kind":"query","query_id":"line_snapshot","params":{}}',
    "work_order_progress": '{"kind":"query","query_id":"work_order_progress",'
    '"params":{"line_id":1}}',
    "downtime_details": '{"kind":"query","query_id":"downtime_details","params":'
    '{"window_start":"2026-11-01T00:00:00+08:00",'
    '"window_end":"2026-11-02T00:00:00+08:00"}}',
}
_MESSAGE_EXAMPLES = (
    '{"kind":"clarify","message":"具体澄清问题"}',
    '{"kind":"refuse","message":"明确范围或安全原因"}',
)


class PlanError(ValueError):
    """Fixed-code plan failure; ``str`` never contains the model content."""

    def __init__(self, code: str, message: str):
        if code not in (PLAN_INVALID, PLAN_FORBIDDEN):
            raise ValueError("unknown PlanError code")
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.message)


def _fail(code: str, message: str):
    raise PlanError(code, message)


def _catalog_index() -> dict:
    index = {}
    for item in get_query_catalog():
        if item.get("review_status") != "pending":
            continue
        if item["query_id"] in _BUSINESS_QUERY_IDS:
            index[item["query_id"]] = item
    if set(index) != set(_BUSINESS_QUERY_IDS):
        raise RuntimeError("query catalog no longer contains the three business queries")
    return index


def _catalog_summary(index: dict, selected_ids) -> str:
    lines = []
    for query_id in selected_ids:
        item = index[query_id]
        params = item.get("params") or {}
        if params:
            param_text = "，".join(
                "%s:%s" % (name, spec.get("type", "?")) for name, spec in params.items()
            )
        else:
            param_text = "无"
        columns = "，".join(
            "%s:%s%s"
            % (
                column["name"],
                column["type"],
                "" if not column.get("nullable") else "(可空)",
            )
            for column in item.get("columns", [])
        )
        lines.append("- %s：参数 %s；列 %s" % (query_id, param_text, columns))
    return "\n".join(lines)


def _metric_summary(policy: dict, selected_ids) -> str:
    return "\n".join(
        "- %s：%s" % (query_id, policy["metric_summaries"][query_id])
        for query_id in selected_ids
    )


def _alias_summary(policy: dict) -> str:
    return "，".join("%s=%d" % (alias, value) for alias, value in policy["aliases"].items())


def _output_examples(selected_ids) -> str:
    examples = [_QUERY_EXAMPLES[query_id] for query_id in selected_ids]
    examples.extend(_MESSAGE_EXAMPLES)
    return "\n".join(examples)


def _request_context(request) -> dict:
    if not isinstance(request, dict):
        _fail(PLAN_INVALID, "request must be an object")
    question = request.get("question")
    if not isinstance(question, str) or not question.strip():
        _fail(PLAN_INVALID, "request.question must be a non-empty string")
    context = {}
    for name in ("reference_time", "timezone", "data_version"):
        value = request.get(name)
        if not isinstance(value, str) or not value.strip():
            _fail(PLAN_INVALID, "request.%s must be a non-empty string" % name)
        context[name] = value
    context["question"] = question
    return context


def build_messages(request: dict, *, previous_error: str = None) -> list:
    """Build the fixed two-message prompt for one planner call.

    ``request.context_mode`` is optional (default ``full``).  ``focused`` only
    trims the query descriptions/metric summaries/examples; the action shapes,
    all legal ids, global constraints, aliases, messages limits and correction
    code stay identical.
    """
    if previous_error is not None and previous_error not in _ALLOWED_FEEDBACK:
        _fail(PLAN_INVALID, "previous_error must be PLAN_INVALID when provided")
    context = _request_context(request)
    mode = request.get("context_mode", "full")
    if not isinstance(mode, str) or mode not in _CONTEXT_MODES:
        _fail(PLAN_INVALID, "context_mode must be full or focused")

    selection = select_context(request, mode=mode)
    policy = get_semantic_policy()
    selected = selection["selected_query_ids"]
    index = _catalog_index()

    feedback = ""
    if previous_error == PLAN_INVALID:
        feedback = (
            "\n\n上一次输出被判为 PLAN_INVALID。请只按上述三种 JSON 形态之一重新输出，"
            "不要解释，也不要输出任何额外字段。"
        )
    matched_text = "、".join(selection["matched_terms"]) or "无"

    system_content = (
        "你是受控 MES 数据问答的查询规划器。只输出一个 JSON 对象："
        "不要代码围栏、不要解释、不要前后缀，也不要输出任何 JSON 之外的文字。\n"
        "当前数据版本：%s\n固定参考时间：%s\n时区：%s\n"
        "语义口径版本：%s（registry_sha256=%s，review_status=%s）\n"
        "上下文模式：%s（reason=%s，命中词：%s）\n"
        "允许的 query_id（全部合法；上下文选择不限制正式 parser）：%s\n"
        "本次展开的查询描述：\n%s\n"
        "指标口径：\n%s\n"
        "只允许以下动作形态：\n%s\n"
        "约束：\n"
        "- 只能使用 query/clarify/refuse 三种 kind 与上述三个 query_id。\n"
        "- 禁止 sql 字段、额外字段、额外操作；参数键必须与查询定义精确一致。\n"
        "- line_id 必须是整数；时间参数必须是带 +08:00 的 ISO 日期时间字符串"
        "（详细日历与范围由程序复检）。\n"
        "- clarify/refuse 的 message 去空白后必须非空且不超过 512 字符。\n"
        "- 输出内容不得超过 8192 字符。\n"
        "产线别名：%s\n"
        "语义边界（已确认的演示口径）：\n%s\n"
        "数据是 synthetic，业务口径为 pending；synthetic/pending 始终可见，"
        "不得编造数据或企业收益。%s"
        % (
            context["data_version"],
            context["reference_time"],
            context["timezone"],
            policy["semantic_policy_version"],
            policy["registry_sha256"],
            policy["review_status"],
            mode,
            selection["reason"],
            matched_text,
            ", ".join(_BUSINESS_QUERY_IDS),
            _catalog_summary(index, selected),
            _metric_summary(policy, selected),
            _output_examples(selected),
            _alias_summary(policy),
            "\n".join(policy["rules"]),
            feedback,
        )
    )
    user_content = (
        "问题：%s\n参考时间：%s\n时区：%s\n数据版本：%s"
        % (
            context["question"],
            context["reference_time"],
            context["timezone"],
            context["data_version"],
        )
    )
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
    total = sum(len(message["content"]) for message in messages)
    if total > MAX_PROMPT_CHARS:
        _fail(PLAN_INVALID, "prompt exceeds the client message limit")
    return messages


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constants(value):
    raise ValueError("non-finite constant")


def _contains_sql_field(document) -> bool:
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "sql" in node:
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _validate_param_value(name: str, spec: dict, value) -> object:
    param_type = spec.get("type")
    if param_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            _fail(PLAN_INVALID, "parameter %s must be an integer" % name)
        return value
    if param_type == "string":
        if not isinstance(value, str):
            _fail(PLAN_INVALID, "parameter %s must be a string" % name)
        if not value or len(value) > MAX_STRING_PARAM_CHARS:
            _fail(PLAN_INVALID, "parameter %s has an invalid length" % name)
        return value
    _fail(PLAN_INVALID, "parameter %s has an unsupported type" % name)


def _parse_query(document: dict) -> dict:
    if set(document) != {"kind", "query_id", "params"}:
        if set(document) - {"kind", "query_id", "params"}:
            _fail(PLAN_FORBIDDEN, "query action contains extra fields")
        _fail(PLAN_INVALID, "query action is missing required fields")
    query_id = document["query_id"]
    if not isinstance(query_id, str) or query_id not in _BUSINESS_QUERY_IDS:
        _fail(PLAN_FORBIDDEN, "query_id is not one of the three business queries")
    params = document["params"]
    if not isinstance(params, dict):
        _fail(PLAN_INVALID, "params must be an object")

    index = _catalog_index()[query_id]
    expected = index.get("params") or {}
    extra = set(params) - set(expected)
    if extra:
        _fail(PLAN_FORBIDDEN, "params contains extra keys")
    missing = set(expected) - set(params)
    if missing:
        _fail(PLAN_INVALID, "params is missing required keys")

    normalized = {}
    for name, spec in expected.items():
        normalized[name] = _validate_param_value(name, spec, params[name])
    return {"kind": "query", "query_id": query_id, "params": normalized}


def _parse_message_action(document: dict, kind: str) -> dict:
    if set(document) != {"kind", "message"}:
        if set(document) - {"kind", "message"}:
            _fail(PLAN_FORBIDDEN, "%s action contains extra fields" % kind)
        _fail(PLAN_INVALID, "%s action is missing the message" % kind)
    message = document["message"]
    if not isinstance(message, str):
        _fail(PLAN_INVALID, "%s message must be a string" % kind)
    stripped = message.strip()
    if not stripped:
        _fail(PLAN_INVALID, "%s message must not be blank" % kind)
    if len(stripped) > MAX_MESSAGE_CHARS:
        _fail(PLAN_INVALID, "%s message is too long" % kind)
    return {"kind": kind, "message": stripped}


def parse_plan(content: str) -> dict:
    """Strictly parse one planner action; never executes tools or SQL."""
    if not isinstance(content, str):
        _fail(PLAN_INVALID, "content must be a string")
    if len(content) > MAX_CONTENT_CHARS:
        _fail(PLAN_INVALID, "content exceeds the plan length limit")
    try:
        document = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constants,
        )
    except (ValueError, TypeError, RecursionError):
        _fail(PLAN_INVALID, "content is not a strict JSON object")

    if not isinstance(document, dict):
        _fail(PLAN_INVALID, "plan must be a JSON object")
    if _contains_sql_field(document):
        _fail(PLAN_FORBIDDEN, "plan contains a sql field")

    kind = document.get("kind")
    if not isinstance(kind, str):
        _fail(PLAN_INVALID, "plan.kind must be a string")
    if kind not in _KINDS:
        _fail(PLAN_FORBIDDEN, "plan.kind is not an allowed action")

    if kind == "query":
        return _parse_query(document)
    return _parse_message_action(document, kind)
