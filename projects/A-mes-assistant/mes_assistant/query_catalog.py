"""A-004 M1 三场景命名查询目录与候选指标说明。

只读描述 B-004 v1.1 已冻结的三个命名查询，不包含 SQL、连接参数或业务
黄金答案；返回深拷贝，调用方修改不会影响内部目录。
"""

import copy

DATA_VERSION = "mes-a07-boundary-v1-pending"
DEFINITION_VERSION = "candidate-v1-pending"
REVIEW_STATUS = "pending"

_TABLES = ["line", "product", "work_order", "machine", "downtime"]
_QUERY_ORDER = ("line_snapshot", "work_order_progress", "downtime_details")

_QUERIES = {
    "line_snapshot": {
        "query_id": "line_snapshot",
        "title": "产线当前累计实际产量（候选快照）",
        "metric_id": "line_actual_qty_snapshot",
        "definition_version": DEFINITION_VERSION,
        "review_status": REVIEW_STATUS,
        "data_scope": (
            "mes-a07-boundary-v1-pending 的合成边界数据；只读 line/product/work_order/"
            "machine/downtime 五表，不含工序、质检或库存表。"
        ),
        "tables": list(_TABLES),
        "formula": (
            "按 line.id 分组，COALESCE(SUM(work_order.actual_qty), 0)；"
            "当前累计快照，不做日增量或工序累计。"
        ),
        "unit": "数量",
        "params": {},
        "parameter_notes": {},
        "columns": [
            {"name": "line_no", "type": "string", "nullable": False},
            {"name": "actual_qty", "type": "decimal", "nullable": False},
        ],
        "answer_note": "当前累计快照，不能回答某日产量或工序产出。",
        "limitations": [
            "只表示当前 work_order.actual_qty 的快照累计，不能回答某日产量或日增量。",
            "不把 production_record 或各工序 output_qty 相加，也不推断数据的日粒度。",
            "候选口径 review_status=pending；数据为合成演示数据，不是已审核业务答案。",
        ],
    },
    "work_order_progress": {
        "query_id": "work_order_progress",
        "title": "工单数量完成比（候选）",
        "metric_id": "work_order_quantity_completion",
        "definition_version": DEFINITION_VERSION,
        "review_status": REVIEW_STATUS,
        "data_scope": (
            "mes-a07-boundary-v1-pending 的合成边界数据；按产线读取 work_order 与 line，"
            "product 仅用于保留工单外键。"
        ),
        "tables": list(_TABLES),
        "formula": (
            "quantity_ratio = actual_qty / NULLIF(plan_qty, 0)；"
            "零分母为 NULL，不封顶，不按 status 过滤。"
        ),
        "unit": "比率",
        "params": {"line_id": {"type": "integer", "required": True}},
        "parameter_notes": {
            "line_id": "1..2147483647 的非 bool 整数；筛选 work_order.line_id。"
        },
        "columns": [
            {"name": "order_no", "type": "string", "nullable": False},
            {"name": "line_no", "type": "string", "nullable": False},
            {"name": "plan_qty", "type": "integer", "nullable": False},
            {"name": "actual_qty", "type": "integer", "nullable": False},
            {"name": "quantity_ratio", "type": "decimal", "nullable": True},
            {"name": "status", "type": "string", "nullable": False},
        ],
        "answer_note": (
            "数量完成比是候选口径；计划量为 0 时显示未知，不写成零值百分比；"
            "不封顶，也不代表工单终态。"
        ),
        "limitations": [
            "status 是原始 VARCHAR，不是已审核的终态集合；不能由 actual>=plan 推断完成。",
            "零计划量的比值保持 NULL，不能写成 0 或零值百分比；超产不截断。",
            "候选口径 review_status=pending；不输出正式业务正确率或黄金答案。",
        ],
    },
    "downtime_details": {
        "query_id": "downtime_details",
        "title": "查询窗内设备停机原始事件明细（候选）",
        "metric_id": "downtime_intersecting_events",
        "definition_version": DEFINITION_VERSION,
        "review_status": REVIEW_STATUS,
        "data_scope": (
            "mes-a07-boundary-v1-pending 的合成边界数据；读取 downtime 与 machine，"
            "order_id 是工单号字符串，不连接 work_order.id。"
        ),
        "tables": list(_TABLES),
        "formula": (
            "只返回与 [window_start, window_end) 相交的 downtime 原始事件；"
            "不裁剪端点，不计算区间并集或停机分钟。"
        ),
        "unit": "事件行",
        "params": {
            "window_start": {"type": "string", "required": True},
            "window_end": {"type": "string", "required": True},
        },
        "parameter_notes": {
            "window_start": (
                "精确 YYYY-MM-DDTHH:MM:SS+08:00，日历有效，且必须早于 window_end。"
            ),
            "window_end": (
                "精确 YYYY-MM-DDTHH:MM:SS+08:00，日历有效，且必须晚于 window_start。"
            ),
        },
        "columns": [
            {"name": "downtime_id", "type": "integer", "nullable": False},
            {"name": "machine_no", "type": "string", "nullable": False},
            {"name": "start_time", "type": "datetime", "nullable": False},
            {"name": "end_time", "type": "datetime", "nullable": True},
            {"name": "reason", "type": "string", "nullable": False},
            {"name": "order_id", "type": "string", "nullable": True},
        ],
        "answer_note": (
            "只列与窗口相交的原始事件；原始事件可能保留 NULL 结束、重叠、"
            "反向或零长度区间，需人工审核；A 不删行、不修正，"
            "未裁剪端点、未计算并集，不能当停机分钟或全线停机时间。"
        ),
        "limitations": [
            "end_time 为 NULL 表示结束未知/未结束；重叠事件作为多行保留，不计算区间并集。",
            "原始事件可能保留反向（结束早于开始）或零长度区间；A 原样返回供人工审核，不删行、不修正。",
            "事件条数不是停机分钟，也不是产线全停时长；order_id 不是外键 id。",
        ],
    },
}


def list_queries() -> list:
    """按固定顺序返回三个查询说明的深拷贝。"""
    return [copy.deepcopy(_QUERIES[query_id]) for query_id in _QUERY_ORDER]


def describe_query(query_id: str) -> dict:
    """返回单个查询说明的深拷贝；未知 ID 抛 KeyError。"""
    if not isinstance(query_id, str) or query_id not in _QUERIES:
        raise KeyError(query_id)
    return copy.deepcopy(_QUERIES[query_id])
