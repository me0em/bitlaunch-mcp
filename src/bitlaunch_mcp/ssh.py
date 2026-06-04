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
