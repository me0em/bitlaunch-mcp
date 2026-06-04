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

    @staticmethod
    def _server_dict(s: dict) -> dict:
        created = datetime.fromisoformat(s["created"].replace("Z", "+00:00"))
        uptime_h = max(
            0.0, (datetime.now(timezone.utc) - created).total_seconds() / 3600
        )
        rate_usd = musd_to_usd(s.get("rate", 0))
        return {
            "id": s["id"],
            "name": s["name"],
            "ipv4": s.get("ipv4", ""),
            "status": s.get("status", ""),
            "error_text": s.get("errorText", ""),
            "region": s.get("region", ""),
            "size_id": s.get("size", ""),
            "size": s.get("sizeDescription", ""),
            "image": s.get("imageDescription", ""),
            "cost_per_hour_usd": rate_usd,
            "uptime_hours": round(uptime_h, 2),
            "accrued_cost_usd": round(rate_usd * uptime_h, 2),
        }

    async def list_servers(self) -> list[dict]:
        d = await self._request("GET", "/servers")
        return [self._server_dict(s) for s in (d or [])]

    async def get_server(self, server_id: str) -> dict:
        d = await self._request("GET", f"/servers/{server_id}")
        return self._server_dict(d["server"])

    async def create_server(
        self,
        *,
        name: str,
        size_id: str,
        region_id: str,
        image_version_id: str,
        ssh_key_ids: list[str],
        init_script: str = "",
    ) -> dict:
        payload = {
            "server": {
                "name": name,
                "hostID": VULTR_HOST_ID,
                "HostImageID": image_version_id,
                "sizeID": size_id,
                "regionID": region_id,
                "sshKeys": ssh_key_ids,
                "password": "",
                "initscript": init_script,
            }
        }
        d = await self._request("POST", "/servers", json=payload)
        return self._server_dict(d)

    async def destroy_server(self, server_id: str) -> None:
        await self._request("DELETE", f"/servers/{server_id}")

    async def restart_server(self, server_id: str) -> None:
        await self._request("POST", f"/servers/{server_id}/restart")
