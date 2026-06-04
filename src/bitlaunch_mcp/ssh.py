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
