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
