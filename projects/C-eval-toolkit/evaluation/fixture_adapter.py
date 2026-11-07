"""Deterministic fixture adapter used by the C-003 runner and CLI."""

from __future__ import annotations

import copy

_FIXTURE_ERROR = "fixture raised"


def fixture_invoke(case: dict) -> object:
    """Return a deep copy of the fixture response, or raise a fixed error.

    ``raise`` fixtures always produce ``RuntimeError("fixture raised")`` and
    never embed case input in the message.
    """
    fixture = case["fixture"]
    if fixture["kind"] == "raise":
        raise RuntimeError(_FIXTURE_ERROR)
    return copy.deepcopy(fixture["response"])
