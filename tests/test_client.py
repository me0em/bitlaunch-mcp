import httpx
import pytest
import respx

from bitlaunch_mcp.client import BASE_URL, BitLaunchClient, BitLaunchError, musd_to_usd


def test_musd_to_usd():
    assert musd_to_usd(164) == 0.164
    assert musd_to_usd(20000) == 20.0
    assert musd_to_usd(0) == 0.0


@respx.mock
async def test_auth_header_format():
    """BitLaunch uses non-standard 'Bearer: <token>' (with colon)."""
    route = respx.get(f"{BASE_URL}/user").mock(
        return_value=httpx.Response(200, json={
            "email": "a@b.c", "balance": 20000, "costPerHr": 0,
            "used": 0, "limit": 5,
        })
    )
    client = BitLaunchClient("tok123")
    await client.get_account()
    assert route.calls.last.request.headers["Authorization"] == "Bearer: tok123"


@respx.mock
async def test_401_gives_actionable_error():
    respx.get(f"{BASE_URL}/user").mock(return_value=httpx.Response(401))
    client = BitLaunchClient("bad")
    with pytest.raises(BitLaunchError, match="BITLAUNCH_API_KEY"):
        await client.get_account()


@respx.mock
async def test_400_passes_body_through():
    respx.get(f"{BASE_URL}/user").mock(
        return_value=httpx.Response(400, text="insufficient balance")
    )
    client = BitLaunchClient("tok")
    with pytest.raises(BitLaunchError, match="insufficient balance"):
        await client.get_account()


@respx.mock
async def test_get_account_converts_money():
    respx.get(f"{BASE_URL}/user").mock(
        return_value=httpx.Response(200, json={
            "email": "a@b.c", "balance": 20000, "costPerHr": 164,
            "used": 1, "limit": 5,
        })
    )
    acc = await BitLaunchClient("tok").get_account()
    assert acc == {
        "email": "a@b.c",
        "balance_usd": 20.0,
        "cost_per_hour_usd": 0.164,
        "servers_used": 1,
        "server_limit": 5,
    }


from datetime import datetime, timedelta, timezone

SERVER_JSON = {
    "id": "abc123",
    "name": "train-1",
    "host": 1,
    "ipv4": "1.2.3.4",
    "region": "Frankfurt",
    "size": "vcg-a40-1c-5g-2vram",
    "sizeDescription": "1/24 GPU 2GB RAM",
    "image": "2284",
    "imageDescription": "Ubuntu 24.04 LTS x64",
    "created": (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(),
    "rate": 164,
    "status": "ok",
    "errorText": "",
    "diskGB": 90,
}


@respx.mock
async def test_list_servers_bare_array_and_accrued_cost():
    respx.get(f"{BASE_URL}/servers").mock(
        return_value=httpx.Response(200, json=[SERVER_JSON])
    )
    servers = await BitLaunchClient("tok").list_servers()
    assert len(servers) == 1
    s = servers[0]
    assert s["id"] == "abc123"
    assert s["ipv4"] == "1.2.3.4"
    assert s["cost_per_hour_usd"] == 0.164
    assert 9.9 <= s["uptime_hours"] <= 10.1
    assert 1.6 <= s["accrued_cost_usd"] <= 1.7  # ~10h * $0.164


@respx.mock
async def test_get_server_unwraps_envelope():
    respx.get(f"{BASE_URL}/servers/abc123").mock(
        return_value=httpx.Response(200, json={"server": SERVER_JSON})
    )
    s = await BitLaunchClient("tok").get_server("abc123")
    assert s["name"] == "train-1"
    assert s["status"] == "ok"


@respx.mock
async def test_create_server_payload_shape():
    route = respx.post(f"{BASE_URL}/servers").mock(
        return_value=httpx.Response(200, json=SERVER_JSON)
    )
    await BitLaunchClient("tok").create_server(
        name="train-1",
        size_id="vcg-a40-1c-5g-2vram",
        region_id="fra",
        image_version_id="2284",
        ssh_key_ids=["key1"],
        init_script="#!/bin/bash\necho hi",
    )
    import json as _json
    sent = _json.loads(route.calls.last.request.content)["server"]
    assert sent == {
        "name": "train-1",
        "hostID": 1,
        "HostImageID": "2284",  # capital H — exact API field name
        "sizeID": "vcg-a40-1c-5g-2vram",
        "regionID": "fra",
        "sshKeys": ["key1"],
        "password": "",
        "initscript": "#!/bin/bash\necho hi",
    }


@respx.mock
async def test_destroy_and_restart_paths():
    d = respx.delete(f"{BASE_URL}/servers/abc123").mock(
        return_value=httpx.Response(200)
    )
    r = respx.post(f"{BASE_URL}/servers/abc123/restart").mock(
        return_value=httpx.Response(200)
    )
    c = BitLaunchClient("tok")
    await c.destroy_server("abc123")
    await c.restart_server("abc123")
    assert d.called and r.called


import json
from pathlib import Path

OPTIONS = json.loads(
    (Path(__file__).parent / "fixtures" / "vultr_options.json").read_text()
)


@respx.mock
async def test_get_create_options_path():
    route = respx.get(f"{BASE_URL}/hosts-create-options/1").mock(
        return_value=httpx.Response(200, json=OPTIONS)
    )
    d = await BitLaunchClient("tok").get_create_options()
    assert route.called
    assert d["hostID"] == 1


def test_parse_plans_gpu_availability():
    plans = BitLaunchClient.parse_plans(OPTIONS, plan_type="gpu")
    assert [p["size_id"] for p in plans] == [
        "vcg-a40-1c-5g-2vram",
        "vcg-a40-24c-120g-48vram",
    ]
    small, big = plans
    # 2GB slice: blocked in Tokyo, available in Frankfurt
    assert small["available_regions"] == [{"name": "Frankfurt", "region_id": "fra"}]
    assert small["cost_per_hour_usd"] == 0.164
    assert small["description"] == "1/24 GPU 2GB RAM"
    # full A40: blocked everywhere right now
    assert big["available_regions"] == []


def test_parse_plans_all_types():
    plans = BitLaunchClient.parse_plans(OPTIONS)
    assert len(plans) == 3
    std = plans[0]
    assert std["plan_type"] == "standard"
    # standard plan available in both regions
    assert {r["region_id"] for r in std["available_regions"]} == {"fra", "nrt"}


@respx.mock
async def test_list_ssh_keys_unwraps_keys():
    respx.get(f"{BASE_URL}/ssh-keys").mock(
        return_value=httpx.Response(200, json={"keys": [
            {"id": "k1", "name": "bitlaunch-mcp", "content": "ssh-ed25519 AAA x"}
        ]})
    )
    keys = await BitLaunchClient("tok").list_ssh_keys()
    assert keys == [{"id": "k1", "name": "bitlaunch-mcp", "content": "ssh-ed25519 AAA x"}]


@respx.mock
async def test_create_ssh_key():
    route = respx.post(f"{BASE_URL}/ssh-keys").mock(
        return_value=httpx.Response(200, json={"id": "k2", "name": "bitlaunch-mcp"})
    )
    created = await BitLaunchClient("tok").create_ssh_key(
        "bitlaunch-mcp", "ssh-ed25519 BBB y"
    )
    import json as _json
    sent = _json.loads(route.calls.last.request.content)
    assert sent == {"name": "bitlaunch-mcp", "content": "ssh-ed25519 BBB y"}
    assert created["id"] == "k2"
