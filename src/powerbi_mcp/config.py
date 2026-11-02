"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    # Authentication
    auth_mode: str = "service_principal"  # service_principal | device_code | access_token
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    access_token: str | None = None

    # Endpoints (override for sovereign clouds, e.g. GCC / China)
    authority_host: str = "https://login.microsoftonline.com"
    api_base: str = "https://api.powerbi.com/v1.0/myorg"
    scope: str = "https://analysis.windows.net/powerbi/api/.default"

    # Behaviour
    read_only: bool = False
    enable_admin_tools: bool = False
    default_workspace_id: str | None = None
    max_rows: int = 1000
    request_timeout: int = 120
    export_dir: Path = field(default_factory=lambda: Path.cwd() / "powerbi_exports")
    token_cache_path: Path = field(
        default_factory=lambda: Path.home() / ".powerbi-mcp" / "token_cache.json"
    )

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            auth_mode=os.environ.get("POWERBI_AUTH_MODE", "").strip().lower() or "",
            tenant_id=os.environ.get("POWERBI_TENANT_ID") or os.environ.get("AZURE_TENANT_ID"),
            client_id=os.environ.get("POWERBI_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID"),
            client_secret=os.environ.get("POWERBI_CLIENT_SECRET")
            or os.environ.get("AZURE_CLIENT_SECRET"),
            access_token=os.environ.get("POWERBI_ACCESS_TOKEN"),
            authority_host=os.environ.get(
                "POWERBI_AUTHORITY_HOST", "https://login.microsoftonline.com"
            ).rstrip("/"),
            api_base=os.environ.get(
                "POWERBI_API_BASE", "https://api.powerbi.com/v1.0/myorg"
            ).rstrip("/"),
            scope=os.environ.get(
                "POWERBI_SCOPE", "https://analysis.windows.net/powerbi/api/.default"
            ),
            read_only=_bool("POWERBI_READ_ONLY"),
            enable_admin_tools=_bool("POWERBI_ENABLE_ADMIN_TOOLS"),
            default_workspace_id=os.environ.get("POWERBI_DEFAULT_WORKSPACE_ID") or None,
            max_rows=_int("POWERBI_MAX_ROWS", 1000),
            request_timeout=_int("POWERBI_REQUEST_TIMEOUT", 120),
        )
        if os.environ.get("POWERBI_EXPORT_DIR"):
            s.export_dir = Path(os.environ["POWERBI_EXPORT_DIR"]).expanduser()
        if os.environ.get("POWERBI_TOKEN_CACHE"):
            s.token_cache_path = Path(os.environ["POWERBI_TOKEN_CACHE"]).expanduser()

        # Infer auth mode when not explicitly set.
        if not s.auth_mode:
            if s.access_token:
                s.auth_mode = "access_token"
            elif s.client_secret:
                s.auth_mode = "service_principal"
            else:
                s.auth_mode = "device_code"
        return s

    @property
    def admin_api_base(self) -> str:
        # .../v1.0/myorg -> .../v1.0/myorg/admin
        return f"{self.api_base}/admin"
