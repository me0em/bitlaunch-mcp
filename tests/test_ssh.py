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
