"""
zoho_client.py — shared, self-contained Zoho client for the Metamorphosis bundle.

Why standalone (not importing the repo-root core/ package): app.py ships to Render as a
small bundle; keeping the client dependency-free of the setup toolkit makes deployment
clean and the safe-mode guarantees auditable in one file.

Responsibilities:
  - .COM base + token URL (data center proven .com by the live audit; NEVER .in)
  - OAuth2 refresh-token flow with in-memory caching (tokens live ~60 min)
  - Inject ?organization_id= on every Inventory / Books call
  - Retry with exponential backoff on 429 / 5xx ; respect Zoho's pagination
  - FAIL FAST on OAUTH_SCOPE_MISMATCH / invalid_grant / invalid_client — emit the
    token-regen runbook pointer and stop (a scope error is NOT empty data)

Env (see .env.example): ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN,
ZOHO_ORG_ID, ZOHO_ACCOUNTS_URL, ZOHO_API_BASE.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import tempfile
import time
from typing import Any

import httpx
import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger("zoho_client")

# --- configuration (env-only; never hardcode secrets) --------------------------
ACCOUNTS_URL = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com").rstrip("/")
API_BASE = os.getenv("ZOHO_API_BASE", "https://www.zohoapis.com").rstrip("/")
ORG_ID = os.getenv("ZOHO_ORG_ID", "906246204")

TOKEN_URL = f"{ACCOUNTS_URL}/oauth/v2/token"
BOOKS_BASE = f"{API_BASE}/books/v3"
CRM_BASE = f"{API_BASE}/crm/v6"
INVENTORY_BASE = f"{API_BASE}/inventory/v1"

_MAX_RETRIES = 4
_TOKEN_TTL = 3500  # refresh ~100s before Zoho's 3600s expiry

TOKEN_REGEN_RUNBOOK = (
    "TOKEN REGEN REQUIRED -> api-console.zoho.com (.com, NOT .in) -> Self Client -> "
    "Generate Code with scopes "
    "ZohoCRM.modules.ALL,ZohoCRM.settings.ALL,ZohoInventory.fullaccess.all,ZohoBooks.fullaccess.all "
    "-> exchange the code at https://accounts.zoho.com/oauth/v2/token (grant_type=authorization_code) "
    "-> put the new refresh_token in ZOHO_REFRESH_TOKEN. Never fabricate a token."
)


class ZohoError(Exception):
    """Generic Zoho API error. .payload holds the verbatim Zoho body when available."""

    def __init__(self, message: str, *, status_code: int | None = None, code: Any = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload


class ZohoAuthError(ZohoError):
    """Auth/scope failure that a retry cannot fix. Caller should stop and regen the token."""


class _RateLimiter:
    """Sliding-window limiter: at most `max_calls` in any rolling `period` seconds."""

    def __init__(self, max_calls: int = 90, period: float = 60.0) -> None:
        self.max_calls = max_calls
        self.period = period
        self._stamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._stamps = [t for t in self._stamps if t > now - self.period]
                if len(self._stamps) < self.max_calls:
                    self._stamps.append(now)
                    return
                wait = self._stamps[0] + self.period - now
            await asyncio.sleep(max(wait, 0.01))


class ZohoClient:
    """Async Zoho client for CRM (v6), Inventory (v1) and Books (v3) on the .com DC.

    Usage:
        async with ZohoClient() as z:
            orgs = await z.get(z.inventory("/organizations"))
    """

    def __init__(self) -> None:
        self._client_id = os.getenv("ZOHO_CLIENT_ID")
        self._client_secret = os.getenv("ZOHO_CLIENT_SECRET")
        self._refresh_token = os.getenv("ZOHO_REFRESH_TOKEN")
        self.org_id = ORG_ID
        self._access_token: str | None = None
        self._expiry: float = 0.0
        self._lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None
        self._limiter = _RateLimiter()

    # -- lifecycle --------------------------------------------------------------
    async def __aenter__(self) -> "ZohoClient":
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=60.0, write=60.0, pool=60.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- URL builders -----------------------------------------------------------
    @staticmethod
    def books(endpoint: str) -> str:
        return f"{BOOKS_BASE}{endpoint}"

    @staticmethod
    def crm(endpoint: str) -> str:
        return f"{CRM_BASE}{endpoint}"

    @staticmethod
    def inventory(endpoint: str) -> str:
        return f"{INVENTORY_BASE}{endpoint}"

    # -- auth -------------------------------------------------------------------
    def _cache_path(self) -> str:
        # Cross-process cache of the short-lived ACCESS token (not the refresh token),
        # keyed by a hash of the refresh token. Avoids hammering Zoho's refresh endpoint
        # (which throttles with "Access Denied" after a handful of refreshes per window).
        h = hashlib.sha256((self._refresh_token or "").encode()).hexdigest()[:16]
        return os.path.join(tempfile.gettempdir(), f"k24_zoho_token_{h}.json")

    def _load_cached_token(self) -> bool:
        try:
            with open(self._cache_path(), encoding="utf-8") as f:
                data = json.load(f)
            if data.get("access_token") and time.time() < float(data.get("expiry", 0)):
                self._access_token = data["access_token"]
                self._expiry = float(data["expiry"])
                return True
        except (OSError, ValueError):
            pass
        return False

    def _save_cached_token(self) -> None:
        try:
            with open(self._cache_path(), "w", encoding="utf-8") as f:
                json.dump({"access_token": self._access_token, "expiry": self._expiry}, f)
        except OSError:
            pass

    async def _token(self) -> str:
        if self._access_token and time.time() < self._expiry:
            return self._access_token
        async with self._lock:
            if self._access_token and time.time() < self._expiry:
                return self._access_token
            if self._load_cached_token():
                return self._access_token  # type: ignore[return-value]
            return await self._refresh()

    async def _refresh(self) -> str:
        if not (self._client_id and self._client_secret and self._refresh_token):
            raise ZohoAuthError(
                "Missing ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN in env. " + TOKEN_REGEN_RUNBOOK
            )
        assert self._http is not None, "ZohoClient must be used as an async context manager"
        log.info("zoho_token_refresh")
        resp = await self._http.post(
            TOKEN_URL,
            data={
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
            },
        )
        body = resp.json() if resp.content else {}
        if resp.status_code != 200 or "access_token" not in body:
            err = body.get("error") or body.get("message") or "unknown_error"
            # invalid_grant / invalid_client are terminal — stop, regen the token.
            log.error("zoho_token_refresh_failed", status=resp.status_code, error=err)
            raise ZohoAuthError(f"Token refresh failed ({err}). " + TOKEN_REGEN_RUNBOOK, status_code=resp.status_code, payload=body)
        self._access_token = body["access_token"]
        self._expiry = time.time() + _TOKEN_TTL
        self._save_cached_token()
        return self._access_token

    # -- core request -----------------------------------------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        with_org: bool | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request. Inventory/Books get organization_id auto-appended
        (override with with_org=True/False). Retries 429/5xx; fails fast on scope errors."""
        assert self._http is not None, "ZohoClient must be used as an async context manager"

        if with_org is None:
            with_org = ("/inventory/" in url) or ("/books/" in url)
        params = dict(params or {})
        if with_org:
            params.setdefault("organization_id", self.org_id)

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            await self._limiter.acquire()
            token = await self._token()
            req_headers = {"Authorization": f"Zoho-oauthtoken {token}"}
            if headers:
                req_headers.update(headers)
            try:
                resp = await self._http.request(method, url, headers=req_headers, params=params, json=json)
            except httpx.HTTPError as exc:
                last_exc = exc
                wait = (2**attempt) + random.uniform(0, 0.5)
                log.warning("zoho_network_error", error=str(exc), attempt=attempt, retry_in=round(wait, 2))
                await asyncio.sleep(wait)
                continue

            # scope mismatch (CRM) — terminal, do not retry, do not treat as empty data
            if resp.status_code == 401:
                body = self._safe_json(resp)
                if isinstance(body, dict) and body.get("code") == "OAUTH_SCOPE_MISMATCH":
                    log.error("zoho_scope_mismatch", url=url, body=body)
                    raise ZohoAuthError(
                        f"OAUTH_SCOPE_MISMATCH on {url}: {body.get('message')}. " + TOKEN_REGEN_RUNBOOK,
                        status_code=401,
                        payload=body,
                    )
                # otherwise: token may be stale — invalidate and retry
                self._access_token = None
                if attempt < _MAX_RETRIES - 1:
                    continue
                raise ZohoAuthError("401 Unauthorized after refresh. " + TOKEN_REGEN_RUNBOOK, status_code=401, payload=body)

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = (2**attempt) + random.uniform(0, 0.5)
                log.warning("zoho_retryable", status=resp.status_code, attempt=attempt, retry_in=round(wait, 2))
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(wait)
                    continue
                raise ZohoError(f"Zoho {resp.status_code} after {_MAX_RETRIES} retries on {url}", status_code=resp.status_code)

            return self._parse(resp, url)

        raise ZohoError(f"Request to {url} failed after {_MAX_RETRIES} retries", payload=str(last_exc))

    @staticmethod
    def _safe_json(resp: httpx.Response) -> Any:
        try:
            return resp.json() if resp.content else {}
        except ValueError:
            return {}

    def _parse(self, resp: httpx.Response, url: str) -> dict[str, Any]:
        body = self._safe_json(resp)
        if resp.status_code >= 400:
            # CRM nests per-record errors under "data"/"fields"; surface verbatim
            raise ZohoError(
                (body.get("message") if isinstance(body, dict) else None) or f"HTTP {resp.status_code}",
                status_code=resp.status_code,
                code=(body.get("code") if isinstance(body, dict) else None),
                payload=body,
            )
        # Books / Inventory use {"code":0,...}; non-zero = error
        if isinstance(body, dict) and body.get("code") not in (None, 0):
            raise ZohoError(body.get("message", "Zoho API error"), code=body.get("code"), payload=body, status_code=resp.status_code)
        return body if isinstance(body, dict) else {"data": body}

    # -- convenience verbs ------------------------------------------------------
    async def get(self, url: str, **kw: Any) -> dict[str, Any]:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, json: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return await self.request("POST", url, json=json, **kw)

    async def put(self, url: str, json: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return await self.request("PUT", url, json=json, **kw)

    async def patch(self, url: str, json: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return await self.request("PATCH", url, json=json, **kw)

    async def delete(self, url: str, **kw: Any) -> dict[str, Any]:
        return await self.request("DELETE", url, **kw)

    # -- pagination -------------------------------------------------------------
    async def paginate_inventory(self, endpoint: str, key: str, **params: Any) -> list[dict[str, Any]]:
        """Inventory/Books pagination via page_context.has_more_page (~0.5s between pages)."""
        page, per_page, out = 1, int(params.pop("per_page", 200)), []
        while True:
            resp = await self.get(self.inventory(endpoint), params={**params, "page": page, "per_page": per_page})
            out.extend(resp.get(key, []) or [])
            if not resp.get("page_context", {}).get("has_more_page"):
                break
            page += 1
            await asyncio.sleep(0.5)
        return out

    async def paginate_crm(self, module: str, **params: Any) -> list[dict[str, Any]]:
        """CRM pagination via info.more_records."""
        page, per_page, out = 1, int(params.pop("per_page", 200)), []
        while True:
            resp = await self.get(self.crm(f"/{module}"), params={**params, "page": page, "per_page": per_page}, with_org=False)
            out.extend(resp.get("data", []) or [])
            if not resp.get("info", {}).get("more_records"):
                break
            page += 1
            await asyncio.sleep(0.3)
        return out


async def validate_auth() -> dict[str, Any]:
    """One cheap GET to prove auth on .com. Returns the org dict. Raises ZohoAuthError on failure."""
    async with ZohoClient() as z:
        data = await z.get(z.inventory("/organizations"))
        orgs = data.get("organizations", [])
        match = next((o for o in orgs if str(o.get("organization_id")) == str(z.org_id)), orgs[0] if orgs else {})
        log.info(
            "auth_ok",
            org_id=match.get("organization_id"),
            name=match.get("name"),
            gstin=match.get("gst_no") or match.get("tax_reg_no") or "UNSET",
        )
        return match


if __name__ == "__main__":
    # `python zoho_client.py` -> validate auth and print the org (execution-order step 1)
    import json

    org = asyncio.run(validate_auth())
    print(json.dumps({k: org.get(k) for k in ("organization_id", "name", "gst_no", "tax_reg_no", "currency_code")}, indent=2))
