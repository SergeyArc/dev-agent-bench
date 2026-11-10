"""Process control: run commands as the agent user, stop agent processes,
start the HTTP app / worker under verifier control and collect their logs.

Agent-written code never runs as root: the verifier state and the hidden tests
are readable by root only.
"""

from __future__ import annotations

import os
import pwd
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Self

from . import config

_PASS_THROUGH = ("UV_EXCLUDE_NEWER", "LANG", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


def agent_env(extra: Mapping[str, str] | None = None, unset: Iterable[str] = ()) -> dict[str, str]:
    """Minimal environment for agent-side processes.

    DATABASE_URL / RABBITMQ_URL are NOT inherited: the caller passes exactly the
    variables the process under test is supposed to read (contract §2.1).
    Names in `unset` are removed last, whatever put them there.
    """
    env = {
        "HOME": str(config.AGENT_HOME),
        "USER": config.AGENT_USER,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONUNBUFFERED": "1",
    }
    for key in _PASS_THROUGH:
        if key in os.environ:
            env[key] = os.environ[key]
    if extra:
        env.update(extra)
    for key in unset:
        env.pop(key, None)
    return env


def _agent_ids() -> tuple[int, int]:
    entry = pwd.getpwnam(config.AGENT_USER)
    return entry.pw_uid, entry.pw_gid


@dataclass
class CommandResult:
    args: list[str]
    returncode: int | None  # None = timed out
    output: str  # stdout + stderr, interleaved
    duration_sec: float

    @property
    def timed_out(self) -> bool:
        return self.returncode is None

    def tail(self, lines: int = 30) -> str:
        return "\n".join(self.output.strip().splitlines()[-lines:])


def run_as_agent(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path = config.APP_DIR,
    timeout: float = 300.0,
    unset: Iterable[str] = (),
) -> CommandResult:
    """Run a command as the agent user; kill its whole process group on timeout."""
    uid, gid = _agent_ids()
    started = time.monotonic()
    proc = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=agent_env(env, unset),
        user=uid,
        group=gid,
        extra_groups=[],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
        errors="replace",
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
        code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
        code = None
    return CommandResult(list(args), code, output or "", round(time.monotonic() - started, 3))


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


# --- /proc helpers ----------------------------------------------------------


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """(state, ppid) of a process, or None if it is gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = raw[raw.rindex(")") + 2 :].split()
    return fields[0], int(fields[1])


def _pids() -> list[int]:
    return [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]


def agent_pids() -> list[int]:
    """Live (non-zombie) processes owned by the agent user."""
    uid, _ = _agent_ids()
    found = []
    for pid in _pids():
        try:
            owner = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            continue
        stat = _proc_stat(pid)
        if owner == uid and stat is not None and stat[0] != "Z":
            found.append(pid)
    return found


def descendants(pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for other in _pids():
        stat = _proc_stat(other)
        if stat is not None and stat[0] != "Z":
            children.setdefault(stat[1], []).append(other)
    out: list[int] = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            out.append(child)
            stack.append(child)
    return out


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def stop_agent_processes(grace: float = 3.0) -> int:
    """Stop every process of the agent user (contract §2.2, first reset step).

    SIGTERM first, SIGKILL after the grace period. Returns the number of
    processes that were running. Raises if something survives SIGKILL.
    """
    pids = agent_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids and not wait_until(lambda: not agent_pids(), grace):
        for pid in agent_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not wait_until(lambda: not agent_pids(), 5.0):
            raise RuntimeError(f"agent processes survived SIGKILL: {agent_pids()}")
    if port_open(config.HTTP_PORT):
        raise RuntimeError(f"port {config.HTTP_PORT} is still in use after stopping agent processes")
    return len(pids)


# --- processes under verifier control ---------------------------------------


class ManagedProcess:
    """A service process (HTTP app, worker) started by the verifier as the agent
    user in its own process group, with output collected into a log file."""

    def __init__(self, name: str, args: Sequence[str], env: Mapping[str, str], log_dir: Path) -> None:
        self.name = name
        self.args = list(args)
        self.env = dict(env)
        self.log_path = log_dir / f"{name}.log"
        self._log: IO[bytes] | None = None
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> ManagedProcess:
        uid, gid = _agent_ids()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "ab")  # noqa: SIM115 - closed in stop()
        self._proc = subprocess.Popen(
            self.args,
            cwd=config.APP_DIR,
            env=agent_env(self.env),
            user=uid,
            group=gid,
            extra_groups=[],
            stdin=subprocess.DEVNULL,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return self

    @property
    def pid(self) -> int:
        """PID of the launcher (`uv run …`), which is also the process group ID."""
        if self._proc is None:
            raise RuntimeError(f"{self.name} was not started")
        return self._proc.pid

    def service_pid(self) -> int | None:
        """PID of the actual service process: the deepest descendant of the
        launcher, or the launcher itself when it has exec'ed into the service.
        Used for the "same PID" check of contract §7.6."""
        if not self.alive():
            return None
        below = descendants(self.pid)
        return below[-1] if below else self.pid

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    def send_signal(self, sig: int) -> None:
        """Signal the whole process group (launcher and service)."""
        if self._proc is not None:
            _kill_group(self._proc.pid, sig)

    def wait(self, timeout: float) -> float | None:
        """Wait for exit; returns seconds waited, or None on timeout."""
        if self._proc is None:
            return 0.0
        started = time.monotonic()
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        # the group may outlive the launcher for a moment
        wait_until(lambda: not self._group_alive(), max(0.0, timeout - (time.monotonic() - started)))
        return round(time.monotonic() - started, 3)

    def _group_alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            os.killpg(self._proc.pid, 0)
        except ProcessLookupError:
            return False
        return any(
            (stat := _proc_stat(pid)) is not None and stat[0] != "Z" and _pgid(pid) == self._proc.pid
            for pid in _pids()
        )

    def terminate(self, timeout: float = config.SIGTERM_LIMIT) -> float | None:
        """SIGTERM and wait; returns the exit time or None if it did not exit."""
        self.send_signal(signal.SIGTERM)
        return self.wait(timeout)

    def kill(self) -> None:
        """SIGKILL (contract §6.3) and reap."""
        self.send_signal(signal.SIGKILL)
        if self._proc is not None:
            self._proc.wait(timeout=10)
            wait_until(lambda: not self._group_alive(), 5.0, interval=0.05)

    def stop(self) -> None:
        """Best-effort shutdown used in clean-up paths."""
        if self._proc is not None and self._group_alive():
            self.send_signal(signal.SIGTERM)
            if self.wait(5.0) is None:
                self.send_signal(signal.SIGKILL)
                self._proc.wait(timeout=10)
        if self._log is not None:
            self._log.close()
            self._log = None

    def log_tail(self, lines: int = 40) -> str:
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


def http_app(
    database_url: str | None,
    *,
    log_dir: Path,
    name: str = "app",
    extra_env: Mapping[str, str] | None = None,
) -> ManagedProcess:
    """HTTP app with the start command of contract §2.2. Reads DATABASE_URL only."""
    env: dict[str, str] = {}
    if database_url is not None:
        env["DATABASE_URL"] = database_url
    env.update(extra_env or {})
    return ManagedProcess(name, config.HTTP_APP_CMD, env, log_dir)


def worker(
    database_url: str,
    rabbitmq_url: str,
    *,
    log_dir: Path,
    name: str = "worker",
    max_retries: int | None = None,
) -> ManagedProcess:
    """Worker with the start command of contract §2.2."""
    env = {"DATABASE_URL": database_url, "RABBITMQ_URL": rabbitmq_url}
    if max_retries is not None:
        env["CONSUMER_MAX_RETRIES"] = str(max_retries)
    return ManagedProcess(name, config.WORKER_CMD, env, log_dir)


def wait_health(process: ManagedProcess | None = None, timeout: float = config.HEALTH_TIMEOUT) -> bool:
    """Poll GET /health until 200. Gives up early if the process exited."""
    import httpx

    def ok() -> bool:
        try:
            return httpx.get(f"{config.HTTP_BASE}/health", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ok():
            return True
        if process is not None and not process.alive():
            return False
        time.sleep(0.25)
    return False
