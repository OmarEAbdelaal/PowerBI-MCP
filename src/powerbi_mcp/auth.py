"""Azure AD (Entra ID) token acquisition for the Power BI REST API.

Supported modes:
  * service_principal - client-credentials flow (app registration + secret).
  * device_code       - delegated user sign-in. Run `powerbi-mcp login` once;
                        the refresh token is cached and used silently afterwards.
  * access_token      - a pre-acquired bearer token (handy for quick testing).
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

import msal

from .config import Settings


class AuthError(RuntimeError):
    pass


class TokenProvider:
    def __init__(self, settings: Settings):
        self.s = settings
        self._lock = threading.Lock()
        self._cache = msal.SerializableTokenCache()
        self._app: msal.ClientApplication | None = None

    # ------------------------------------------------------------------ cache
    def _load_cache(self) -> None:
        p = self.s.token_cache_path
        if p.exists():
            self._cache.deserialize(p.read_text())

    def _save_cache(self) -> None:
        if not self._cache.has_state_changed:
            return
        p = self.s.token_cache_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self._cache.serialize())
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    # -------------------------------------------------------------- app setup
    @property
    def authority(self) -> str:
        tenant = self.s.tenant_id or "organizations"
        return f"{self.s.authority_host}/{tenant}"

    def _get_app(self) -> msal.ClientApplication:
        if self._app is not None:
            return self._app
        mode = self.s.auth_mode
        if mode == "service_principal":
            missing = [
                n
                for n, v in (
                    ("POWERBI_TENANT_ID", self.s.tenant_id),
                    ("POWERBI_CLIENT_ID", self.s.client_id),
                    ("POWERBI_CLIENT_SECRET", self.s.client_secret),
                )
                if not v
            ]
            if missing:
                raise AuthError(f"Service principal auth requires: {', '.join(missing)}")
            self._app = msal.ConfidentialClientApplication(
                client_id=self.s.client_id,
                client_credential=self.s.client_secret,
                authority=self.authority,
            )
        elif mode == "device_code":
            if not self.s.client_id:
                raise AuthError(
                    "Device-code auth requires POWERBI_CLIENT_ID (a public-client app "
                    "registration with Power BI Service delegated permissions)."
                )
            self._load_cache()
            self._app = msal.PublicClientApplication(
                client_id=self.s.client_id,
                authority=self.authority,
                token_cache=self._cache,
            )
        else:
            raise AuthError(f"Unsupported POWERBI_AUTH_MODE '{mode}'")
        return self._app

    # ------------------------------------------------------------------ token
    def get_token_sync(self) -> str:
        if self.s.auth_mode == "access_token":
            if not self.s.access_token:
                raise AuthError("POWERBI_ACCESS_TOKEN is empty")
            return self.s.access_token

        with self._lock:
            app = self._get_app()
            scopes = [self.s.scope]
            if isinstance(app, msal.ConfidentialClientApplication):
                result = app.acquire_token_for_client(scopes=scopes)
            else:
                accounts = app.get_accounts()
                result = (
                    app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
                )
                self._save_cache()
                if not result:
                    raise AuthError(
                        "No cached Power BI sign-in found. Run `powerbi-mcp login` in a "
                        "terminal (with the same environment variables) and retry."
                    )
            if "access_token" not in result:
                raise AuthError(
                    f"Token acquisition failed: {result.get('error')}: "
                    f"{result.get('error_description')}"
                )
            return result["access_token"]

    async def get_token(self) -> str:
        return await asyncio.to_thread(self.get_token_sync)

    # ---------------------------------------------------------- interactive
    def interactive_login(self) -> None:
        """Run the device-code flow and persist the token cache (CLI only)."""
        if self.s.auth_mode != "device_code":
            print(
                f"POWERBI_AUTH_MODE is '{self.s.auth_mode}'; login is only needed for "
                "device_code mode. Testing token acquisition instead...",
                file=sys.stderr,
            )
            self.get_token_sync()
            print("Token acquired successfully.", file=sys.stderr)
            return
        app = self._get_app()
        assert isinstance(app, msal.PublicClientApplication)
        flow = app.initiate_device_flow(scopes=[self.s.scope])
        if "user_code" not in flow:
            raise AuthError(f"Could not start device flow: {flow}")
        print(flow["message"], file=sys.stderr, flush=True)
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise AuthError(
                f"Login failed: {result.get('error')}: {result.get('error_description')}"
            )
        self._save_cache()
        user = (result.get("id_token_claims") or {}).get("preferred_username", "user")
        print(
            f"Signed in as {user}. Token cache saved to {self.s.token_cache_path}",
            file=sys.stderr,
        )

    def logout(self) -> None:
        p = self.s.token_cache_path
        if p.exists():
            p.unlink()
            print(f"Removed {p}", file=sys.stderr)
        else:
            print("No cached sign-in found.", file=sys.stderr)


