"""Async HTTP client for the Power BI REST API with retry/throttling handling."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from .auth import TokenProvider
from .config import Settings


class PowerBIError(RuntimeError):
    def __init__(self, status: int, message: str, details: Any = None):
        super().__init__(f"Power BI API error {status}: {message}")
        self.status = status
        self.details = details


def _extract_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:1000] or resp.reason_phrase
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or json.dumps(err)
        details = err.get("pbi.error", {}).get("details") if isinstance(err.get("pbi.error"), dict) else None
        if details:
            parts = []
            for d in details:
                detail = d.get("detail")
                val = detail.get("value") if isinstance(detail, dict) else detail
                parts.append(f"{d.get('code', '')}: {val}")
            msg += " | " + "; ".join(parts)
        return msg
    return json.dumps(body)[:1000]


class PowerBIClient:
    MAX_RETRIES = 4

    def __init__(self, settings: Settings, tokens: TokenProvider | None = None):
        self.s = settings
        self.tokens = tokens or TokenProvider(settings)
        self._http = httpx.AsyncClient(timeout=settings.request_timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------- helpers
    def group_path(self, workspace_id: str | None) -> str:
        """Path prefix for a workspace; empty string means 'My workspace'."""
        ws = workspace_id or self.s.default_workspace_id
        if ws and ws.lower() not in {"me", "my", "myworkspace", "my workspace"}:
            return f"/groups/{ws}"
        return ""

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.s.api_base}{path}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Any = None,
        raw: bool = False,
    ) -> Any:
        url = self._url(path)
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        attempt = 0
        while True:
            token = await self.tokens.get_token()
            headers = {"Authorization": f"Bearer {token}"}
            resp = await self._http.request(
                method, url, params=params, json=json_body, files=files, headers=headers
            )
            if resp.status_code in (429, 502, 503, 504) and attempt < self.MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 2 ** (attempt + 1)
                except ValueError:
                    delay = 2 ** (attempt + 1)
                await asyncio.sleep(min(delay, 60))
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise PowerBIError(resp.status_code, _extract_error(resp))
            if raw:
                return resp
            if not resp.content:
                return {"status": resp.status_code, "location": resp.headers.get("Location")}
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                return resp.json()
            return resp.text

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return await self.request("POST", path, json_body=body, **kw)

    async def patch(self, path: str, body: Any = None, **kw: Any) -> Any:
        return await self.request("PATCH", path, json_body=body, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    async def get_values(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """GET a collection, following @odata.nextLink / continuationUri pagination."""
        out: list[dict] = []
        data = await self.get(path, params=params)
        while True:
            if isinstance(data, dict):
                out.extend(data.get("value", data.get("activityEventEntities", [])) or [])
                nxt = data.get("@odata.nextLink") or (
                    data.get("continuationUri") if not data.get("lastResultSet", True) else None
                )
            else:
                nxt = None
            if not nxt:
                break
            data = await self.get(nxt)
        return out

    async def download(self, path: str, dest: Path) -> Path:
        resp = await self.request("GET", path, raw=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return dest
