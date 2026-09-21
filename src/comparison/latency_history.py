import json
import re
from contextlib import AsyncExitStack
from datetime import timedelta

from azure.monitor.query import LogsQueryStatus
from azure.monitor.query.aio import LogsQueryClient

from .agents import AGENT_SIDES
from .clients import default_credential

RUN_LIMIT = 14
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

# One row per comparison run, most recent first: a bag of {side: DurationMs}
# built from the "agent.<side>" dependency spans emitted by telemetry.py.
QUERY = f"""
AppDependencies
| where TimeGenerated > ago(7d)
| where Name startswith "agent."
| extend comparisonId = tostring(Properties["comparison.id"]), side = tostring(Properties["comparison.side"])
| where isnotempty(comparisonId) and isnotempty(side)
| summarize DurationMs = max(DurationMs), TimeGenerated = max(TimeGenerated) by comparisonId, side
| summarize latestTime = max(TimeGenerated), sides = make_bag(pack(side, DurationMs)) by comparisonId
| top {RUN_LIMIT} by latestTime desc
| project comparisonId, sides
"""


async def open_logs_query_client(stack: AsyncExitStack) -> LogsQueryClient:
    credential = default_credential()
    await stack.enter_async_context(credential)
    return await stack.enter_async_context(LogsQueryClient(credential))


def _comparison_id(value):
    return value if isinstance(value, str) and _UUID.fullmatch(value) else None


def _latencies(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, dict):
        raw = {}
    return {
        side: raw[side] if isinstance(raw.get(side), (int, float)) and raw[side] >= 0 else None
        for side in AGENT_SIDES
    }


async def fetch_latency_runs(client, workspace_id, *, timeout=10):
    """Last RUN_LIMIT comparison runs' per-side latency, oldest first (most recent last)."""
    response = await client.query_workspace(
        workspace_id, QUERY, timespan=timedelta(days=7), server_timeout=timeout,
    )
    tables = response.tables if response.status == LogsQueryStatus.SUCCESS else response.partial_data
    runs = []
    for table in tables or []:
        columns = list(table.columns)
        for row in table.rows:
            record = dict(zip(columns, row))
            comparison_id = _comparison_id(record.get("comparisonId"))
            if comparison_id is None:
                continue
            runs.append({"comparison_id": comparison_id, "latencies": _latencies(record.get("sides"))})
    runs.reverse()
    return runs
