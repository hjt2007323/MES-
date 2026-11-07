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
                return message.get("content", "")
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
            if "2026-11-01" in question and "2026-11-02" in question:
                action = {
                    "kind": "query",
                    "query_id": "downtime_details",
                    "params": {
                        "window_start": "2026-11-01T00:00:00+08:00",
                        "window_end": "2026-11-02T00:00:00+08:00",
                    },
                }
            else:
                action = {
                    "kind": "clarify",
                    "message": "请明确停机的开始和结束时间窗（含时区）。",
                }
        elif "工单" in question and any(token in question for token in ("完成", "进度", "完成比")):
            match = re.search(r"line_id\s*=\s*(\d+)", question)
            if match:
                line_id = int(match.group(1))
            elif "产线1" in question or "L01" in question:
                line_id = 1
            else:
                action = {
                    "kind": "clarify",
                    "message": "请明确工单所属的产线 line_id（例如 line_id=1）。",
                }
                line_id = None
            if line_id is not None:
                action = {
                    "kind": "query",
                    "query_id": "work_order_progress",
                    "params": {"line_id": line_id},
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
