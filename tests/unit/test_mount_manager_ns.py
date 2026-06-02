"""Tests for namespace-aware mount_manager helpers."""

from unittest.mock import MagicMock, patch

from chroot_distro.helpers import mount_manager as mm


def test_get_active_mounts_via_holder():
    holder = MagicMock()
    holder.get_proc_mounts.return_value = (
        "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"
        "tmpfs /tmp/rootfs/dev/shm tmpfs rw,nosuid,nodev,relatime 0 0\n"
    )
    rootfs = "/tmp/rootfs"
    with patch("os.path.realpath", side_effect=lambda p: p):
        mounts = mm.get_active_mounts(rootfs, holder=holder)
    assert "/tmp/rootfs/dev/shm" in mounts


@patch("chroot_distro.helpers.mount_manager._run_mount_cmd")
def test_prepare_rootfs_bind_then_rslave(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    with (
        patch("os.path.isdir", return_value=True),
        patch("os.path.realpath", side_effect=lambda p: p),
        patch.object(mm, "is_mounted", return_value=False),
        patch.object(mm, "safe_mount") as mock_bind,
    ):
        assert mm.prepare_rootfs_for_nested_namespaces("/tmp/rootfs") is True
    mock_bind.assert_called_once_with("/tmp/rootfs", "/tmp/rootfs", holder=None)
    mock_run.assert_called_once_with(["mount", "--make-rslave", "/tmp/rootfs"], None)


@patch("chroot_distro.helpers.mount_manager._run_mount_cmd")
def test_prepare_rootfs_skips_bind_when_already_mounted(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    with (
        patch("os.path.isdir", return_value=True),
        patch("os.path.realpath", side_effect=lambda p: p),
        patch.object(mm, "is_mounted", return_value=True),
        patch.object(mm, "safe_mount") as mock_bind,
    ):
        assert mm.prepare_rootfs_for_nested_namespaces("/tmp/rootfs") is True
    mock_bind.assert_not_called()


@patch("chroot_distro.helpers.mount_manager._run_mount_cmd")
def test_safe_mount_via_holder(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    holder = MagicMock()
    holder.is_mounted = MagicMock(return_value=False)

    with (
        patch("os.path.isdir", return_value=True),
        patch("os.path.exists", return_value=True),
        patch("os.path.realpath", side_effect=lambda p: p),
        patch("os.makedirs"),
        patch.object(mm, "is_mounted", return_value=False),
    ):
        mm.safe_mount("/host/src", "/tmp/rootfs/mnt", holder=holder)

    mock_run.assert_called_once()
    assert mock_run.call_args[0][0] == ["mount", "--bind", "/host/src", "/tmp/rootfs/mnt"]
    assert mock_run.call_args[0][1] is holder
