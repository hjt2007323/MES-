"""Offline component fakes for A-007 mock mode/tests.

Only the model, tool and (via mes_assistant.fakes) clock are replaced.  The
planner is always B's real query_planner, including in mock mode.
"""

from __future__ import annotations

import json
import re

from mes_assistant import query_catalog
from mes_assistant.llm_client import ModelClientError, ModelReply

DATA_VERSION = "mes-a07-boundary-v1-pending"
FIXED_DOWNTIME_START = "2026-11-01T00:00:00+08:00"
FIXED_DOWNTIME_END = "2026-11-02T00:00:00+08:00"

_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_AMBIGUOUS_LINE = re.compile(
    r"(?:其他|其它|不同|各(?:个)?|多(?:条|个)?)产线|"
    r"产线\s*\d+\s*[、,，和及与]\s*(?:\d+|产线|L|line_id)|"
    r"\d+\s*号线\s*[、,，和及与]\s*\d+"
)


def _chinese_number(text):
    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char == "十":
            if current == 0:
                current = 1
            total += current * 10
            current = 0
        elif char == "百":
            if current == 0:
                current = 1
            total += current * 100
            current = 0
        else:
            return None
    return total + current


def _extract_line_refs(question):
    """Extract every complete line reference; no substring guessing."""
    refs = []
    for match in re.finditer(
        r"line_id\s*=\s*(\d+)(?![0-9A-Za-z_.])", question, re.IGNORECASE
    ):
        refs.append(int(match.group(1)))
    for match in re.finditer(
        r"(?<![0-9A-Za-z_])L(\d+)(?![0-9A-Za-z_])", question, re.IGNORECASE
    ):
        refs.append(int(match.group(1)))
    for match in re.finditer(r"产线\s*(\d+)(?![0-9A-Za-z_.])", question):
        refs.append(int(match.group(1)))
    for match in re.finditer(r"(\d+)\s*(?:号产线|号线)", question):
        refs.append(int(match.group(1)))
    unknown = False
    for match in re.finditer(r"([零一二三四五六七八九十百两]+)(?:号产线|号线)", question):
        value = _chinese_number(match.group(1))
        if value is None:
            unknown = True
        else:
            refs.append(value)
    return refs, unknown


class ScriptedModelClient:
    """Deterministic model replacement with actual-attempt counting."""

    def __init__(self, replies, *, clock=None, default_usage=None):
        if not isinstance(replies, list) or not replies:
            raise ValueError("replies must be a non-empty list")
        self._replies = replies
        self._clock = clock
        self._default_usage = (
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            if default_usage is None
            else default_usage
        )
        self._index = 0
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def complete(self, messages: list, *, timeout_ms: int) -> ModelReply:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise ModelClientError("MODEL_TIMEOUT", "no time remains")
        if self._index >= len(self._replies):
            raise ModelClientError("NETWORK_ERROR", "script exhausted")
        item = self._replies[self._index]
        self._index += 1
        self._attempts += 1
        if self._clock is not None:
            advance = item.get("advance_ms", 0) if isinstance(item, dict) else 0
            if advance:
                self._clock.advance(advance)
        if isinstance(item, str):
            content = item
            usage = self._default_usage
        else:
            content = item["content"]
            usage = item.get("usage", self._default_usage)
        if not isinstance(content, str):
            raise ModelClientError("OUTPUT_INVALID", "content is not a string")
        return ModelReply(content=content, usage=usage)


class KeywordMockModel:
    """Deterministic Chinese-question classifier for page/mock demos."""

    def __init__(self):
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    @staticmethod
    def _question(messages):
        for message in messages:
            if message.get("role") == "user" and "上一轮计划失败" not in message.get("content", ""):
                content = message.get("content", "")
                for line in content.splitlines():
                    if line.startswith("问题："):
                        return line[3:].strip()
                return content
        return ""

    def complete(self, messages: list, *, timeout_ms: int) -> ModelReply:
        self._attempts += 1
        question = self._question(messages)
        if any(token in question for token in ("库存", "删除", "写入", "修改", "质检")):
            action = {
                "kind": "refuse",
                "message": "该问题超出当前受控命名查询范围，不能执行写操作或编造目录外指标。",
            }
        elif "停机" in question:
            stamps = re.findall(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})",
                question,
            )
            if stamps == [FIXED_DOWNTIME_START, FIXED_DOWNTIME_END]:
                action = {
                    "kind": "query",
                    "query_id": "downtime_details",
                    "params": {
                        "window_start": FIXED_DOWNTIME_START,
                        "window_end": FIXED_DOWNTIME_END,
                    },
                }
            else:
                action = {
                    "kind": "clarify",
                    "message": (
                        "公开 mock 停机样例只接受恰好两个、按固定顺序的端点："
                        "2026-11-01T00:00:00+08:00 → 2026-11-02T00:00:00+08:00；"
                        "反向、缺失、额外端点或其他窗口需接入真实数据源。"
                    ),
                }
        elif "工单" in question and any(token in question for token in ("完成", "进度", "完成比")):
            refs, unknown = _extract_line_refs(question)
            ambiguous = bool(_AMBIGUOUS_LINE.search(question))
            if len(refs) > 1 or (refs and ambiguous) or unknown:
                action = {
                    "kind": "clarify",
                    "message": (
                        "公开 mock 工单样例只支持单个完整产线编号；检测到多个/"
                        "冲突或未识别的产线，请只给 line_id=1。"
                    ),
                }
            elif refs:
                if refs[0] == 1:
                    action = {
                        "kind": "query",
                        "query_id": "work_order_progress",
                        "params": {"line_id": 1},
                    }
                else:
                    action = {
                        "kind": "clarify",
                        "message": (
                            "公开 mock 工单样例仅覆盖 line_id=1；line_id=%d "
                            "需接入真实数据源。" % refs[0]
                        ),
                    }
            elif ambiguous:
                action = {
                    "kind": "clarify",
                    "message": (
                        "公开 mock 工单样例只支持单个完整产线编号；请只给一个 "
                        "line_id=1（或完整同义编号）。"
                    ),
                }
            else:
                action = {
                    "kind": "clarify",
                    "message": "请明确单个产线 line_id=1（或 L01/一号线等完整同义编号）。",
                }
        elif "产量" in question or "快照" in question:
            action = {"kind": "query", "query_id": "line_snapshot", "params": {}}
        else:
            action = {
                "kind": "clarify",
                "message": "请明确指标、产线和时间窗；当前只支持产量快照、工单完成比和停机明细。",
            }
        return ModelReply(
            content=json.dumps(action, ensure_ascii=False),
            usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
        )


def _downtime_rows():
    return [
        [1, "M01", "2026-10-31T23:50:00+08:00", "2026-11-01T00:20:00+08:00", "故障", "WO01"],
        [2, "M01", "2026-11-01T01:00:00+08:00", None, "保养", "WO01"],
        [3, "M02", "2026-11-01T10:00:00+08:00", "2026-11-01T09:30:00+08:00", "缺料", None],
        [4, "M02", "2026-11-01T12:00:00+08:00", "2026-11-01T12:00:00+08:00", "换型", None],
    ]


class MockTool:
    """B-shaped DTOs for offline query execution."""

    def __init__(self):
        self.calls = []

    def __call__(self, query_id, params, *, remaining_timeout_ms, max_rows=200):
        self.calls.append((query_id, dict(params)))
        description = query_catalog.describe_query(query_id)
        if query_id == "work_order_progress" and params.get("line_id") != 1:
            raise ValueError("public mock only supports line_id=1")
        if query_id == "downtime_details" and (
            params.get("window_start") != FIXED_DOWNTIME_START
            or params.get("window_end") != FIXED_DOWNTIME_END
        ):
            raise ValueError("public mock only supports the fixed downtime window")
        if query_id == "line_snapshot":
            rows = [["L01", "205"], ["L02", "80"], ["L03", "0"]]
        elif query_id == "work_order_progress":
            rows = [
                ["WO01", "L01", 100, 80, "0.8000", "生产中"],
                ["WO02", "L01", 0, 5, None, "生产中"],
                ["WO03", "L01", 100, 120, "1.2000", "已完成"],
            ]
        else:
            rows = _downtime_rows()
        return {
            "query_id": query_id,
            "data_version": DATA_VERSION,
            "status": "ok",
            "table": {
                "columns": description["columns"],
                "rows": rows,
                "truncated": False,
                "truncation_reason": None,
            },
            "sql": "SELECT /* offline mock */ 1;",
            "error": None,
            "counts": {"queries": 2},
            "timing": {"internal_ms": 0},
            "cleanup": [],
        }
