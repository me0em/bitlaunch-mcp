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
    min_balance_hours: float = 24.0  # require balance >= this many hours of the plan
    max_topup_usd: float = 50.0      # create_transaction refuses larger invoices


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
        min_balance_hours=float(env.get("BITLAUNCH_MIN_BALANCE_HOURS", "24")),
        max_topup_usd=float(env.get("BITLAUNCH_MAX_TOPUP_USD", "50")),
        ssh_key_path=Path(
            env.get("BITLAUNCH_SSH_KEY_PATH", "~/.bitlaunch-mcp/id_ed25519")
        ).expanduser(),
    )
