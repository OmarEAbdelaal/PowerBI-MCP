import os

os.environ.update(
    {
        "POWERBI_AUTH_MODE": "access_token",
        "POWERBI_ACCESS_TOKEN": "test-token",
        "POWERBI_DEFAULT_WORKSPACE_ID": "",
        "POWERBI_EXPORT_DIR": "/tmp/pbi-mcp-test-exports",
    }
)

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

from powerbi_mcp import server  # noqa: E402
from powerbi_mcp.client import PowerBIError  # noqa: E402

API = "https://api.powerbi.com/v1.0/myorg"
WS = "ws-1"
DS = "ds-1"


@pytest.fixture(autouse=True)
def fresh_client():
    server._client = None
    yield
    server._client = None


@respx.mock
async def test_list_workspaces_paginates_and_sends_token():
    route = respx.get(f"{API}/groups").mock(
        side_effect=[
            httpx.Response(200, json={"value": [{"id": "a", "name": "A"}], "@odata.nextLink": f"{API}/groups?$skip=1"}),
            httpx.Response(200, json={"value": [{"id": "b", "name": "B"}]}),
        ]
    )
    result = await server.list_workspaces()
    assert [w["id"] for w in result] == ["a", "b"]
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token"


@respx.mock
async def test_workspace_path_vs_my_workspace():
    respx.get(f"{API}/groups/{WS}/datasets").mock(return_value=httpx.Response(200, json={"value": [{"id": DS, "name": "Sales"}]}))
    respx.get(f"{API}/datasets").mock(return_value=httpx.Response(200, json={"value": [{"id": "mine", "name": "Mine"}]}))
    assert (await server.list_datasets(WS))[0]["id"] == DS
    assert (await server.list_datasets())[0]["id"] == "mine"


@respx.mock
async def test_execute_dax_query_truncates_and_cleans_keys():
    rows = [{"Date[Year]": 2020 + i, "[Sales]": i * 10} for i in range(5)]
    route = respx.post(f"{API}/groups/{WS}/datasets/{DS}/executeQueries").mock(
        return_value=httpx.Response(200, json={"results": [{"tables": [{"rows": rows}]}]})
    )
    out = await server.execute_dax_query(DS, "EVALUATE x", WS, max_rows=3, impersonated_user="u@x.com")
    assert out["row_count"] == 5 and out["truncated"] is True and len(out["rows"]) == 3
    assert out["columns"] == ["Date[Year]", "Sales"]
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["queries"][0]["query"] == "EVALUATE x"
    assert body["impersonatedUserName"] == "u@x.com"


@respx.mock
async def test_dax_error_is_raised():
    respx.post(f"{API}/groups/{WS}/datasets/{DS}/executeQueries").mock(
        return_value=httpx.Response(400, json={"error": {"code": "DatasetExecuteQueriesError", "pbi.error": {"details": [{"code": "DetailsMessage", "detail": {"value": "Bad DAX"}}]}}})
    )
    with pytest.raises(PowerBIError, match="Bad DAX"):
        await server.execute_dax_query(DS, "EVALUATE nope", WS)


@respx.mock
async def test_get_dataset_schema():
    def handler(request):
        import json

        q = json.loads(request.content)["queries"][0]["query"]
        data = {
            "TABLES": [{"[Name]": "Sales", "[IsHidden]": False}, {"[Name]": "Hidden", "[IsHidden]": True}],
            "COLUMNS": [
                {"[Table]": "Sales", "[Name]": "Amount", "[DataType]": "Decimal", "[IsHidden]": False},
                {"[Table]": "Sales", "[Name]": "RowNumber-x", "[Type]": "RowNumber", "[IsHidden]": True},
            ],
            "MEASURES": [{"[Table]": "Sales", "[Name]": "Total", "[Expression]": "SUM(Sales[Amount])", "[IsHidden]": False}],
            "RELATIONSHIPS": [{"[FromTable]": "Sales", "[FromColumn]": "DateKey", "[ToTable]": "Date", "[ToColumn]": "DateKey", "[FromCardinality]": "Many", "[ToCardinality]": "One", "[IsActive]": True}],
        }
        key = q.split("INFO.VIEW.")[1].split("(")[0]
        return httpx.Response(200, json={"results": [{"tables": [{"rows": data[key]}]}]})

    respx.post(f"{API}/groups/{WS}/datasets/{DS}/executeQueries").mock(side_effect=handler)
    schema = await server.get_dataset_schema(DS, WS)
    assert list(schema["tables"]) == ["Sales"]
    sales = schema["tables"]["Sales"]
    assert sales["columns"] == [{"name": "Amount", "dataType": "Decimal"}]
    assert sales["measures"][0]["expression"] == "SUM(Sales[Amount])"
    assert schema["relationships"][0]["from"] == "'Sales'[DateKey]"


@respx.mock
async def test_retry_on_429(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("powerbi_mcp.client.asyncio.sleep", no_sleep)
    respx.get(f"{API}/groups/{WS}/reports").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"value": [{"id": "r1", "name": "R"}]}),
        ]
    )
    assert (await server.list_reports(WS))[0]["id"] == "r1"


@respx.mock
async def test_refresh_dataset_wait(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("powerbi_mcp.server.asyncio.sleep", no_sleep)
    path = f"{API}/groups/{WS}/datasets/{DS}/refreshes"
    respx.post(path).mock(return_value=httpx.Response(202, headers={"RequestId": "req-9"}))
    respx.get(path).mock(
        side_effect=[
            httpx.Response(200, json={"value": [{"status": "Unknown"}]}),
            httpx.Response(200, json={"value": [{"status": "Completed", "requestId": "req-9"}]}),
        ]
    )
    out = await server.refresh_dataset(DS, WS, wait_for_completion=True)
    assert out["requestId"] == "req-9" and out["status"] == "Completed"


@respx.mock
async def test_export_report(monkeypatch, tmp_path):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("powerbi_mcp.server.asyncio.sleep", no_sleep)
    monkeypatch.setattr(server.settings, "export_dir", tmp_path)
    base = f"{API}/groups/{WS}/reports/r1"
    respx.post(f"{base}/ExportTo").mock(return_value=httpx.Response(202, json={"id": "e1", "status": "NotStarted"}))
    respx.get(f"{base}/exports/e1").mock(
        return_value=httpx.Response(200, json={"id": "e1", "status": "Succeeded", "reportName": "Exec/Summary", "resourceFileExtension": ".pdf"})
    )
    respx.get(f"{base}/exports/e1/file").mock(return_value=httpx.Response(200, content=b"%PDF-1.7 data"))
    out = await server.export_report("r1", WS)
    assert out["status"] == "Succeeded"
    assert out["file"].endswith(".pdf") and "Exec_Summary" in out["file"]
    assert open(out["file"], "rb").read().startswith(b"%PDF")


@respx.mock
async def test_update_refresh_schedule_body():
    import json

    path = f"{API}/groups/{WS}/datasets/{DS}/refreshSchedule"
    patch = respx.patch(path).mock(return_value=httpx.Response(200))
    respx.get(path).mock(return_value=httpx.Response(200, json={"enabled": True, "times": ["07:00"]}))
    await server.update_refresh_schedule(DS, WS, enabled=True, times=["07:00"], local_time_zone_id="UTC")
    assert json.loads(patch.calls[0].request.content) == {"value": {"enabled": True, "times": ["07:00"], "localTimeZoneId": "UTC"}}


async def test_tool_registration_and_annotations():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert "execute_dax_query" in tools
    assert tools["execute_dax_query"].annotations.read_only_hint is True
    assert tools["delete_report"].annotations.destructive_hint is True
    assert "admin_list_workspaces" not in tools  # admin tools are opt-in
