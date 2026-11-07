"""Rule-only baseline for the frozen M5 intake cases.

The baseline deliberately supports only a complete explicit ``key=value``
template with the required machine/time/reason fields and an optional order.
Every other input is reported as ``abstain``; the baseline never guesses.
"""

from __future__ import annotations

FIELDS = ("machine_no", "start_time", "end_time", "reason", "order_id")
REQUIRED = ("machine_no", "start_time", "end_time", "reason")
MODE = "rule_template"
ABSTAIN_ISSUE = "ABSTAIN_UNSUPPORTED_TEMPLATE"


def _quote(text: str, key: str, value: str):
    quote = "%s=%s" % (key, value)
    if text.count(quote) != 1:
        return None
    start = text.index(quote)
    return {"quote": quote, "start": start, "end": start + len(quote)}


def parse_template(text: str):
    if not isinstance(text, str) or not text.strip():
        return None
    segments = [part.strip() for part in text.replace("；", ";").split(";") if part.strip()]
    if len(segments) < len(REQUIRED) or len(segments) > len(FIELDS):
        return None
    parsed = {}
    for segment in segments:
        if "=" not in segment:
            return None
        key, value = segment.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in FIELDS or key in parsed or not value:
            return None
        parsed[key] = value
    if any(key not in parsed for key in REQUIRED):
        return None
    if any(parsed.get(key) != parsed[key].strip() for key in parsed):
        return None
    evidence = {field: None for field in FIELDS}
    for key in REQUIRED + (("order_id",) if "order_id" in parsed else ()):
        quote = _quote(text, key, parsed[key])
        if quote is None:
            return None
        evidence[key] = quote
    record = {field: parsed.get(field) for field in FIELDS}
    return {"record": record, "evidence": evidence}


def baseline_draft(case: dict) -> dict:
    text = case.get("text")
    parsed = parse_template(text) if isinstance(text, str) else None
    if parsed is None:
        return {
            "draft_id": "rule-" + str(case.get("id")),
            "state": "abstain",
            "record": None,
            "evidence": None,
            "issues": [ABSTAIN_ISSUE],
            "mode": MODE,
        }
    return {
        "draft_id": "rule-" + str(case.get("id")),
        "state": "ready",
        "record": parsed["record"],
        "evidence": parsed["evidence"],
        "issues": [],
        "mode": MODE,
    }


def baseline_drafts(cases: list[dict]) -> list[dict]:
    return [baseline_draft(case) for case in cases]
