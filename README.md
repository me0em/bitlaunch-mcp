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
