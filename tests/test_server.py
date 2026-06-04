import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from bitlaunch_mcp import server
from bitlaunch_mcp.config import Config

OPTIONS = json.loads(
    (Path(__file__).parent / "fixtures" / "vultr_options.json").read_text()
)


class FakeBitLaunch:
    def __init__(self):
        self.account = {
            "email": "a@b.c", "balance_usd": 50.0, "cost_per_hour_usd": 0.0,
            "servers_used": 0, "server_limit": 5,
        }
        self.servers = []
        self.keys = []
        self.created_payloads = []
        self.destroyed = []

    async def get_account(self):
        return self.account

    async def get_create_options(self):
        return OPTIONS

    async def list_servers(self):
        return self.servers

    async def get_server(self, server_id):
        for s in self.servers:
            if s["id"] == server_id:
                return s
        raise AssertionError(f"unknown server {server_id}")

    async def create_server(self, **kw):
        self.created_payloads.append(kw)
        s = {
            "id": f"srv{len(self.servers) + 1}", "name": kw["name"],
            "ipv4": "1.2.3.4", "status": "ok", "error_text": "",
            "region": kw["region_id"], "size_id": kw["size_id"],
            "size": "", "image": "", "cost_per_hour_usd": 0.164,
            "uptime_hours": 0.0, "accrued_cost_usd": 0.0,
        }
        self.servers.append(s)
        return s

    async def destroy_server(self, server_id):
        self.destroyed.append(server_id)

    async def restart_server(self, server_id):
        pass

    async def list_ssh_keys(self):
        return self.keys

    async def create_ssh_key(self, name, content):
        key = {"id": "k1", "name": name, "content": content}
        self.keys.append(key)
        return key


@pytest.fixture
def fake(tmp_path):
    fake_client = FakeBitLaunch()
    server._state.clear()
    server._state["config"] = Config(
        api_key="tok",
        max_cost_per_hour=1.0,
        max_servers=2,
        ssh_key_path=tmp_path / "id_ed25519",
    )
    server._state["client"] = fake_client
    yield fake_client
    server._state.clear()


async def test_get_account_tool(fake):
    async with Client(server.mcp) as c:
        res = await c.call_tool("get_account", {})
    assert res.data["balance_usd"] == 50.0


async def test_list_gpu_plans_tool(fake):
    async with Client(server.mcp) as c:
        res = await c.call_tool("list_gpu_plans", {})
    plans = res.data
    assert [p["size_id"] for p in plans] == [
        "vcg-a40-1c-5g-2vram", "vcg-a40-24c-120g-48vram",
    ]


async def test_create_server_rejects_expensive_plan(fake):
    # full A40 is $3.721/hr > limit $1.0/hr
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="MAX_COST_PER_HOUR"):
            await c.call_tool("create_server", {
                "name": "big", "size_id": "vcg-a40-24c-120g-48vram",
                "region_id": "fra", "wait": False,
            })
    assert fake.created_payloads == []


async def test_create_server_rejects_over_server_limit(fake):
    fake.servers = [{"id": "s1"}, {"id": "s2"}]  # already at MAX_SERVERS=2
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="MAX_SERVERS"):
            await c.call_tool("create_server", {
                "name": "x", "size_id": "vcg-a40-1c-5g-2vram",
                "region_id": "fra", "wait": False,
            })


async def test_create_server_rejects_low_balance(fake):
    fake.account["balance_usd"] = 1.0  # < 24h * $0.164
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="[Bb]alance"):
            await c.call_tool("create_server", {
                "name": "x", "size_id": "vcg-a40-1c-5g-2vram",
                "region_id": "fra", "wait": False,
            })


async def test_create_server_rejects_unknown_size(fake):
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="size_id"):
            await c.call_tool("create_server", {
                "name": "x", "size_id": "nope", "region_id": "fra",
                "wait": False,
            })


async def test_create_gpu_server_happy_path(fake):
    async with Client(server.mcp) as c:
        res = await c.call_tool("create_server", {
            "name": "train-1", "size_id": "vcg-a40-1c-5g-2vram",
            "region_id": "fra", "wait": False,
        })
    assert res.data["id"] == "srv1"
    payload = fake.created_payloads[0]
    # ssh key auto-generated and registered
    assert fake.keys and fake.keys[0]["content"].startswith("ssh-ed25519 ")
    assert payload["ssh_key_ids"] == ["k1"]
    # GPU plan gets driver-installing init script
    assert "nvidia" in payload["init_script"].lower()
    assert payload["image_version_id"] == "2284"


async def test_create_standard_server_no_gpu_script(fake):
    async with Client(server.mcp) as c:
        await c.call_tool("create_server", {
            "name": "cpu-1", "size_id": "1gb-1vcpu",
            "region_id": "fra", "wait": False,
        })
    payload = fake.created_payloads[0]
    assert "nvidia" not in payload["init_script"].lower()
    assert "tmux" in payload["init_script"]  # base tooling still installed


async def test_destroy_and_list_tools(fake):
    async with Client(server.mcp) as c:
        await c.call_tool("create_server", {
            "name": "x", "size_id": "1gb-1vcpu", "region_id": "fra",
            "wait": False,
        })
        listed = await c.call_tool("list_servers", {})
        await c.call_tool("destroy_server", {"server_id": "srv1"})
    assert len(listed.data) == 1
    assert fake.destroyed == ["srv1"]


@pytest.fixture
def fake_with_server(fake):
    fake.servers.append({
        "id": "srv1", "name": "x", "ipv4": "9.9.9.9", "status": "ok",
        "error_text": "", "region": "fra", "size_id": "1gb-1vcpu",
        "size": "", "image": "", "cost_per_hour_usd": 0.009,
        "uptime_hours": 1.0, "accrued_cost_usd": 0.01,
    })
    return fake


async def test_run_command_tool_resolves_ip(fake_with_server, monkeypatch):
    calls = []

    async def fake_run(host, key_path, command, timeout_s=120):
        calls.append((host, command, timeout_s))
        return {"stdout": "ok", "stderr": "", "exit_code": 0, "timed_out": False}

    monkeypatch.setattr(server.ssh, "run_command", fake_run)
    async with Client(server.mcp) as c:
        res = await c.call_tool("run_command", {
            "server_id": "srv1", "command": "echo ok", "timeout_s": 5,
        })
    assert res.data["exit_code"] == 0
    assert calls == [("9.9.9.9", "echo ok", 5)]


async def test_run_command_tool_no_ip_yet(fake):
    fake.servers.append({
        "id": "srv1", "name": "x", "ipv4": "", "status": "creating",
        "error_text": "", "region": "fra", "size_id": "1gb-1vcpu",
        "size": "", "image": "", "cost_per_hour_usd": 0.009,
        "uptime_hours": 0.0, "accrued_cost_usd": 0.0,
    })
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="creating"):
            await c.call_tool("run_command", {
                "server_id": "srv1", "command": "echo ok",
            })


async def test_ssh_failure_wrapped_with_status(fake_with_server, monkeypatch):
    async def fake_run(host, key_path, command, timeout_s=120):
        raise OSError("connection refused")

    monkeypatch.setattr(server.ssh, "run_command", fake_run)
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="SSH .* failed.*status.*ok"):
            await c.call_tool("run_command", {
                "server_id": "srv1", "command": "echo ok",
            })


async def test_upload_requires_exactly_one_source(fake_with_server):
    async with Client(server.mcp) as c:
        with pytest.raises(ToolError, match="exactly one"):
            await c.call_tool("upload_file", {
                "server_id": "srv1", "remote_path": "/root/x",
            })


async def test_job_tools_wiring(fake_with_server, monkeypatch):
    started, queried = [], []

    async def fake_start(host, key_path, name, command, workdir=None):
        started.append((host, name, command, workdir))
        return {"job": name, "status": "running", "log": f"~/jobs/{name}.log"}

    async def fake_get(host, key_path, name, tail=100):
        queried.append((name, tail))
        return {"status": "running", "exit_code": None, "log_tail": "epoch 1\n"}

    monkeypatch.setattr(server.ssh, "start_job", fake_start)
    monkeypatch.setattr(server.ssh, "get_job", fake_get)
    async with Client(server.mcp) as c:
        await c.call_tool("start_job", {
            "server_id": "srv1", "name": "train1",
            "command": "python train.py", "workdir": "/root/proj",
        })
        res = await c.call_tool("get_job", {
            "server_id": "srv1", "name": "train1", "tail": 20,
        })
    assert started == [("9.9.9.9", "train1", "python train.py", "/root/proj")]
    assert queried == [("train1", 20)]
    assert res.data["status"] == "running"
