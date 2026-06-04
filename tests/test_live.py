"""Live end-to-end test against the real BitLaunch API.

Opt-in: BITLAUNCH_LIVE_TEST=1 uv run pytest tests/test_live.py -v -s
Creates the cheapest standard server, runs a command, destroys it.
Cost: a few cents. Requires BITLAUNCH_API_KEY with balance.
"""
import os

import pytest

from bitlaunch_mcp import server
from bitlaunch_mcp.client import BitLaunchClient
from bitlaunch_mcp.config import load_config

pytestmark = pytest.mark.skipif(
    os.environ.get("BITLAUNCH_LIVE_TEST") != "1",
    reason="live test costs money; set BITLAUNCH_LIVE_TEST=1 to run",
)


async def test_full_cycle_on_cheapest_cpu_server():
    from fastmcp import Client

    server._state.clear()
    server._state["config"] = load_config()

    server_id = None
    try:
        async with Client(server.mcp) as c:
            plans = (await c.call_tool("list_plans", {"plan_type": "standard"})).data
            cheapest = min(
                (p for p in plans if p["available_regions"]),
                key=lambda p: p["cost_per_hour_usd"],
            )
            region = cheapest["available_regions"][0]["region_id"]
            print(f"\ncreating {cheapest['size_id']} in {region} "
                  f"(${cheapest['cost_per_hour_usd']}/hr)")

            created = (await c.call_tool("create_server", {
                "name": "mcp-live-test",
                "size_id": cheapest["size_id"],
                "region_id": region,
                "wait": True,
            })).data
            server_id = created["id"]
            assert created.get("ready"), f"server not ready: {created}"

            res = (await c.call_tool("run_command", {
                "server_id": server_id, "command": "echo live-ok && uname -a",
            })).data
            assert res["exit_code"] == 0
            assert "live-ok" in res["stdout"]

            job = (await c.call_tool("start_job", {
                "server_id": server_id, "name": "smoke",
                "command": "sleep 2 && echo job-done",
            })).data
            assert job["status"] == "running"

            import asyncio
            await asyncio.sleep(5)
            status = (await c.call_tool("get_job", {
                "server_id": server_id, "name": "smoke",
            })).data
            assert status["status"] == "exited"
            assert status["exit_code"] == 0
            assert "job-done" in status["log_tail"]
    finally:
        # always destroy to stop billing, even on assertion failure
        if server_id:
            cfg = load_config()
            client = BitLaunchClient(cfg.api_key)
            await client.destroy_server(server_id)
            await client.aclose()
            print(f"destroyed {server_id}")
        server._state.clear()
