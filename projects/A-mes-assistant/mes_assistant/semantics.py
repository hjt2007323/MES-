"""Versioned demo semantics and deterministic context selection (B-008).

This module is the single source for the three approved demo metrics, the seven
shared semantic boundaries, production-line aliases and the fixed selection
dictionary used by ``query_planner.build_messages``.  It is offline: no model,
HTTP or database access.

Registry hash algorithm: canonical UTF-8 JSON of ``semantic_policy_version``,
``review_status``, ``rules``, ``aliases`` and ``metric_summaries`` with sorted
keys and compact separators; SHA-256 of those bytes.  ``registry_sha256`` is not
part of its own hash input.
"""

from __future__ import annotations

import copy
import hashlib
import json

__all__ = [
    "SEMANTIC_POLICY_VERSION",
    "REVIEW_STATUS",
    "CATALOG_ORDER",
    "SELECTION_TERMS",
    "get_semantic_policy",
    "select_context",
]

SEMANTIC_POLICY_VERSION = "demo-metrics-v1-user-reviewed-20261103"
REVIEW_STATUS = "approved_for_demo"

CATALOG_ORDER = ("line_snapshot", "work_order_progress", "downtime_details")

METRIC_SUMMARIES = {
    "line_snapshot": (
        "按产线汇总工单当前累计实际量；包括所有产线、所有工单状态，"
        "无工单显示 0。单问“产量是多少”时澄清范围；明确的日产量或工序产量请求超出目录。"
    ),
    "work_order_progress": (
        "按一条产线列工单，完成比为实际量除以计划量；零计划返回 NULL 并展示为未知，"
        "超产比值保留，精度沿用数据库四位小数，原始状态原样展示。"
    ),
    "downtime_details": (
        "列出与窗口相交的原始停机事件；匹配 start < window_end AND "
        "(end IS NULL OR end > window_start)，按半开窗解释；保留原始 NULL、重叠及"
        "异常边界记录，无时长汇总。"
    ),
}

RULES = (
    "1. 数据始终标为 synthetic；使用快照标识 mes-a07-boundary-v1-pending，"
    "旧协议 review_status 与已确认演示口径分别显示，不得编造指标或企业收益。",
    "2. 产线产量只回答当前 work_order.actual_qty 快照；不得回答日产量或工序产出，"
    "单问“产量是多少”时澄清范围。",
    "3. 工单完成比 = actual_qty / NULLIF(plan_qty, 0)，不封顶，不等于工单终态；"
    "零计划为未知，超产比值保留。",
    "4. NULL 或零分母显示为未知；目录明确 COALESCE 的快照零值可如实展示。",
    "5. 停机只列与窗口相交的原始明细：不裁剪、不做并集、不算分钟；"
    "NULL、重叠与反向边界记录都保留。",
    "6. 工单查询必须有一条明确的 line_id 或已知产线别名，缺省或多产线都要澄清；"
    "未知别名要澄清。显式合法数值 ID 可直接查询并允许零行。",
    "7. 停机查询缺时间窗或窗口反向时澄清；相对日期只按固定 reference_time 解释，"
    "“昨天”是上海时区完整昨日窗口。库存、质检、写入、系统表、密钥等目录外请求拒绝。",
)

ALIASES = {
    "L01": 1,
    "L02": 2,
    "L03": 3,
    "1号产线": 1,
    "一号线": 1,
    "1号线": 1,
    "2号产线": 2,
    "二号线": 2,
    "2号线": 2,
    "3号产线": 3,
    "三号线": 3,
    "3号线": 3,
}

#: Fixed substring dictionary per query, in catalog order and term order.
SELECTION_TERMS = {
    "line_snapshot": ("产量", "产出", "累计", "快照", "actual_qty"),
    "work_order_progress": ("工单", "完成比", "计划量", "计划", "进度"),
    "downtime_details": ("停机", "故障", "时间窗"),
}

_REGISTRY = {
    "semantic_policy_version": SEMANTIC_POLICY_VERSION,
    "review_status": REVIEW_STATUS,
    "rules": list(RULES),
    "aliases": dict(ALIASES),
    "metric_summaries": {
        query_id: METRIC_SUMMARIES[query_id] for query_id in CATALOG_ORDER
    },
}


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


REGISTRY_SHA256 = hashlib.sha256(_canonical_bytes(_REGISTRY)).hexdigest()

_POLICY = {
    "semantic_policy_version": SEMANTIC_POLICY_VERSION,
    "registry_sha256": REGISTRY_SHA256,
    "review_status": REVIEW_STATUS,
    "rules": list(RULES),
    "aliases": dict(ALIASES),
    "metric_summaries": {
        query_id: METRIC_SUMMARIES[query_id] for query_id in CATALOG_ORDER
    },
}


def get_semantic_policy() -> dict:
    """Return a fresh deep copy of the fixed registry."""
    return copy.deepcopy(_POLICY)


def _normalized(text: str) -> str:
    return text.lower()


def _matched_by_query(question: str) -> dict:
    text = _normalized(question)
    matched = {}
    for query_id in CATALOG_ORDER:
        hits = [term for term in SELECTION_TERMS[query_id] if _normalized(term) in text]
        if hits:
            matched[query_id] = hits
    return matched


def select_context(request: dict, *, mode: str = "full") -> dict:
    """Select which query descriptions to show; selection never limits the parser."""
    if mode not in ("full", "focused"):
        raise ValueError("mode must be full or focused")

    policy_version = SEMANTIC_POLICY_VERSION
    registry_sha256 = REGISTRY_SHA256
    question = request.get("question") if isinstance(request, dict) else None
    if not isinstance(question, str):
        question = ""

    matched = _matched_by_query(question)
    matched_terms = []
    for query_id in CATALOG_ORDER:
        for term in matched.get(query_id, ()):
            if term not in matched_terms:
                matched_terms.append(term)

    if mode == "full":
        selected = list(CATALOG_ORDER)
        reason = "full"
    elif not matched:
        selected = list(CATALOG_ORDER)
        reason = "fallback_no_match"
    elif len(matched) >= len(CATALOG_ORDER):
        selected = list(CATALOG_ORDER)
        reason = "fallback_all_match"
    else:
        selected = [query_id for query_id in CATALOG_ORDER if query_id in matched]
        reason = "matched"

    return {
        "context_mode": mode,
        "selected_query_ids": selected,
        "matched_terms": matched_terms,
        "reason": reason,
        "semantic_policy_version": policy_version,
        "registry_sha256": registry_sha256,
    }
