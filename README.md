# bitlaunch-mcp

**MCP server that lets AI agents rent GPU machines and run training on them.**

Wraps the [BitLaunch](https://bitlaunch.io) API (Vultr host) into 15 [Model Context Protocol](https://modelcontextprotocol.io) tools, so an auto-research agent — Claude Code, Hermes, or any MCP client — can do the full loop without a human in it:

```
list_gpu_plans → create_server → upload_file → start_job → get_job (poll) → download_file → destroy_server
```

- 🖥 **Provisioning** — list live GPU/CPU plan availability, create/destroy/restart servers, account balance & burn rate
- 🔑 **Zero-config SSH** — an ed25519 key is generated and registered with BitLaunch automatically on first use
- 🏃 **Remote execution** — run commands, upload/download files, launch long training runs in detached tmux sessions that survive disconnects
- 💸 **Spending guardrails** — hard limits on $/hr per server, concurrent server count, and minimum balance, enforced server-side before any money is spent
- 🔌 **Two transports** — stdio (Claude Code / Claude Desktop) and streamable HTTP (Hermes, remote agents)

GPU inventory is Nvidia A40 (full cards and fractional vGPU slices), from ~$0.16/hr for a 2 GB VRAM slice. BitLaunch is prepaid (crypto top-ups), so the worst-case blast radius is your balance.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- A BitLaunch account with balance and an API token: https://app.bitlaunch.io/account/api

## Setup

```bash
git clone https://github.com/me0em/bitlaunch-mcp.git
cd bitlaunch-mcp
uv sync
uv run pytest   # offline suite, no token needed
```

## Running with Claude Code

```bash
claude mcp add bitlaunch -e BITLAUNCH_API_KEY=YOUR_TOKEN \
  -- uv run --project /path/to/bitlaunch-mcp bitlaunch-mcp
```

Then just ask Claude:

> Rent the cheapest available GPU, fine-tune the model in ./train.py on it, fetch the checkpoint and destroy the machine.

<details>
<summary>Claude Desktop (claude_desktop_config.json)</summary>

```json
{
  "mcpServers": {
    "bitlaunch": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/bitlaunch-mcp", "bitlaunch-mcp"],
      "env": { "BITLAUNCH_API_KEY": "YOUR_TOKEN" }
    }
  }
}
```

</details>

## Running with Hermes (or any HTTP MCP client)

Start the server with the HTTP transport:

```bash
BITLAUNCH_API_KEY=YOUR_TOKEN uv run bitlaunch-mcp \
  --transport http --host 127.0.0.1 --port 8000
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`. Point your agent's MCP config at it; for clients that use the standard `mcpServers` config format:

```json
{
  "mcpServers": {
    "bitlaunch": { "url": "http://127.0.0.1:8000/mcp" }
  }
}
```

> ⚠️ The HTTP transport has **no auth of its own** — anyone who can reach the port can spend your balance. Keep it on localhost or behind a reverse proxy / VPN.

## Configuration

All configuration is via environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `BITLAUNCH_API_KEY` | — (required) | BitLaunch API token |
| `BITLAUNCH_MAX_COST_PER_HOUR` | `1.0` | guardrail: refuse to create servers pricier than this ($/hr) |
| `BITLAUNCH_MAX_SERVERS` | `2` | guardrail: max concurrent servers on the account |
| `BITLAUNCH_SSH_KEY_PATH` | `~/.bitlaunch-mcp/id_ed25519` | local SSH key (auto-generated if missing) |

---

# Documentation

## Tools: provisioning

### `get_account()`
Account overview: `balance_usd`, `cost_per_hour_usd` (current burn rate across all servers), `servers_used`, `server_limit`.

### `list_gpu_plans()`
GPU plans with **live availability**. Each plan: `size_id`, `description` (e.g. `"1/24 GPU 2GB RAM"`), `cpu_count`, `memory_mb`, `disk_gb`, `cost_per_hour_usd`, `cost_per_month_usd`, `available_regions` (list of `{name, region_id}`). A plan with an empty `available_regions` is out of stock everywhere right now — availability changes constantly, re-check before creating.

### `list_plans(plan_type?)`
All plans. `plan_type`: `"standard"` | `"cpu"` | `"gpu"` | omitted (all). Cheap standard plans (~$0.01/hr) are handy for debugging your pipeline before paying for a GPU.

### `create_server(name, size_id, region_id, image_version_id?, wait?)`
Rent a server. **Billing starts immediately and stops only at `destroy_server`.**

- `size_id` / `region_id` — from `list_gpu_plans`/`list_plans`; the region must be in the plan's `available_regions`
- `image_version_id` — default `"2284"` (Ubuntu 24.04 LTS)
- `wait` — default `true`: poll until the server is reachable over SSH (for GPU plans — until `nvidia-smi` works), up to 10 minutes. On timeout returns `ready: false` with a note; the server keeps running and billing
- Every server gets base tooling via cloud-init: tmux, git, curl, rsync, [uv](https://docs.astral.sh/uv/)
- GPU plans (`vcg-*`) additionally get a best-effort NVIDIA driver install (+reboot)

Guardrail failures (price cap, server cap, low balance, unknown size) return a descriptive error **before** anything is created.

### `get_server(server_id)` / `list_servers()`
Status, `ipv4`, region, plan, plus running-cost telemetry: `cost_per_hour_usd`, `uptime_hours`, `accrued_cost_usd` — so the agent (and you) can spot a forgotten machine.

### `destroy_server(server_id)`
Permanently delete the server and **stop billing**. Unsaved data is lost — `download_file` what you need first.

### `restart_server(server_id)`
Reboot. Running tmux jobs are killed.

## Tools: remote execution

All execution tools take a `server_id`, resolve its IP via the BitLaunch API, and connect over SSH as `root` with the auto-managed key. Connections are stateless — nothing breaks if the MCP server restarts mid-training.

### `run_command(server_id, command, timeout_s=120)`
Synchronous shell command. Returns `stdout`, `stderr`, `exit_code`, `timed_out`. On timeout you get the partial output and `timed_out: true`. For anything longer than a few minutes, use `start_job`.

### `upload_file(server_id, remote_path, local_path? | content?)`
Upload either a local file (`local_path`) or inline text (`content`) — exactly one. `remote_path` must be absolute, e.g. `/root/train.py`.

### `download_file(server_id, remote_path, local_path)`
Fetch results/checkpoints back to the local machine.

### `start_job(server_id, name, command, workdir?)`
Launch a long-running command in a detached tmux session. It survives SSH disconnects, MCP restarts, and your laptop sleeping. Output is captured to `~/jobs/<name>.log`, exit code to `~/jobs/<name>.exit`. Job names: letters, digits, `-`, `_`.

### `get_job(server_id, name, tail=100)`
Poll a job: `status` (`running` | `exited` | `unknown`), `exit_code`, and the last `tail` lines of the log.

### `stop_job(server_id, name)` / `list_jobs(server_id)`
Kill a job's tmux session; list running sessions and exited jobs with exit codes.

## Spending guardrails

`create_server` refuses when:

1. the plan costs more than `BITLAUNCH_MAX_COST_PER_HOUR`
2. the account already runs `BITLAUNCH_MAX_SERVERS` servers
3. the balance covers less than 24h of the requested plan

Every refusal message tells the agent which env variable to raise, so a human stays in the loop for limit changes.

## Money units

The BitLaunch API internally uses mUSD (1/1000 USD). All tool inputs and outputs are **plain USD** — the agent never sees mUSD.

## Tests

```bash
uv run pytest                                                  # offline: unit + in-memory MCP integration
BITLAUNCH_LIVE_TEST=1 uv run pytest tests/test_live.py -v -s   # live e2e: creates & destroys the cheapest CPU server (~$0.02/hr, runs ~2 min)
```

The live test always destroys the server in a `finally` block, even when assertions fail.

## Known limitations

- **Vultr only** (BitLaunch hostID 1). BitLaunch also proxies DigitalOcean and Linode — the client is parameterized by host ID, but only Vultr is tested and wired up.
- **GPU driver install is best-effort.** Vultr's fractional vGPU slices may require GRID drivers that plain Ubuntu images don't ship. `create_server(wait=true)` reports `ready: true` only after `nvidia-smi` succeeds; if it times out, diagnose with `run_command(server_id, "nvidia-smi")` or pick a different plan.
- **No persistent SSH sessions / interactive shells** — by design. Long work belongs in tmux jobs.
- Domains/DNS, DDoS protection, resize/rebuild, and crypto top-ups are out of scope; manage those at https://app.bitlaunch.io.

## License

[WTFPL](LICENSE) — do what the fuck you want to.
