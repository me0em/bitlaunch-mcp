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
