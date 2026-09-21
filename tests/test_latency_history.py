import json
from types import SimpleNamespace

from azure.monitor.query import LogsQueryStatus

from comparison.latency_history import fetch_latency_runs


def table(columns, rows):
    return SimpleNamespace(columns=columns, rows=rows)


def response(status=LogsQueryStatus.SUCCESS, tables=None, partial_data=None):
    return SimpleNamespace(status=status, tables=tables, partial_data=partial_data)


def fake_client(result):
    async def query_workspace(workspace_id, query, *, timespan, server_timeout):
        return result
    return SimpleNamespace(query_workspace=query_workspace)


async def test_returns_runs_oldest_first_with_only_known_agent_sides():
    rows = [
        ["11111111-1111-1111-1111-111111111111", json.dumps({"prompt": 900, "hosted": 2100, "unknown_side": 1})],
        ["22222222-2222-2222-2222-222222222222", {"prompt": 300}],
    ]
    client = fake_client(response(tables=[table(["comparisonId", "sides"], rows)]))
    runs = await fetch_latency_runs(client, "workspace-id")
    assert [run["comparison_id"] for run in runs] == [
        "22222222-2222-2222-2222-222222222222", "11111111-1111-1111-1111-111111111111",
    ]
    assert runs[1]["latencies"]["prompt"] == 900
    assert runs[1]["latencies"]["hosted"] == 2100
    assert runs[1]["latencies"]["aca"] is None
    assert "unknown_side" not in runs[1]["latencies"]
    assert runs[0]["latencies"]["prompt"] == 300
    assert runs[0]["latencies"]["hosted"] is None


async def test_discards_rows_with_invalid_comparison_id_or_malformed_sides():
    rows = [
        ["not-a-uuid", {"prompt": 900}],
        ["33333333-3333-3333-3333-333333333333", "not json"],
        ["44444444-4444-4444-4444-444444444444", {"prompt": -5, "hosted": "not-a-number", "aca": None}],
    ]
    client = fake_client(response(tables=[table(["comparisonId", "sides"], rows)]))
    runs = await fetch_latency_runs(client, "workspace-id")
    ids = [run["comparison_id"] for run in runs]
    assert "not-a-uuid" not in ids
    assert ids == ["44444444-4444-4444-4444-444444444444", "33333333-3333-3333-3333-333333333333"]
    negative_run = next(run for run in runs if run["comparison_id"] == "44444444-4444-4444-4444-444444444444")
    assert all(value is None for value in negative_run["latencies"].values())
    malformed_run = next(run for run in runs if run["comparison_id"] == "33333333-3333-3333-3333-333333333333")
    assert all(value is None for value in malformed_run["latencies"].values())


async def test_reads_partial_data_when_query_only_partially_succeeds():
    rows = [["55555555-5555-5555-5555-555555555555", {"prompt": 500}]]
    client = fake_client(response(
        status=LogsQueryStatus.PARTIAL, tables=None, partial_data=[table(["comparisonId", "sides"], rows)],
    ))
    runs = await fetch_latency_runs(client, "workspace-id")
    assert runs == [{
        "comparison_id": "55555555-5555-5555-5555-555555555555",
        "latencies": {
            "prompt": 500, "hosted": None, "prompt_none": None,
            "hosted_none": None, "aca": None, "aca_none": None,
        },
    }]


async def test_empty_tables_produce_no_runs():
    client = fake_client(response(tables=[]))
    assert await fetch_latency_runs(client, "workspace-id") == []
