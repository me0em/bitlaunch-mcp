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


VALID_CRYPTO = ("BTC", "LTC", "ETH")


@mcp.tool
async def create_transaction(amount_usd: int, crypto_symbol: str) -> dict:
    """Create a crypto top-up invoice for the account balance. Nothing is
    charged automatically: the user must manually pay the returned
    invoice_url (or send amount_crypto to address; qr_code_url renders a
    scannable code). crypto_symbol: BTC | LTC | ETH. Status starts as
    Pending — track it with get_transaction. Balance updates only after
    the payment confirms."""
    cfg = get_config()
    symbol = crypto_symbol.upper()
    if symbol not in VALID_CRYPTO:
        raise ToolError(
            f"crypto_symbol must be one of: {', '.join(VALID_CRYPTO)}."
        )
    if amount_usd <= 0:
        raise ToolError("amount_usd must be positive.")
    if amount_usd > cfg.max_topup_usd:
        raise ToolError(
            f"Top-up of ${amount_usd} exceeds the limit of "
            f"${cfg.max_topup_usd:g} (BITLAUNCH_MAX_TOPUP_USD). Raise it "
            f"to allow larger invoices."
        )
    return await get_client().create_transaction(amount_usd, symbol)


@mcp.tool
async def list_transactions(page: int = 1, items: int = 25) -> dict:
    """Paginated top-up transaction history (newest first) with statuses
    and invoice links. Returns {transactions: [...], total}."""
    return await get_client().list_transactions(page=page, items=items)


@mcp.tool
async def get_transaction(transaction_id: str) -> dict:
    """Status of one top-up transaction (Pending -> Confirming -> Complete).
    Use after create_transaction to check whether the payment confirmed."""
    return await get_client().get_transaction(transaction_id)


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
    if account["balance_usd"] < price * cfg.min_balance_hours:
        raise ToolError(
            f"Balance ${account['balance_usd']} is less than "
            f"{cfg.min_balance_hours:g}h of {size_id} "
            f"(${round(price * cfg.min_balance_hours, 2)}). Top up at "
            f"https://app.bitlaunch.io first, or lower "
            f"BITLAUNCH_MIN_BALANCE_HOURS."
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
