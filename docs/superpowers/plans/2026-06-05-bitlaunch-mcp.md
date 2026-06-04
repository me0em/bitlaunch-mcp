# BitLaunch MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MCP-сервер, через который авторесёрч-агент арендует GPU-машины на BitLaunch (хост Vultr) и запускает на них обучение по SSH.

**Architecture:** Python-пакет `bitlaunch_mcp` (src-layout) из четырёх модулей: `config.py` (env-конфиг), `client.py` (async REST-клиент BitLaunch на httpx), `ssh.py` (stateless SSH-операции и tmux-джобы на asyncssh), `server.py` (FastMCP-приложение с 15 тулами и guardrails). Долгие задачи живут в tmux на удалённой машине; MCP-сервер состояния не хранит.

**Tech Stack:** Python ≥3.11, FastMCP 2.x, httpx, asyncssh; тесты — pytest, pytest-asyncio, respx.

**Spec:** `docs/superpowers/specs/2026-06-05-bitlaunch-mcp-design.md`

**Verified API facts** (см. спеку): base URL `https://app.bitlaunch.io/api`; заголовок `Authorization: Bearer: <token>` (именно с двоеточием); Vultr hostID=1; деньги в mUSD (÷1000 = $); Ubuntu 24.04 image version id `"2284"`; create-поле образа сериализуется как `"HostImageID"` (заглавная H — так в официальном Go-клиенте); `GET /servers` возвращает голый массив, `GET /servers/{id}` — `{"server": {...}}`, `GET /ssh-keys` — `{"keys": [...]}`.

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `src/bitlaunch_mcp/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "bitlaunch-mcp"
version = "0.1.0"
description = "MCP server for renting GPU machines via BitLaunch (Vultr host)"
requires-python = ">=3.11"
dependencies = [
    "fastmcp>=2.10",
    "httpx>=0.27",
    "asyncssh>=2.14",
]

[project.scripts]
bitlaunch-mcp = "bitlaunch_mcp.cli:main"

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
    "respx>=0.21",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create package skeleton**

```bash
mkdir -p src/bitlaunch_mcp tests/fixtures
touch src/bitlaunch_mcp/__init__.py tests/__init__.py
```

- [ ] **Step 3: Install and verify**

Run: `uv sync && uv run pytest`
Expected: deps install, pytest exits with "no tests ran" (exit code 5).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src tests uv.lock
git commit -m "chore: scaffold bitlaunch-mcp package"
```

---

### Task 2: config.py — env-конфиг с guardrail-лимитами

**Files:**
- Create: `src/bitlaunch_mcp/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config.py
import pytest
from pathlib import Path

from bitlaunch_mcp.config import Config, load_config


def test_defaults():
    cfg = load_config({"BITLAUNCH_API_KEY": "tok123"})
    assert cfg.api_key == "tok123"
    assert cfg.max_cost_per_hour == 1.0
    assert cfg.max_servers == 2
    assert cfg.ssh_key_path == Path("~/.bitlaunch-mcp/id_ed25519").expanduser()


def test_overrides():
    cfg = load_config({
        "BITLAUNCH_API_KEY": "tok123",
        "BITLAUNCH_MAX_COST_PER_HOUR": "3.5",
        "BITLAUNCH_MAX_SERVERS": "4",
        "BITLAUNCH_SSH_KEY_PATH": "/tmp/key",
    })
    assert cfg.max_cost_per_hour == 3.5
    assert cfg.max_servers == 4
    assert cfg.ssh_key_path == Path("/tmp/key")


def test_missing_api_key_raises():
    with pytest.raises(RuntimeError, match="BITLAUNCH_API_KEY"):
        load_config({})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitlaunch_mcp.config'`

- [ ] **Step 3: Implement config.py**

```python
# src/bitlaunch_mcp/config.py
"""Environment-based configuration with spending guardrails."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Config:
    api_key: str
    max_cost_per_hour: float  # USD; create_server refuses pricier plans
    max_servers: int          # max concurrent servers across the account
    ssh_key_path: Path        # local ed25519 private key (generated on demand)


def load_config(env: Mapping[str, str] | None = None) -> Config:
    if env is None:
        env = os.environ
    api_key = env.get("BITLAUNCH_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "BITLAUNCH_API_KEY is required. Get a token at "
            "https://app.bitlaunch.io/account/api and export it."
        )
    return Config(
        api_key=api_key,
        max_cost_per_hour=float(env.get("BITLAUNCH_MAX_COST_PER_HOUR", "1.0")),
        max_servers=int(env.get("BITLAUNCH_MAX_SERVERS", "2")),
        ssh_key_path=Path(
            env.get("BITLAUNCH_SSH_KEY_PATH", "~/.bitlaunch-mcp/id_ed25519")
        ).expanduser(),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/config.py tests/test_config.py
git commit -m "feat: env config with spending guardrail limits"
```

---

### Task 3: client.py — ядро REST-клиента (auth, ошибки, деньги)

**Files:**
- Create: `src/bitlaunch_mcp/client.py`
- Test: `tests/test_client.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_client.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitlaunch_mcp.client'`

- [ ] **Step 3: Implement client core**

```python
# src/bitlaunch_mcp/client.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_client.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/client.py tests/test_client.py
git commit -m "feat: BitLaunch REST client core with auth and error mapping"
```

---

### Task 4: client.py — серверы (list/get/create/destroy/restart, стоимость)

**Files:**
- Modify: `src/bitlaunch_mcp/client.py`
- Test: `tests/test_client.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_client.py -v`
Expected: new tests FAIL — `AttributeError: 'BitLaunchClient' object has no attribute 'list_servers'`

- [ ] **Step 3: Implement server methods**

Append to `BitLaunchClient` in `src/bitlaunch_mcp/client.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_client.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/client.py tests/test_client.py
git commit -m "feat: server CRUD with uptime and accrued cost"
```

---

### Task 5: client.py — планы и живая GPU-доступность по регионам

**Files:**
- Create: `tests/fixtures/vultr_options.json`
- Modify: `src/bitlaunch_mcp/client.py`
- Test: `tests/test_client.py` (append)

- [ ] **Step 1: Create fixture** (компактный слепок реальной структуры `/hosts-create-options/1`)

```json
{
  "hostID": 1,
  "available": true,
  "bandwidthCost": 30,
  "image": [
    {
      "id": 0,
      "name": "Ubuntu",
      "type": "image",
      "version": {"id": "2284", "description": "Ubuntu 24.04 LTS x64"},
      "versions": [
        {"id": "2284", "description": "Ubuntu 24.04 LTS x64"},
        {"id": "1743", "description": "Ubuntu 22.04 LTS x64"}
      ],
      "unavailableRegions": []
    }
  ],
  "region": [
    {
      "id": 9,
      "name": "Frankfurt",
      "iso": "de",
      "subregion": {
        "id": "fra",
        "description": "Frankfurt",
        "slug": "fra",
        "unavailableSizes": ["vcg-a40-24c-120g-48vram"]
      },
      "subregions": []
    },
    {
      "id": 25,
      "name": "Tokyo",
      "iso": "jp",
      "subregion": {
        "id": "nrt",
        "description": "Tokyo",
        "slug": "nrt",
        "unavailableSizes": [
          "vcg-a40-1c-5g-2vram",
          "vcg-a40-24c-120g-48vram"
        ]
      },
      "subregions": []
    }
  ],
  "size": [
    {
      "id": "1gb-1vcpu",
      "slug": "1gb-1vcpu",
      "bandwidthGB": 1000,
      "cpuCount": 1,
      "diskGB": 25,
      "memoryMB": 1024,
      "costPerHr": 9,
      "costPerMonth": 6,
      "planType": "standard"
    },
    {
      "id": "vcg-a40-1c-5g-2vram",
      "slug": "vcg-a40-1c-5g-2vram",
      "bandwidthGB": 3000,
      "cpuCount": 1,
      "diskGB": 90,
      "memoryMB": 5120,
      "freeText": "1/24 GPU 2GB RAM",
      "costPerHr": 164,
      "costPerMonth": 110,
      "planType": "gpu"
    },
    {
      "id": "vcg-a40-24c-120g-48vram",
      "slug": "vcg-a40-24c-120g-48vram",
      "bandwidthGB": 15000,
      "cpuCount": 24,
      "diskGB": 1400,
      "memoryMB": 122880,
      "freeText": "1 GPU 48GB RAM",
      "costPerHr": 3721,
      "costPerMonth": 2500,
      "planType": "gpu"
    }
  ],
  "planTypes": [
    {"type": "standard", "name": "Standard", "hardwareGroup": "cpu1"},
    {"type": "gpu", "name": "Nvidia A40 GPU", "hardwareGroup": "gpu1"}
  ],
  "hostOptions": {"rebuild": false, "resize": true, "userScript": true}
}
```

- [ ] **Step 2: Write failing tests**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_client.py -v`
Expected: new tests FAIL — no attribute `get_create_options` / `parse_plans`

- [ ] **Step 4: Implement**

Append to `BitLaunchClient`:

```python
    async def get_create_options(self) -> dict:
        return await self._request("GET", f"/hosts-create-options/{VULTR_HOST_ID}")

    @staticmethod
    def parse_plans(options: dict, plan_type: str | None = None) -> list[dict]:
        """Flatten sizes into plans with live per-region availability.

        A size is available in a region unless it appears in the region's
        subregion.unavailableSizes list.
        """
        regions = options.get("region") or []
        plans = []
        for s in options.get("size") or []:
            if plan_type and s.get("planType") != plan_type:
                continue
            available_regions = []
            for r in regions:
                sub = r.get("subregion") or {}
                if s["id"] not in (sub.get("unavailableSizes") or []):
                    available_regions.append(
                        {"name": r["name"], "region_id": sub["id"]}
                    )
            plans.append({
                "size_id": s["id"],
                "plan_type": s.get("planType", ""),
                "description": s.get("freeText", ""),
                "cpu_count": s.get("cpuCount", 0),
                "memory_mb": s.get("memoryMB", 0),
                "disk_gb": s.get("diskGB", 0),
                "cost_per_hour_usd": musd_to_usd(s.get("costPerHr", 0)),
                "cost_per_month_usd": s.get("costPerMonth", 0),
                "available_regions": available_regions,
            })
        return plans
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_client.py -v`
Expected: 12 passed

- [ ] **Step 6: Commit**

```bash
git add src/bitlaunch_mcp/client.py tests/test_client.py tests/fixtures/vultr_options.json
git commit -m "feat: plan listing with live per-region GPU availability"
```

---

### Task 6: client.py — SSH-ключи

**Files:**
- Modify: `src/bitlaunch_mcp/client.py`
- Test: `tests/test_client.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_client.py -v`
Expected: new tests FAIL — no attribute `list_ssh_keys`

- [ ] **Step 3: Implement**

Append to `BitLaunchClient`:

```python
    async def list_ssh_keys(self) -> list[dict]:
        d = await self._request("GET", "/ssh-keys")
        return (d or {}).get("keys") or []

    async def create_ssh_key(self, name: str, content: str) -> dict:
        return await self._request(
            "POST", "/ssh-keys", json={"name": name, "content": content}
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_client.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/client.py tests/test_client.py
git commit -m "feat: ssh key API methods"
```

---

### Task 7: ssh.py — локальный ключ и run_command

**Files:**
- Create: `src/bitlaunch_mcp/ssh.py`
- Test: `tests/test_ssh.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ssh.py
import stat

import pytest

from bitlaunch_mcp import ssh


def test_ensure_local_key_generates_once(tmp_path):
    key_path = tmp_path / "id_ed25519"
    pub1 = ssh.ensure_local_key(key_path)
    assert pub1.startswith("ssh-ed25519 ")
    assert key_path.exists()
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600
    # second call reuses, does not regenerate
    pub2 = ssh.ensure_local_key(key_path)
    assert pub2 == pub1


class FakeResult:
    def __init__(self, stdout="", stderr="", exit_status=0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class FakeConn:
    def __init__(self, result=None, exc=None):
        self.result = result or FakeResult()
        self.exc = exc
        self.commands = []
        self.closed = False

    async def run(self, command, timeout=None):
        self.commands.append(command)
        if self.exc:
            raise self.exc
        return self.result

    def close(self):
        self.closed = True


@pytest.fixture
def fake_conn(monkeypatch):
    holder = {"conn": FakeConn()}

    async def _connect(host, key_path):
        return holder["conn"]

    monkeypatch.setattr(ssh, "_connect", _connect)
    return holder


async def test_run_command_success(fake_conn, tmp_path):
    fake_conn["conn"] = FakeConn(FakeResult("out", "err", 0))
    res = await ssh.run_command("1.2.3.4", tmp_path / "k", "echo hi", timeout_s=5)
    assert res == {"stdout": "out", "stderr": "err", "exit_code": 0, "timed_out": False}
    assert fake_conn["conn"].commands == ["echo hi"]
    assert fake_conn["conn"].closed


async def test_run_command_timeout_returns_partial_output(fake_conn, tmp_path):
    import asyncssh
    exc = asyncssh.TimeoutError(
        env=None, command="x", subsystem=None, exit_status=None,
        exit_signal=None, returncode=None, stdout="partial", stderr="",
    )
    fake_conn["conn"] = FakeConn(exc=exc)
    res = await ssh.run_command("1.2.3.4", tmp_path / "k", "sleep 999", timeout_s=1)
    assert res["timed_out"] is True
    assert res["stdout"] == "partial"
    assert res["exit_code"] is None
    assert fake_conn["conn"].closed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ssh.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitlaunch_mcp.ssh'`

- [ ] **Step 3: Implement**

```python
# src/bitlaunch_mcp/ssh.py
"""Stateless SSH layer: every call opens a connection and closes it.

Long-running work lives in tmux sessions on the remote machine (see jobs
functions below), so nothing here survives—or needs to survive—a restart
of the MCP server.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import asyncssh

JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def ensure_local_key(key_path: Path) -> str:
    """Generate an ed25519 keypair at key_path if missing; return public key."""
    pub_path = key_path.with_suffix(".pub")
    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = asyncssh.generate_private_key("ssh-ed25519", comment="bitlaunch-mcp")
        key.write_private_key(key_path)
        key_path.chmod(0o600)
        key.write_public_key(pub_path)
    return pub_path.read_text().strip()


async def _connect(host: str, key_path: Path) -> asyncssh.SSHClientConnection:
    return await asyncssh.connect(
        host,
        username="root",
        client_keys=[str(key_path)],
        known_hosts=None,  # fresh VMs have unknown host keys by definition
        connect_timeout=15,
    )


async def run_command(
    host: str, key_path: Path, command: str, timeout_s: int = 120
) -> dict:
    conn = await _connect(host, key_path)
    try:
        try:
            result = await conn.run(command, timeout=timeout_s)
            return {
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "exit_code": result.exit_status,
                "timed_out": False,
            }
        except asyncssh.TimeoutError as e:
            return {
                "stdout": e.stdout or "",
                "stderr": e.stderr or "",
                "exit_code": None,
                "timed_out": True,
            }
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ssh.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/ssh.py tests/test_ssh.py
git commit -m "feat: ssh key generation and stateless run_command"
```

---

### Task 8: ssh.py — upload/download файлов

**Files:**
- Modify: `src/bitlaunch_mcp/ssh.py`
- Test: `tests/test_ssh.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ssh.py`:

```python
class FakeSFTPFile:
    def __init__(self, store, path):
        self.store, self.path = store, path

    async def write(self, data):
        self.store[self.path] = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSFTP:
    def __init__(self):
        self.files = {}
        self.puts = []
        self.gets = []

    def open(self, path, mode="r"):
        return FakeSFTPFile(self.files, path)

    async def put(self, local, remote):
        self.puts.append((local, remote))

    async def get(self, remote, local):
        self.gets.append((remote, local))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConnSFTP(FakeConn):
    def __init__(self):
        super().__init__()
        self.sftp = FakeSFTP()

    def start_sftp_client(self):
        return self.sftp


@pytest.fixture
def fake_sftp_conn(monkeypatch):
    conn = FakeConnSFTP()

    async def _connect(host, key_path):
        return conn

    monkeypatch.setattr(ssh, "_connect", _connect)
    return conn


async def test_upload_content(fake_sftp_conn, tmp_path):
    await ssh.upload("1.2.3.4", tmp_path / "k", "/root/train.py",
                     content="print('hi')")
    assert fake_sftp_conn.sftp.files == {"/root/train.py": "print('hi')"}


async def test_upload_local_file(fake_sftp_conn, tmp_path):
    local = tmp_path / "data.bin"
    local.write_bytes(b"x")
    await ssh.upload("1.2.3.4", tmp_path / "k", "/root/data.bin",
                     local_path=str(local))
    assert fake_sftp_conn.sftp.puts == [(str(local), "/root/data.bin")]


async def test_download(fake_sftp_conn, tmp_path):
    await ssh.download("1.2.3.4", tmp_path / "k", "/root/out.pt",
                       str(tmp_path / "out.pt"))
    assert fake_sftp_conn.sftp.gets == [("/root/out.pt", str(tmp_path / "out.pt"))]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ssh.py -v`
Expected: new tests FAIL — module 'bitlaunch_mcp.ssh' has no attribute 'upload'

- [ ] **Step 3: Implement**

Append to `src/bitlaunch_mcp/ssh.py`:

```python
async def upload(
    host: str,
    key_path: Path,
    remote_path: str,
    local_path: str | None = None,
    content: str | None = None,
) -> None:
    """Upload a local file OR inline text content to remote_path (absolute)."""
    conn = await _connect(host, key_path)
    try:
        async with conn.start_sftp_client() as sftp:
            if content is not None:
                async with sftp.open(remote_path, "w") as f:
                    await f.write(content)
            else:
                await sftp.put(local_path, remote_path)
    finally:
        conn.close()


async def download(
    host: str, key_path: Path, remote_path: str, local_path: str
) -> None:
    conn = await _connect(host, key_path)
    try:
        async with conn.start_sftp_client() as sftp:
            await sftp.get(remote_path, local_path)
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ssh.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/ssh.py tests/test_ssh.py
git commit -m "feat: sftp upload/download"
```

---

### Task 9: ssh.py — tmux-джобы

**Files:**
- Modify: `src/bitlaunch_mcp/ssh.py`
- Test: `tests/test_ssh.py` (append)

Дизайн: `start_job` создаёт detached tmux-сессию, весь вывод в `$HOME/jobs/<name>.log`, exit code в `$HOME/jobs/<name>.exit`. `get_job`: tmux-сессия жива → `running`; иначе exit-файл есть → `exited`; иначе `unknown` (упал до старта / имя неверное). Имена джобов валидируются регэкспом — они интерполируются в shell-команды.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_ssh.py`:

```python
def test_job_name_validation():
    with pytest.raises(ValueError, match="job name"):
        ssh.build_start_script("bad name; rm -rf /", "echo hi")
    with pytest.raises(ValueError, match="job name"):
        ssh.build_job_query("$(evil)", 10)


def test_build_start_script():
    script = ssh.build_start_script("train1", "python train.py", workdir="/root/proj")
    assert "mkdir -p $HOME/jobs" in script
    assert "rm -f $HOME/jobs/train1.exit" in script
    assert "tmux new-session -d -s train1 " in script
    # command wrapped: cd, redirect to log, exit code capture
    assert "cd /root/proj" in script
    assert "(python train.py)" in script
    assert "$HOME/jobs/train1.log" in script
    assert "echo $? >$HOME/jobs/train1.exit" in script


def test_parse_job_query_running():
    out = "RUNNING\n---LOG---\nepoch 1\nepoch 2\n"
    assert ssh.parse_job_query(out) == {
        "status": "running", "exit_code": None, "log_tail": "epoch 1\nepoch 2\n",
    }


def test_parse_job_query_exited():
    out = "EXITED 0\n---LOG---\ndone\n"
    assert ssh.parse_job_query(out) == {
        "status": "exited", "exit_code": 0, "log_tail": "done\n",
    }


def test_parse_job_query_unknown():
    out = "UNKNOWN\n---LOG---\n"
    parsed = ssh.parse_job_query(out)
    assert parsed["status"] == "unknown"
    assert parsed["exit_code"] is None


async def test_start_and_get_job_wiring(monkeypatch, tmp_path):
    sent = []

    async def fake_run(host, key_path, command, timeout_s=120):
        sent.append(command)
        return {"stdout": "RUNNING\n---LOG---\nhi\n", "stderr": "",
                "exit_code": 0, "timed_out": False}

    monkeypatch.setattr(ssh, "run_command", fake_run)
    await ssh.start_job("1.2.3.4", tmp_path / "k", "train1", "echo hi")
    res = await ssh.get_job("1.2.3.4", tmp_path / "k", "train1", tail=50)
    assert res["status"] == "running"
    assert "tmux new-session" in sent[0]
    assert "tail -n 50" in sent[1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ssh.py -v`
Expected: new tests FAIL — no attribute `build_start_script`

- [ ] **Step 3: Implement**

Append to `src/bitlaunch_mcp/ssh.py`:

```python
def _check_job_name(name: str) -> None:
    if not JOB_NAME_RE.match(name):
        raise ValueError(
            f"Invalid job name {name!r}: use only letters, digits, '-' and '_'."
        )


def build_start_script(name: str, command: str, workdir: str | None = None) -> str:
    _check_job_name(name)
    log = f"$HOME/jobs/{name}.log"
    exitf = f"$HOME/jobs/{name}.exit"
    body = f"({command})"
    if workdir:
        body = f"(cd {shlex.quote(workdir)} && {body})"
    wrapped = f"{body} >{log} 2>&1; echo $? >{exitf}"
    return (
        f"mkdir -p $HOME/jobs && rm -f {exitf} && "
        f"tmux new-session -d -s {name} {shlex.quote(wrapped)}"
    )


def build_job_query(name: str, tail: int) -> str:
    _check_job_name(name)
    return (
        f"if tmux has-session -t ={name} 2>/dev/null; then echo RUNNING; "
        f"elif [ -f $HOME/jobs/{name}.exit ]; then "
        f"echo EXITED $(cat $HOME/jobs/{name}.exit); "
        f"else echo UNKNOWN; fi; "
        f"echo ---LOG---; tail -n {int(tail)} $HOME/jobs/{name}.log 2>/dev/null"
    )


def parse_job_query(stdout: str) -> dict:
    header, _, log_tail = stdout.partition("---LOG---\n")
    parts = header.strip().split()
    status = parts[0].lower() if parts else "unknown"
    exit_code = None
    if status == "exited" and len(parts) > 1 and parts[1].lstrip("-").isdigit():
        exit_code = int(parts[1])
    return {"status": status, "exit_code": exit_code, "log_tail": log_tail}


async def start_job(
    host: str, key_path: Path, name: str, command: str,
    workdir: str | None = None,
) -> dict:
    script = build_start_script(name, command, workdir)
    res = await run_command(host, key_path, script, timeout_s=30)
    if res["exit_code"] != 0:
        raise RuntimeError(
            f"Failed to start job {name!r}: {res['stderr'] or res['stdout']}"
        )
    return {"job": name, "status": "running", "log": f"~/jobs/{name}.log"}


async def get_job(
    host: str, key_path: Path, name: str, tail: int = 100
) -> dict:
    res = await run_command(host, key_path, build_job_query(name, tail), timeout_s=30)
    return parse_job_query(res["stdout"])


async def stop_job(host: str, key_path: Path, name: str) -> dict:
    _check_job_name(name)
    await run_command(host, key_path, f"tmux kill-session -t ={name}", timeout_s=30)
    return {"job": name, "status": "stopped"}


async def list_jobs(host: str, key_path: Path) -> dict:
    script = (
        "tmux list-sessions -F '#S' 2>/dev/null; echo ---EXITED---; "
        "for f in $HOME/jobs/*.exit; do "
        '[ -f "$f" ] && echo "$(basename "$f" .exit) $(cat "$f")"; done'
    )
    res = await run_command(host, key_path, script, timeout_s=30)
    running_part, _, exited_part = res["stdout"].partition("---EXITED---\n")
    running = [line for line in running_part.splitlines() if line.strip()]
    exited = {}
    for line in exited_part.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            exited[parts[0]] = int(parts[1])
    # a finished job leaves an .exit file; one still running has a session
    return {
        "running": running,
        "exited": [
            {"job": n, "exit_code": c} for n, c in exited.items()
            if n not in running
        ],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ssh.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/ssh.py tests/test_ssh.py
git commit -m "feat: tmux-based remote jobs (start/status/stop/list)"
```

---

### Task 10: server.py — FastMCP-приложение, провиженинг-тулы и guardrails

**Files:**
- Create: `src/bitlaunch_mcp/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_server.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitlaunch_mcp.server'`

- [ ] **Step 3: Implement server.py (провиженинг-часть)**

```python
# src/bitlaunch_mcp/server.py
"""FastMCP application: provisioning + remote execution tools.

Dependency state lives in _state so tests can inject fakes.
"""
from __future__ import annotations

import asyncio

import asyncssh
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import ssh
from .client import DEFAULT_IMAGE_VERSION_ID, BitLaunchClient, BitLaunchError
from .config import Config, load_config

mcp = FastMCP("bitlaunch")

_state: dict = {}


def get_config() -> Config:
    if "config" not in _state:
        _state["config"] = load_config()
    return _state["config"]


def get_client() -> BitLaunchClient:
    if "client" not in _state:
        _state["client"] = BitLaunchClient(get_config().api_key)
    return _state["client"]


# Base tooling for every server; GPU plans additionally get a best-effort
# NVIDIA driver install (skipped when the image already ships drivers).
BASE_INIT_SCRIPT = """#!/bin/bash
set -x
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y tmux git curl rsync
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
"""

GPU_INIT_SCRIPT = BASE_INIT_SCRIPT + """
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi >/dev/null 2>&1; then
  apt-get install -y ubuntu-drivers-common
  DRIVER=$(ubuntu-drivers list --gpgpu 2>/dev/null \\
    | grep -o 'nvidia-driver-[0-9]*-server' | sort -V | tail -1)
  if [ -n "$DRIVER" ]; then
    apt-get install -y "$DRIVER" "${DRIVER/driver/utils}"
    reboot
  fi
fi
"""


def _is_gpu(size_id: str) -> bool:
    return size_id.startswith("vcg-")


@mcp.tool
async def get_account() -> dict:
    """Account balance (USD), current burn rate ($/hr) and server count/limit."""
    return await get_client().get_account()


@mcp.tool
async def list_gpu_plans() -> list[dict]:
    """GPU plans on Vultr with LIVE availability. A plan can only be created
    in regions listed in its available_regions; an empty list means the plan
    is out of stock everywhere right now."""
    options = await get_client().get_create_options()
    return BitLaunchClient.parse_plans(options, plan_type="gpu")


@mcp.tool
async def list_plans(plan_type: str | None = None) -> list[dict]:
    """All Vultr plans. plan_type: 'standard' | 'cpu' | 'gpu' | None (all).
    Cheap standard plans are useful for debugging the pipeline before
    renting a GPU."""
    options = await get_client().get_create_options()
    return BitLaunchClient.parse_plans(options, plan_type=plan_type)


async def _ensure_ssh_key(client: BitLaunchClient, cfg: Config) -> str:
    """Generate local ed25519 key if missing, register it on BitLaunch once."""
    pub = ssh.ensure_local_key(cfg.ssh_key_path)
    for key in await client.list_ssh_keys():
        if key.get("content", "").strip() == pub:
            return key["id"]
    created = await client.create_ssh_key("bitlaunch-mcp", pub)
    return created["id"]


async def _wait_ready(
    client: BitLaunchClient, cfg: Config, server_id: str, gpu: bool,
    timeout_s: int = 600,
) -> dict:
    """Poll until the server has an IP and SSH works (for GPU: nvidia-smi).
    On timeout returns the server with ready=False — never destroys it."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    delay = 5.0
    server = await client.get_server(server_id)
    while loop.time() < deadline:
        server = await client.get_server(server_id)
        if server["error_text"]:
            raise ToolError(
                f"Server {server_id} failed to provision: {server['error_text']}"
            )
        if server["ipv4"] and server["ipv4"] != "0.0.0.0":
            check = "nvidia-smi" if gpu else "echo ok"
            try:
                res = await ssh.run_command(
                    server["ipv4"], cfg.ssh_key_path, check, timeout_s=20
                )
                if res["exit_code"] == 0:
                    return {**server, "ready": True}
            except (OSError, asyncssh.Error):
                pass  # not booted yet / mid-reboot after driver install
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 30.0)
    return {
        **server,
        "ready": False,
        "note": (
            "Server created but not SSH-ready within timeout. It is still "
            "running (and billing). Check again with get_server/run_command, "
            "or destroy_server to stop paying."
        ),
    }


@mcp.tool
async def create_server(
    name: str,
    size_id: str,
    region_id: str,
    image_version_id: str = DEFAULT_IMAGE_VERSION_ID,
    wait: bool = True,
) -> dict:
    """Rent a Vultr server via BitLaunch. Billing starts immediately and
    continues until destroy_server is called.

    size_id/region_id come from list_gpu_plans/list_plans (use a region from
    the plan's available_regions). Default image is Ubuntu 24.04. GPU plans
    (vcg-*) get NVIDIA drivers installed automatically via init script; with
    wait=true the call returns only when SSH works (and nvidia-smi for GPU),
    up to 10 minutes."""
    cfg = get_config()
    client = get_client()

    plans = BitLaunchClient.parse_plans(await client.get_create_options())
    plan = next((p for p in plans if p["size_id"] == size_id), None)
    if plan is None:
        known = ", ".join(p["size_id"] for p in plans)
        raise ToolError(f"Unknown size_id {size_id!r}. Known plans: {known}")

    price = plan["cost_per_hour_usd"]
    if price > cfg.max_cost_per_hour:
        raise ToolError(
            f"Plan {size_id} costs ${price}/hr which exceeds the limit of "
            f"${cfg.max_cost_per_hour}/hr. Raise BITLAUNCH_MAX_COST_PER_HOUR "
            f"to allow it."
        )

    servers = await client.list_servers()
    if len(servers) >= cfg.max_servers:
        raise ToolError(
            f"Already running {len(servers)} servers — limit is "
            f"{cfg.max_servers} (BITLAUNCH_MAX_SERVERS). Destroy one first."
        )

    account = await client.get_account()
    if account["balance_usd"] < price * 24:
        raise ToolError(
            f"Balance ${account['balance_usd']} is less than 24h of "
            f"{size_id} (${round(price * 24, 2)}). Top up at "
            f"https://app.bitlaunch.io first."
        )

    key_id = await _ensure_ssh_key(client, cfg)
    gpu = _is_gpu(size_id)
    server = await client.create_server(
        name=name,
        size_id=size_id,
        region_id=region_id,
        image_version_id=image_version_id,
        ssh_key_ids=[key_id],
        init_script=GPU_INIT_SCRIPT if gpu else BASE_INIT_SCRIPT,
    )
    if wait:
        return await _wait_ready(client, cfg, server["id"], gpu)
    return server


@mcp.tool
async def get_server(server_id: str) -> dict:
    """Server status, IP, uptime and accrued cost in USD."""
    return await get_client().get_server(server_id)


@mcp.tool
async def list_servers() -> list[dict]:
    """All servers on the account with per-server accrued cost."""
    return await get_client().list_servers()


@mcp.tool
async def destroy_server(server_id: str) -> dict:
    """Permanently delete a server. This stops billing. Unsaved data is lost —
    download_file anything you need first."""
    await get_client().destroy_server(server_id)
    return {"destroyed": server_id}


@mcp.tool
async def restart_server(server_id: str) -> dict:
    """Reboot a server (running tmux jobs are killed)."""
    await get_client().restart_server(server_id)
    return {"restarted": server_id}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`
Expected: 9 passed

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: all tests pass (config 3, client 14, ssh 12, server 9)

- [ ] **Step 6: Commit**

```bash
git add src/bitlaunch_mcp/server.py tests/test_server.py
git commit -m "feat: MCP provisioning tools with spending guardrails"
```

---

### Task 11: server.py — execution-тулы (SSH поверх server_id)

**Files:**
- Modify: `src/bitlaunch_mcp/server.py`
- Test: `tests/test_server.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_server.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v`
Expected: new tests FAIL — `Unknown tool: run_command`

- [ ] **Step 3: Implement execution tools**

Append to `src/bitlaunch_mcp/server.py`:

```python
async def _resolve_host(server_id: str) -> str:
    server = await get_client().get_server(server_id)
    if not server["ipv4"] or server["ipv4"] == "0.0.0.0":
        raise ToolError(
            f"Server {server_id} has no IP yet (status: "
            f"{server['status'] or 'unknown'}). Wait and retry."
        )
    return server["ipv4"]


def _wrap_ssh_errors(server_id: str, status: str, exc: Exception) -> ToolError:
    return ToolError(
        f"SSH to server {server_id} failed: {exc}. Server status: {status}. "
        f"If it was just created or rebooted, wait a minute and retry."
    )


@mcp.tool
async def run_command(server_id: str, command: str, timeout_s: int = 120) -> dict:
    """Run a shell command on the server as root, wait for it to finish.
    For anything longer than a few minutes use start_job instead.
    Returns stdout, stderr, exit_code; on timeout timed_out=true with
    partial output."""
    host = await _resolve_host(server_id)
    try:
        return await ssh.run_command(
            host, get_config().ssh_key_path, command, timeout_s
        )
    except (OSError, asyncssh.Error) as e:
        srv = await get_client().get_server(server_id)
        raise _wrap_ssh_errors(server_id, srv["status"], e)


@mcp.tool
async def upload_file(
    server_id: str,
    remote_path: str,
    local_path: str | None = None,
    content: str | None = None,
) -> dict:
    """Upload to the server: either a local file (local_path) or inline text
    (content) — provide exactly one. remote_path must be absolute,
    e.g. /root/train.py."""
    if (local_path is None) == (content is None):
        raise ToolError("Provide exactly one of local_path or content.")
    host = await _resolve_host(server_id)
    try:
        await ssh.upload(
            host, get_config().ssh_key_path, remote_path,
            local_path=local_path, content=content,
        )
    except (OSError, asyncssh.Error) as e:
        srv = await get_client().get_server(server_id)
        raise _wrap_ssh_errors(server_id, srv["status"], e)
    return {"uploaded": remote_path}


@mcp.tool
async def download_file(server_id: str, remote_path: str, local_path: str) -> dict:
    """Download a file from the server to the local machine."""
    host = await _resolve_host(server_id)
    try:
        await ssh.download(
            host, get_config().ssh_key_path, remote_path, local_path
        )
    except (OSError, asyncssh.Error) as e:
        srv = await get_client().get_server(server_id)
        raise _wrap_ssh_errors(server_id, srv["status"], e)
    return {"downloaded": remote_path, "to": local_path}


@mcp.tool
async def start_job(
    server_id: str, name: str, command: str, workdir: str | None = None
) -> dict:
    """Start a long-running command (e.g. training) in a detached tmux
    session that survives SSH disconnects. Output goes to ~/jobs/<name>.log.
    Poll progress with get_job. Job names: letters, digits, '-', '_'."""
    host = await _resolve_host(server_id)
    try:
        return await ssh.start_job(
            host, get_config().ssh_key_path, name, command, workdir
        )
    except ValueError as e:
        raise ToolError(str(e))
    except (OSError, asyncssh.Error, RuntimeError) as e:
        srv = await get_client().get_server(server_id)
        raise _wrap_ssh_errors(server_id, srv["status"], e)


@mcp.tool
async def get_job(server_id: str, name: str, tail: int = 100) -> dict:
    """Job status: running | exited (with exit_code) | unknown, plus the
    last `tail` lines of its log."""
    host = await _resolve_host(server_id)
    try:
        return await ssh.get_job(host, get_config().ssh_key_path, name, tail)
    except ValueError as e:
        raise ToolError(str(e))
    except (OSError, asyncssh.Error) as e:
        srv = await get_client().get_server(server_id)
        raise _wrap_ssh_errors(server_id, srv["status"], e)


@mcp.tool
async def stop_job(server_id: str, name: str) -> dict:
    """Kill a running job's tmux session."""
    host = await _resolve_host(server_id)
    try:
        return await ssh.stop_job(host, get_config().ssh_key_path, name)
    except ValueError as e:
        raise ToolError(str(e))
    except (OSError, asyncssh.Error) as e:
        srv = await get_client().get_server(server_id)
        raise _wrap_ssh_errors(server_id, srv["status"], e)


@mcp.tool
async def list_jobs(server_id: str) -> dict:
    """All jobs on the server: running tmux sessions and exited jobs with
    their exit codes."""
    host = await _resolve_host(server_id)
    try:
        return await ssh.list_jobs(host, get_config().ssh_key_path)
    except (OSError, asyncssh.Error) as e:
        srv = await get_client().get_server(server_id)
        raise _wrap_ssh_errors(server_id, srv["status"], e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add src/bitlaunch_mcp/server.py tests/test_server.py
git commit -m "feat: remote execution tools (run/upload/download/jobs)"
```

---

### Task 12: cli.py — точка входа с выбором транспорта

**Files:**
- Create: `src/bitlaunch_mcp/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli.py
from bitlaunch_mcp.cli import build_parser


def test_default_transport_is_stdio():
    args = build_parser().parse_args([])
    assert args.transport == "stdio"


def test_http_transport_args():
    args = build_parser().parse_args(
        ["--transport", "http", "--host", "0.0.0.0", "--port", "9000"]
    )
    assert args.transport == "http"
    assert args.host == "0.0.0.0"
    assert args.port == 9000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bitlaunch_mcp.cli'`

- [ ] **Step 3: Implement**

```python
# src/bitlaunch_mcp/cli.py
"""Entry point: stdio for Claude Code/Desktop, http for Hermes/remote agents."""
import argparse

from .server import mcp


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bitlaunch-mcp",
        description="MCP server for renting GPU machines via BitLaunch/Vultr",
    )
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests and check the binary**

Run: `uv run pytest tests/test_cli.py -v && uv run bitlaunch-mcp --help`
Expected: 2 passed; help text prints with --transport/--host/--port.

- [ ] **Step 5: Smoke-check stdio startup**

Run: `BITLAUNCH_API_KEY=dummy timeout 3 uv run bitlaunch-mcp < /dev/null; echo "exit: $?"`
Expected: запускается без traceback (выход по EOF/timeout — это нормально).

- [ ] **Step 6: Commit**

```bash
git add src/bitlaunch_mcp/cli.py tests/test_cli.py
git commit -m "feat: cli entry point with stdio/http transports"
```

---

### Task 13: Live smoke-тест (opt-in, тратит центы)

**Files:**
- Create: `tests/test_live.py`

- [ ] **Step 1: Write the live test** (пропускается без `BITLAUNCH_LIVE_TEST=1`)

```python
# tests/test_live.py
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
```

- [ ] **Step 2: Verify it skips by default**

Run: `uv run pytest tests/test_live.py -v`
Expected: 1 skipped ("live test costs money")

- [ ] **Step 3: Commit**

```bash
git add tests/test_live.py
git commit -m "test: opt-in live smoke test (cheapest CPU server)"
```

---

### Task 14: README с конфигами подключения

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README**

````markdown
# bitlaunch-mcp

MCP server for renting (GPU) machines via [BitLaunch](https://bitlaunch.io)
(Vultr host) and running training jobs on them over SSH. Built for
auto-research agents: list GPU plans → create server → upload code →
start job → poll logs → download results → destroy.

## Requirements

- A BitLaunch account with balance and an API token
  (https://app.bitlaunch.io/account/api)
- `uv` (https://docs.astral.sh/uv/)

## Configuration (env)

| Variable | Default | Meaning |
|---|---|---|
| `BITLAUNCH_API_KEY` | — (required) | API token |
| `BITLAUNCH_MAX_COST_PER_HOUR` | `1.0` | refuse to create servers pricier than this ($/hr) |
| `BITLAUNCH_MAX_SERVERS` | `2` | max concurrent servers |
| `BITLAUNCH_SSH_KEY_PATH` | `~/.bitlaunch-mcp/id_ed25519` | auto-generated SSH key |

## Connect: Claude Code

```bash
claude mcp add bitlaunch -e BITLAUNCH_API_KEY=YOUR_TOKEN \
  -- uv run --project /path/to/bitlaunch bitlaunch-mcp
```

## Connect: Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "bitlaunch": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/bitlaunch", "bitlaunch-mcp"],
      "env": { "BITLAUNCH_API_KEY": "YOUR_TOKEN" }
    }
  }
}
```

## Connect: Hermes / remote agents (HTTP)

Run the server:

```bash
BITLAUNCH_API_KEY=YOUR_TOKEN uv run bitlaunch-mcp \
  --transport http --host 127.0.0.1 --port 8000
```

Point the client at `http://127.0.0.1:8000/mcp`.

> The HTTP transport has no auth of its own — keep it on localhost or
> behind a reverse proxy; anyone who can reach it can spend your balance.

## Typical agent flow

1. `list_gpu_plans` — pick a plan with non-empty `available_regions`
2. `create_server(name, size_id, region_id, wait=true)` — GPU plans get
   NVIDIA drivers installed automatically (adds a few minutes + reboot)
3. `upload_file(server_id, "/root/train.py", content=...)`
4. `start_job(server_id, "train", "uv run python /root/train.py")`
5. `get_job(server_id, "train")` — poll until `exited`
6. `download_file(server_id, "/root/model.pt", "./model.pt")`
7. `destroy_server(server_id)` — **billing stops only here**

## Tests

```bash
uv run pytest                                  # unit + integration (offline)
BITLAUNCH_LIVE_TEST=1 uv run pytest tests/test_live.py -v -s  # costs cents
```

## Notes / known limitations

- GPU plans are Nvidia A40 (full or fractional vGPU slices). Live region
  availability changes constantly — always check `available_regions`.
- Driver install on plain Ubuntu is best-effort: `create_server(wait=true)`
  for a GPU plan only reports `ready: true` after `nvidia-smi` succeeds.
  If it times out, inspect with `run_command(server_id, "nvidia-smi")`.
- Money units: BitLaunch API uses mUSD internally; all tool inputs/outputs
  are plain USD.
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README with client configs and agent flow"
```

---

### Task 15: Финальная верификация

- [ ] **Step 1: Full suite**

Run: `uv run pytest -v`
Expected: все тесты зелёные, 1 skipped (live).

- [ ] **Step 2: Tool inventory check**

Run:
```bash
BITLAUNCH_API_KEY=dummy uv run python -c "
import asyncio
from fastmcp import Client
from bitlaunch_mcp.server import mcp

async def main():
    async with Client(mcp) as c:
        tools = await c.list_tools()
        names = sorted(t.name for t in tools)
        print(len(names), names)

asyncio.run(main())
"
```
Expected: `15` тулов: create_server, destroy_server, download_file, get_account, get_job, get_server, list_gpu_plans, list_jobs, list_plans, list_servers, restart_server, run_command, start_job, stop_job, upload_file.

- [ ] **Step 3: Live smoke test** (по согласованию с пользователем — тратит центы)

Run: `set -a && source .env && set +a && BITLAUNCH_LIVE_TEST=1 uv run pytest tests/test_live.py -v -s`
Expected: PASS; сервер создан, команда выполнена, джоб отработал, сервер удалён.

- [ ] **Step 4: Commit any fixes, final commit**

```bash
git add -A && git commit -m "chore: final verification fixes" || echo "nothing to fix"
```
