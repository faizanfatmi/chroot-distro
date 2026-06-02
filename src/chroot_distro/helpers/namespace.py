"""Linux namespace isolation for --isolated sessions (Ubuntu-Chroot pattern)."""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass

from chroot_distro.constants import IS_TERMUX, PROGRAM_NAME, RUNTIME_DIR, TERMUX_PREFIX
from chroot_distro.exceptions import ChrootDistroError

log = logging.getLogger(__name__)

_PROBE_FLAGS = ("--pid", "--mount", "--uts", "--ipc")
_LONG_TO_SHORT = {
    "--mount": "-m",
    "--uts": "-u",
    "--ipc": "-i",
    "--pid": "-p",
}

ISOLATION_MODE_NAMESPACE = "namespace"
ISOLATION_MODE_MOUNT = "mount"
ISOLATION_MODE_HOST = "host"

MOUNT_ONLY_FLAGS = ["--mount"]
_FULL_NAMESPACE_FLAGS = frozenset({"--pid", "--uts", "--ipc"})


class NamespaceError(ChrootDistroError):
    """Raised when namespace setup or execution fails."""


def _container_data_dir(container_name: str) -> str:
    data_dir = os.path.join(RUNTIME_DIR, "data", container_name)
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _holder_pid_file(container_name: str) -> str:
    return os.path.join(_container_data_dir(container_name), "holder.pid")


def _holder_flags_file(container_name: str) -> str:
    return os.path.join(_container_data_dir(container_name), "holder.flags")


def _holder_spawn_pid_file(container_name: str) -> str:
    return os.path.join(_container_data_dir(container_name), "holder.spawn.pid")


def _isolation_mode_file(container_name: str) -> str:
    return os.path.join(_container_data_dir(container_name), "isolation.mode")


def _resolve_unshare() -> str:
    if IS_TERMUX:
        termux_unshare = os.path.join(TERMUX_PREFIX, "bin", "unshare")
        if os.path.isfile(termux_unshare):
            return termux_unshare
    return "unshare"


def _resolve_nsenter() -> str:
    if IS_TERMUX:
        termux_nsenter = os.path.join(TERMUX_PREFIX, "bin", "nsenter")
        if os.path.isfile(termux_nsenter):
            return termux_nsenter
    return "nsenter"


def _nsenter_supports_long_flags(nsenter: str) -> bool:
    try:
        result = subprocess.run(
            [nsenter, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = (result.stdout or "") + (result.stderr or "")
    return "--mount" in output


def long_flags_to_nsenter(flags: list[str], *, use_long: bool) -> list[str]:
    """Translate unshare long flags to nsenter argv tokens."""
    if use_long:
        return list(flags)
    return [_LONG_TO_SHORT[f] for f in flags if f in _LONG_TO_SHORT]


def mount_namespace_available() -> bool:
    """Return True if ``unshare --mount`` succeeds (requires root on Termux)."""
    unshare = _resolve_unshare()
    try:
        result = subprocess.run(
            [unshare, "--mount", "true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def probe_unshare_flags() -> list[str]:
    """Return supported unshare flags; mount namespace is required."""
    if not mount_namespace_available():
        raise NamespaceError("Mount namespace not supported by this kernel (unshare --mount failed).")
    unshare = _resolve_unshare()
    supported: list[str] = []
    for flag in _PROBE_FLAGS:
        try:
            result = subprocess.run(
                [unshare, flag, "true"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            supported.append(flag)

    if "--mount" not in supported:
        raise NamespaceError("Mount namespace not supported by this kernel (unshare --mount failed).")
    return supported


def read_isolation_mode(container_name: str) -> str | None:
    path = _isolation_mode_file(container_name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            mode = fh.read().strip()
    except OSError:
        return None
    return mode or None


def write_isolation_mode(container_name: str, mode: str) -> None:
    with open(_isolation_mode_file(container_name), "w") as fh:
        fh.write(mode)


def clear_isolation_mode(container_name: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(_isolation_mode_file(container_name))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_holder_pid(container_name: str) -> int | None:
    path = _holder_pid_file(container_name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    if not _pid_alive(pid):
        return None
    if not _is_valid_holder_pid(pid):
        return None
    return pid


def _read_holder_flags(container_name: str) -> list[str]:
    path = _holder_flags_file(container_name)
    if not os.path.isfile(path):
        return ["--mount"]
    try:
        with open(path) as fh:
            flags = fh.read().split()
    except OSError:
        return ["--mount"]
    return flags or ["--mount"]


def _remove_holder_state(container_name: str) -> None:
    for path in (
        _holder_pid_file(container_name),
        _holder_flags_file(container_name),
        _holder_spawn_pid_file(container_name),
    ):
        with contextlib.suppress(OSError):
            os.remove(path)


def _proc_comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _is_sleep_infinity_holder(pid: int) -> bool:
    if _proc_comm(pid) != "sleep":
        return False
    try:
        with open(f"/proc/{pid}/cmdline") as fh:
            cmdline = fh.read().replace("\0", " ")
    except OSError:
        return False
    return "infinity" in cmdline.split()


def _resolve_holder_shell() -> str:
    """Shell used to start the holder (Termux ``sudo`` often drops ``$PREFIX`` from PATH)."""
    if IS_TERMUX:
        for candidate in (
            os.path.join(TERMUX_PREFIX, "bin", "bash"),
            os.path.join(TERMUX_PREFIX, "bin", "sh"),
        ):
            if os.path.isfile(candidate):
                return candidate
    return "sh"


def _holder_inner_script(spawn_pid_file: str) -> str:
    """Shell script body for the namespace holder (must not ``exec`` a compound command)."""
    pid_write = f"echo $$ > {shlex.quote(spawn_pid_file)}"
    if IS_TERMUX:
        termux_sleep = os.path.join(TERMUX_PREFIX, "bin", "sleep")
        if os.path.isfile(termux_sleep):
            # ``exec while …`` is invalid; run the loop in the shell (no exec).
            return f"{pid_write}; while true; do {shlex.quote(termux_sleep)} 86400; done"
    return f"{pid_write}; exec sleep infinity"


def _read_spawn_holder_pid(spawn_pid_file: str) -> int | None:
    if not os.path.isfile(spawn_pid_file):
        return None
    try:
        with open(spawn_pid_file) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    if not _pid_alive(pid):
        return None
    return pid


def _is_valid_holder_pid(pid: int) -> bool:
    if not _pid_alive(pid):
        return False
    if _is_sleep_infinity_holder(pid):
        return True
    if IS_TERMUX:
        return _proc_comm(pid) in ("sleep", "sh")
    return False


def _snapshot_sleep_infinity_pids() -> set[int]:
    pids: set[int] = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if _is_sleep_infinity_holder(pid):
            pids.add(pid)
    return pids


def _read_host_child_pids(pid: int) -> list[int]:
    children: list[int] = []
    task_dir = f"/proc/{pid}/task"
    if not os.path.isdir(task_dir):
        return children
    for tid in os.listdir(task_dir):
        children_path = os.path.join(task_dir, tid, "children")
        try:
            with open(children_path) as fh:
                for token in fh.read().split():
                    if token.isdigit():
                        children.append(int(token))
        except OSError:
            continue
    return children


def _pick_new_holder_pid(before: set[int], launcher_pid: int | None = None) -> int | None:
    candidates: list[int] = []
    if launcher_pid is not None:
        if launcher_pid not in before and _is_sleep_infinity_holder(launcher_pid):
            candidates.append(launcher_pid)
        for child_pid in _read_host_child_pids(launcher_pid):
            if child_pid not in before and _is_sleep_infinity_holder(child_pid):
                candidates.append(child_pid)

    for pid in _snapshot_sleep_infinity_pids():
        if pid not in before and pid not in candidates:
            candidates.append(pid)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return min(candidates, key=lambda pid: os.stat(f"/proc/{pid}").st_mtime)


@dataclass
class NamespaceHolder:
    """A long-lived process holding mount/PID/UTS/IPC namespaces."""

    pid: int
    nsenter_flags: list[str]
    nsenter_exe: str
    container_name: str

    def run_argv(self, cmd: list[str]) -> list[str]:
        return [self.nsenter_exe, "--target", str(self.pid), *self.nsenter_flags, "--", *cmd]

    def run(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        check = kwargs.pop("check", False)
        return subprocess.run(self.run_argv(cmd), check=check, **kwargs)

    def is_mounted(self, target: str) -> bool:
        try:
            result = self.run(["mountpoint", "-q", target], capture_output=True)
        except OSError:
            return False
        return result.returncode == 0

    def get_proc_mounts(self) -> str:
        result = self.run(["cat", "/proc/mounts"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return ""
        return result.stdout or ""


def get_live_holder(container_name: str) -> NamespaceHolder | None:
    """Return an active holder for the container, or None."""
    pid = _read_holder_pid(container_name)
    if pid is None:
        return None
    flags = _read_holder_flags(container_name)
    nsenter = _resolve_nsenter()
    use_long = _nsenter_supports_long_flags(nsenter)
    return NamespaceHolder(
        pid=pid,
        nsenter_flags=long_flags_to_nsenter(flags, use_long=use_long),
        nsenter_exe=nsenter,
        container_name=container_name,
    )


def _holder_unshare_argv(unshare: str, flags: list[str], spawn_pid_file: str) -> list[str]:
    """Build unshare argv for a detached namespace holder.

    The inner shell writes its PID to *spawn_pid_file* before execing a blocking
    sleep loop.  /proc scanning alone is unreliable on Android (Termux).
    """
    argv = [unshare]
    if "--pid" in flags and "--fork" not in flags and "-f" not in flags:
        argv.append("--fork")
    argv.extend(flags)
    script = _holder_inner_script(spawn_pid_file)
    argv.extend([_resolve_holder_shell(), "-c", script])
    return argv


def _wait_for_holder_pid(
    *,
    spawn_pid_file: str,
    before_sleep: set[int],
    launcher_pid: int,
    proc: subprocess.Popen,
    max_attempts: int = 150,
) -> int | None:
    for _ in range(max_attempts):
        spawn_pid = _read_spawn_holder_pid(spawn_pid_file)
        if spawn_pid is not None and _is_valid_holder_pid(spawn_pid):
            return spawn_pid

        scanned = _pick_new_holder_pid(before_sleep, launcher_pid=launcher_pid)
        if scanned is not None and _is_valid_holder_pid(scanned):
            return scanned

        if proc.poll() is not None and proc.returncode not in (0, None):
            break
        time.sleep(0.02)
    return None


def _create_holder(container_name: str, flags: list[str]) -> NamespaceHolder:
    unshare = _resolve_unshare()
    pid_file = _holder_pid_file(container_name)
    flags_file = _holder_flags_file(container_name)
    spawn_pid_file = _holder_spawn_pid_file(container_name)

    _remove_holder_state(container_name)

    before_sleep = _snapshot_sleep_infinity_pids()
    unshare_argv = _holder_unshare_argv(unshare, flags, spawn_pid_file)
    proc = subprocess.Popen(
        unshare_argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    host_pid = _wait_for_holder_pid(
        spawn_pid_file=spawn_pid_file,
        before_sleep=before_sleep,
        launcher_pid=proc.pid,
        proc=proc,
    )

    holder_stderr = ""
    if host_pid is None:
        with contextlib.suppress(Exception):
            if proc.stderr is not None:
                _, err = proc.communicate(timeout=2)
                holder_stderr = (err or b"").decode(errors="replace").strip()[:300]
        with contextlib.suppress(OSError):
            proc.kill()
        detail = f" unshare exited with {proc.returncode}." if proc.returncode else ""
        if holder_stderr:
            detail += f" {holder_stderr}"
        raise NamespaceError(
            "Failed to locate namespace holder process (sleep infinity) on the host."
            + detail
        )

    with contextlib.suppress(OSError):
        os.remove(spawn_pid_file)

    if not _is_valid_holder_pid(host_pid):
        with contextlib.suppress(OSError):
            os.kill(host_pid, signal.SIGKILL)
        raise NamespaceError(f"Namespace holder PID {host_pid} is not a valid holder process.")

    with open(pid_file, "w") as fh:
        fh.write(str(host_pid))
    with open(flags_file, "w") as fh:
        fh.write(" ".join(flags))

    nsenter = _resolve_nsenter()
    use_long = _nsenter_supports_long_flags(nsenter)
    return NamespaceHolder(
        pid=host_pid,
        nsenter_flags=long_flags_to_nsenter(flags, use_long=use_long),
        nsenter_exe=nsenter,
        container_name=container_name,
    )


def is_full_namespace_holder(flags: list[str]) -> bool:
    """Return True when holder flags include PID/UTS/IPC (``--isolated`` session)."""
    return bool(_FULL_NAMESPACE_FLAGS.intersection(flags))


def acquire_mount_holder(container_name: str) -> NamespaceHolder:
    """Reuse or create a mount-only namespace holder (normal login, nested bwrap)."""
    existing = get_live_holder(container_name)
    if existing is not None:
        flags = _read_holder_flags(container_name)
        if is_full_namespace_holder(flags):
            raise NamespaceError(
                f"Container '{container_name}' has a full isolated namespace holder. "
                f"Run '{PROGRAM_NAME} unmount {container_name}' before a normal login."
            )
        return existing
    return _create_holder(container_name, MOUNT_ONLY_FLAGS)


def acquire_holder(container_name: str) -> NamespaceHolder:
    """Reuse or create a namespace holder for the container."""
    existing = get_live_holder(container_name)
    if existing is not None:
        flags = _read_holder_flags(container_name)
        if flags == MOUNT_ONLY_FLAGS:
            raise NamespaceError(
                f"Container '{container_name}' has a mount-namespace login session. "
                f"Run '{PROGRAM_NAME} unmount {container_name}' before using --isolated."
            )
        return existing
    flags = probe_unshare_flags()
    return _create_holder(container_name, flags)


def release_holder(container_name: str) -> None:
    """Kill the namespace holder and remove state files."""
    pid = _read_holder_pid(container_name)
    if pid is not None:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            if not _pid_alive(pid):
                break
            time.sleep(0.05)
        if _pid_alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
    _remove_holder_state(container_name)


def make_mount_private(holder: NamespaceHolder) -> bool:
    """Set mount propagation to rprivate inside the holder's mount namespace."""
    try:
        result = holder.run(["mount", "--make-rprivate", "/"], capture_output=True, text=True)
    except OSError:
        return False
    if result.returncode != 0:
        log.debug("mount --make-rprivate / failed: %s", (result.stderr or "").strip())
        return False
    return True


def make_mount_slave(holder: NamespaceHolder) -> bool:
    """Set mount propagation to rslave on / in the holder's mount namespace.

    rprivate breaks nested bubblewrap (``Failed to make / slave``); rslave keeps
    isolation from the host while allowing nested sandboxes inside the guest.
    """
    try:
        result = holder.run(["mount", "--make-rslave", "/"], capture_output=True, text=True)
    except OSError:
        return False
    if result.returncode != 0:
        log.debug("mount --make-rslave / failed: %s", (result.stderr or "").strip())
        return False
    return True


def check_isolation_conflicts(
    container_name: str,
    *,
    want_full_namespace: bool,
    want_mount_namespace: bool,
    host_mounts_exist: bool,
) -> None:
    """Raise NamespaceError when login modes would mix."""
    mode = read_isolation_mode(container_name)
    live_holder = get_live_holder(container_name)

    if want_full_namespace:
        if mode == ISOLATION_MODE_HOST and host_mounts_exist:
            raise NamespaceError(
                f"Container '{container_name}' has active mounts in the host mount namespace. "
                f"Run '{PROGRAM_NAME} unmount {container_name}' before using --isolated."
            )
        if mode == ISOLATION_MODE_MOUNT:
            raise NamespaceError(
                f"Container '{container_name}' has an active mount-namespace login session. "
                f"Run '{PROGRAM_NAME} unmount {container_name}' before using --isolated."
            )
        if mode == ISOLATION_MODE_HOST and not host_mounts_exist:
            clear_isolation_mode(container_name)
    elif want_mount_namespace:
        if mode == ISOLATION_MODE_NAMESPACE:
            raise NamespaceError(
                f"Container '{container_name}' is in isolated namespace mode. "
                f"Use --isolated or run '{PROGRAM_NAME} unmount {container_name}' first."
            )
        if live_holder is not None and is_full_namespace_holder(_read_holder_flags(container_name)):
            raise NamespaceError(
                f"Container '{container_name}' has a full isolated namespace holder. "
                f"Run '{PROGRAM_NAME} unmount {container_name}' first."
            )
    elif live_holder is not None or mode in (
        ISOLATION_MODE_NAMESPACE,
        ISOLATION_MODE_MOUNT,
    ):
        raise NamespaceError(
            f"Container '{container_name}' has an active namespace session. "
            f"Run '{PROGRAM_NAME} unmount {container_name}' before --minimal login."
        )
