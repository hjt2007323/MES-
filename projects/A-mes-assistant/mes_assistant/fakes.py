"""A-001 离线 mock 的脚本化时钟、模型与执行器。

仅用于工程验证：不连接真实模型、数据库或网络，不表示任何真实能力，也不
会产生真实 SQL、真实模型响应或业务指标。事件脚本按顺序消费；等待时间由
注入时钟模拟，不真实 sleep。
"""

import copy
from collections import deque

_MODEL_ERROR = "MODEL_FAILED"
_EXECUTOR_ERROR = "QUERY_FAILED"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _validated_events(events, payload_key, payload_type, error_code):
    """校验事件脚本结构并返回深拷贝，结构错误抛 ValueError。"""
    if not isinstance(events, list):
        raise ValueError("events 必须为列表")
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("事件必须为对象")
        if set(event) - {"elapsed_ms", payload_key, "error"}:
            raise ValueError("事件包含未定义字段")
        if "elapsed_ms" not in event:
            raise ValueError("事件缺少 elapsed_ms")
        elapsed_ms = event["elapsed_ms"]
        if not _is_int(elapsed_ms) or elapsed_ms < 0:
            raise ValueError("elapsed_ms 必须为非负整数")
        has_payload = payload_key in event
        has_error = "error" in event
        if has_payload == has_error:
            raise ValueError("事件必须且只能包含 %s 或 error 之一" % payload_key)
        if has_payload:
            if not isinstance(event[payload_key], payload_type):
                raise ValueError("%s 类型不符合事件协议" % payload_key)
        elif event["error"] != error_code:
            raise ValueError("未知错误码")
    return copy.deepcopy(events)


class FakeClock:
    """单调递增的确定性毫秒时钟；永不 sleep。"""

    def __init__(self, start_ms: int = 0) -> None:
        if not _is_int(start_ms) or start_ms < 0:
            raise ValueError("start_ms 必须为非负整数")
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, ms: int) -> None:
        if not _is_int(ms) or ms < 0:
            raise ValueError("advance 只接受非负整数")
        self._now_ms += ms


class FakeModel:
    """按脚本事件返回模型 payload 的确定性替身。"""

    def __init__(self, events: list[dict], clock: FakeClock) -> None:
        if not isinstance(clock, FakeClock):
            raise ValueError("clock 必须为 FakeClock")
        self._clock = clock
        self._events = deque(_validated_events(events, "payload", str, _MODEL_ERROR))
        self._calls = []

    @property
    def calls(self):
        """返回内部调用记录的深拷贝；不提供 setter。"""
        return copy.deepcopy(self._calls)

    def next_action(self, request: dict, history: list[dict], *, timeout_ms: int) -> str:
        if not _is_int(timeout_ms) or timeout_ms < 0:
            raise ValueError("timeout_ms 必须为非负整数")
        self._calls.append(
            {
                "request": copy.deepcopy(request),
                "history": copy.deepcopy(history),
                "timeout_ms": timeout_ms,
            }
        )
        if not self._events:
            raise RuntimeError("FAKE_SCRIPT_EXHAUSTED")
        event = self._events.popleft()
        elapsed_ms = event["elapsed_ms"]
        if elapsed_ms >= timeout_ms:
            self._clock.advance(timeout_ms)
            raise TimeoutError("fake model timeout")
        self._clock.advance(elapsed_ms)
        if "error" in event:
            raise RuntimeError(event["error"])
        return event["payload"]


class FakeExecutor:
    """按脚本事件返回合成表或抛错的确定性执行器替身。"""

    def __init__(self, events: list[dict], clock: FakeClock) -> None:
        if not isinstance(clock, FakeClock):
            raise ValueError("clock 必须为 FakeClock")
        self._clock = clock
        self._events = deque(_validated_events(events, "table", dict, _EXECUTOR_ERROR))
        self._calls = []

    @property
    def calls(self):
        """返回内部调用记录的深拷贝；不提供 setter。"""
        return copy.deepcopy(self._calls)

    def execute(self, sql: str, *, timeout_ms: int) -> dict:
        if not isinstance(sql, str):
            raise ValueError("sql 必须为字符串")
        if not _is_int(timeout_ms) or timeout_ms < 0:
            raise ValueError("timeout_ms 必须为非负整数")
        self._calls.append({"sql": sql, "timeout_ms": timeout_ms})
        if not self._events:
            raise RuntimeError("FAKE_SCRIPT_EXHAUSTED")
        event = self._events.popleft()
        elapsed_ms = event["elapsed_ms"]
        if elapsed_ms >= timeout_ms:
            self._clock.advance(timeout_ms)
            raise TimeoutError("fake executor timeout")
        self._clock.advance(elapsed_ms)
        if "error" in event:
            raise RuntimeError(event["error"])
        return copy.deepcopy(event["table"])
