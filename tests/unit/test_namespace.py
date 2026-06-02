"""Unit tests for namespace isolation helpers."""

from unittest.mock import MagicMock, patch

import pytest

from chroot_distro.helpers import namespace as ns


def test_long_flags_to_nsenter_short():
    flags = ["--mount", "--pid", "--uts", "--ipc"]
    assert ns.long_flags_to_nsenter(flags, use_long=False) == ["-m", "-p", "-u", "-i"]


def test_holder_unshare_argv_adds_fork_with_pid():
    argv = ns._holder_unshare_argv("unshare", ["--pid", "--mount"], "/tmp/holder.spawn.pid")
    assert argv[:4] == ["unshare", "--fork", "--pid", "--mount"]
    assert argv[4] in ("sh", "bash")
    assert argv[5] == "-c"
    assert "holder.spawn.pid" in argv[6]
    assert "exec sleep infinity" in argv[6]
    assert "exec while" not in argv[6]


@patch("chroot_distro.helpers.namespace.IS_TERMUX", True)
@patch("chroot_distro.helpers.namespace.TERMUX_PREFIX", "/data/data/com.termux/files/usr")
def test_holder_inner_script_termux_no_exec_while():
    with patch("os.path.isfile", return_value=True):
        script = ns._holder_inner_script("/tmp/spawn.pid")
    assert "exec while" not in script
    assert "while true; do" in script
    assert "86400" in script


def test_holder_unshare_argv_no_duplicate_fork():
    argv = ns._holder_unshare_argv("unshare", ["--fork", "--mount"], "/tmp/spawn.pid")
    assert argv.count("--fork") == 1
    assert "sh" in argv and "-c" in argv


def test_read_spawn_holder_pid(tmp_path):
    spawn = tmp_path / "spawn.pid"
    spawn.write_text("4242")
    with patch.object(ns, "_pid_alive", return_value=True):
        assert ns._read_spawn_holder_pid(str(spawn)) == 4242


def test_wait_for_holder_pid_uses_spawn_file(tmp_path):
    spawn = tmp_path / "spawn.pid"
    spawn.write_text("99")
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = None
    with (
        patch.object(ns, "_pid_alive", return_value=True),
        patch.object(ns, "_is_sleep_infinity_holder", return_value=True),
    ):
        pid = ns._wait_for_holder_pid(
            spawn_pid_file=str(spawn),
            before_sleep=set(),
            launcher_pid=1,
            proc=proc,
            max_attempts=1,
        )
    assert pid == 99


def test_pick_new_holder_pid():
    before = {10, 20}
    with patch.object(ns, "_snapshot_sleep_infinity_pids", return_value={10, 20, 99}):
        assert ns._pick_new_holder_pid(before) == 99


def test_pick_new_holder_pid_from_launcher_child():
    before: set[int] = set()
    with (
        patch.object(ns, "_snapshot_sleep_infinity_pids", return_value=set()),
        patch.object(ns, "_read_host_child_pids", return_value=[12345]),
        patch.object(ns, "_is_sleep_infinity_holder", side_effect=lambda pid: pid == 12345),
    ):
        assert ns._pick_new_holder_pid(before, launcher_pid=999) == 12345


def test_long_flags_to_nsenter_long():
    flags = ["--mount", "--pid"]
    assert ns.long_flags_to_nsenter(flags, use_long=True) == ["--mount", "--pid"]


@patch("chroot_distro.helpers.namespace.subprocess.run")
def test_mount_namespace_available(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    assert ns.mount_namespace_available() is True
    mock_run.assert_called_once()


@patch("chroot_distro.helpers.namespace.mount_namespace_available", return_value=True)
@patch("chroot_distro.helpers.namespace.subprocess.run")
def test_probe_unshare_flags_requires_mount(mock_run, _mount_ok):
    def side_effect(cmd, **kwargs):
        flag = cmd[1] if len(cmd) > 1 else ""
        rc = 0 if flag in ("--mount", "--pid") else 1
        return MagicMock(returncode=rc)

    mock_run.side_effect = side_effect
    flags = ns.probe_unshare_flags()
    assert "--mount" in flags
    assert "--pid" in flags


@patch("chroot_distro.helpers.namespace.mount_namespace_available", return_value=False)
def test_probe_unshare_flags_fails_without_mount(_mount_ok):
    with pytest.raises(ns.NamespaceError, match="Mount namespace"):
        ns.probe_unshare_flags()


def test_check_isolation_conflicts_namespace_mode_without_flag():
    with (
        patch.object(ns, "get_live_holder", return_value=MagicMock(pid=1)),
        patch.object(ns, "read_isolation_mode", return_value=ns.ISOLATION_MODE_NAMESPACE),
        pytest.raises(ns.NamespaceError, match="isolated namespace mode"),
    ):
        ns.check_isolation_conflicts(
            "alpine",
            want_full_namespace=False,
            want_mount_namespace=True,
            host_mounts_exist=False,
        )


def test_check_isolation_conflicts_host_mounts_with_isolated():
    with (
        patch.object(ns, "get_live_holder", return_value=None),
        patch.object(ns, "read_isolation_mode", return_value=ns.ISOLATION_MODE_HOST),
        pytest.raises(ns.NamespaceError, match="host mount namespace"),
    ):
        ns.check_isolation_conflicts(
            "alpine",
            want_full_namespace=True,
            want_mount_namespace=False,
            host_mounts_exist=True,
        )


@patch("chroot_distro.helpers.namespace._pid_alive", return_value=True)
@patch("chroot_distro.helpers.namespace._read_holder_flags", return_value=["--mount"])
@patch("chroot_distro.helpers.namespace._read_holder_pid", return_value=42)
@patch("chroot_distro.helpers.namespace._nsenter_supports_long_flags", return_value=True)
def test_get_live_holder(*_mocks):
    holder = ns.get_live_holder("alpine")
    assert holder is not None
    assert holder.pid == 42
    assert holder.run_argv(["echo", "hi"])[0] == "nsenter"


@patch("chroot_distro.helpers.namespace._create_holder")
def test_acquire_mount_holder_creates_mount_only(mock_create):
    mock_create.return_value = MagicMock(pid=50)
    holder = ns.acquire_mount_holder("alpine")
    assert holder.pid == 50
    mock_create.assert_called_once_with("alpine", ns.MOUNT_ONLY_FLAGS)


@patch("chroot_distro.helpers.namespace._read_holder_flags", return_value=["--mount", "--pid"])
@patch("chroot_distro.helpers.namespace.get_live_holder")
@patch("chroot_distro.helpers.namespace._create_holder")
@patch("chroot_distro.helpers.namespace.probe_unshare_flags", return_value=["--mount"])
def test_acquire_holder_reuses_existing(mock_probe, mock_create, mock_get, _mock_flags):
    existing = MagicMock(pid=99)
    mock_get.return_value = existing
    assert ns.acquire_holder("alpine") is existing
    mock_create.assert_not_called()
    mock_probe.assert_not_called()


@patch("chroot_distro.helpers.namespace._remove_holder_state")
@patch("chroot_distro.helpers.namespace._read_holder_pid", return_value=100)
@patch("chroot_distro.helpers.namespace.os.kill")
def test_release_holder(mock_kill, *_mocks):
    ns.release_holder("alpine")
    assert mock_kill.called
