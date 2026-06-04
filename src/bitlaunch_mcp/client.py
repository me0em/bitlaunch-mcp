"""Async REST client for the BitLaunch API (https://app.bitlaunch.io/api).

API quirks (verified against the official Go client, gobitlaunch):
- Auth header is literally "Bearer: <token>" — colon included.
- All money amounts are integers in mUSD (1/1000 USD).
- Create-server image field serializes as "HostImageID" (capital H).
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

BASE_URL = "https://app.bitlaunch.io/api"
VULTR_HOST_ID = 1
DEFAULT_IMAGE_VERSION_ID = "2284"  # Ubuntu 24.04 LTS x64


class BitLaunchError(Exception):
    """Readable API error for surfacing to the agent."""


def musd_to_usd(musd: int) -> float:
    return round(musd / 1000, 3)


class BitLaunchClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer: {api_key}",
                "User-Agent": "bitlaunch-mcp/0.1",
            },
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, json: dict | None = None):
        resp = await self._http.request(method, path, json=json)
        if resp.status_code == 401:
            raise BitLaunchError(
                "BitLaunch returned 401 Unauthorized — check BITLAUNCH_API_KEY."
            )
        if resp.status_code != 200:
            raise BitLaunchError(
                f"BitLaunch API error {resp.status_code}: {resp.text}"
            )
        return resp.json() if resp.content else None

    async def get_account(self) -> dict:
        d = await self._request("GET", "/user")
        return {
            "email": d["email"],
            "balance_usd": musd_to_usd(d["balance"]),
            "cost_per_hour_usd": musd_to_usd(d["costPerHr"]),
            "servers_used": d["used"],
            "server_limit": d["limit"],
        }
