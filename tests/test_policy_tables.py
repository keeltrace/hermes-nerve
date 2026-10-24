"""Policy-table regression pins for the plaintext policy data refactor.

The protected-path and blocked-command policy data moved from inline Python
literals to auditable plaintext tables under ``hermes_nerve/policy/``. These
tests pin the exact protective content so no future edit can silently drop
or weaken an entry, and pin the closed-boundary predicate behavior
(non-string operands and str subclasses never see entries).
"""
import pytest

from pathlib import Path

from hermes_nerve import risk
from hermes_nerve.policy_tables import TableLoadError, load_table, parse_table

# Frozen policy content — the exact protective entries the refactor had to
# preserve (extracted from the pre-refactor source). Any difference is a
# security-policy change and must fail this suite.
FROZEN_BLOCKED_SYSTEM_PREFIXES = (
    "/etc/ssh", "/etc/sudoers", "/etc/cron", "/etc/shadow", "/etc/gshadow",
    "/etc/ssl/private", "/etc/openvpn", "/etc/wireguard", "/etc/systemd/system",
    "/etc/init.d", "/etc/rc", "/etc/ld.so.preload", "/etc/pam.d", "/root",
)
FROZEN_DISK_EXECUTABLES = frozenset({
    "dd", "mkfs", "mkfs.ext4", "mkfs.xfs", "mkfs.btrfs", "newfs", "fdisk",
    "parted", "gparted", "sfdisk", "cfdisk", "blkdiscard", "wipefs"})
FROZEN_UNBOUNDED_EXECUTABLES = frozenset({
    "sudo", "doas", "su", "pkexec", "systemctl", "service", "launchctl",
    "powerctl", "rebootctl", "halt", "poweroff", "init", "kill", "pkill", "killall",
    "dd", "mkfs", "mkfs.ext4", "parted", "fdisk", "shred", "wipefs",
    "mount", "umount", "modprobe", "insmod", "iptables", "nft", "ufw",
    "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "ftp", "telnet",
    "curl", "wget", "aria2c", "http", "https", "httpie",
    "docker", "podman", "kubectl", "helm", "terraform", "aws", "gcloud", "az",
    "gh", "npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "go",
    "printenv", "env", "set", "export", "declare", "typeset", "compgen",
    "crontab", "at", "useradd", "usermod", "passwd", "visudo", "chown", "chmod",
    "eval", "exec", "source", "bash", "sh", "zsh", "dash", "ksh", "fish",
    "timeout", "xargs", "nohup", "stdbuf", "watch", "setsid", "flock",
    "nice", "ionice", "taskset", "chroot", "unshare", "nsenter", "script"})


class TestTablesLoadExactly:
    def test_loaded_tables_match_frozen_content(self):
        assert risk._BLOCKED_SYSTEM_PREFIXES == FROZEN_BLOCKED_SYSTEM_PREFIXES
        assert risk._DISK_EXECUTABLES == FROZEN_DISK_EXECUTABLES
        assert risk._UNBOUNDED_EXECUTABLES == FROZEN_UNBOUNDED_EXECUTABLES

    def test_packaged_tables_parse_to_the_same_content(self):
        assert load_table("blocked_system_prefixes.txt") == FROZEN_BLOCKED_SYSTEM_PREFIXES
        assert frozenset(load_table("disk_executables.txt")) == FROZEN_DISK_EXECUTABLES
        assert frozenset(load_table("unbounded_executables.txt")) == FROZEN_UNBOUNDED_EXECUTABLES

    def test_no_protective_entry_was_lost_or_added(self):
        # Every frozen entry must still be an active policy member.
        for prefix in FROZEN_BLOCKED_SYSTEM_PREFIXES:
            assert prefix in risk._BLOCKED_SYSTEM_PREFIXES
        for name in FROZEN_DISK_EXECUTABLES | FROZEN_UNBOUNDED_EXECUTABLES:
            assert name in risk._DISK_EXECUTABLES | risk._UNBOUNDED_EXECUTABLES


class TestTableLoaderIntegrity:
    def test_missing_table_raises_loudly(self):
        with pytest.raises(TableLoadError):
            load_table("no-such-table.txt")

    def test_duplicate_entry_rejected(self):
        with pytest.raises(TableLoadError):
            parse_table("visudo\nvisudo\n")

    def test_empty_table_rejected(self):
        with pytest.raises(TableLoadError):
            parse_table("")
        with pytest.raises(TableLoadError):
            parse_table("# only a comment\n\n")

    def test_parse_preserves_order_and_strips(self):
        assert parse_table("# c\n  b \n\na\n") == ("b", "a")


class TestClosedPolicyBoundaries:
    """The predicates stay closed: non-str operands and str subclasses never see entries."""

    @pytest.mark.parametrize("bad", [None, 5, 3.14, True, b"/etc/shadow", ["mkfs"], {"a"}, object()])
    def test_non_str_operand_returns_false(self, bad):
        assert risk._protected_system_match(bad, "") is False
        assert risk._is_disk_executable(bad) is False
        assert risk._is_unbounded_executable(bad) is False

    def test_str_subclass_is_rejected(self):
        class HostileStr(str):
            def __eq__(self, other):
                return True

        assert risk._protected_system_match(HostileStr("/etc/shadow"), "") is False
        assert risk._is_disk_executable(HostileStr("mkfs")) is False
        assert risk._is_unbounded_executable(HostileStr("visudo")) is False


class TestPolicyMembershipBoundaries:
    def test_protected_prefix_boundaries(self):
        # Exact matches and beneath-boundary matches stay protected.
        for p in FROZEN_BLOCKED_SYSTEM_PREFIXES:
            assert risk._protected_system_match(p, "") is True
            assert risk._protected_system_match(p + "/nested/file", "/") is True
        # Near misses stay unprotected under the boundary suffix.
        for q in ("/etc/sudoers.d/x", "/etc/shadow-x", "/rootkit", "/roots",
                  "/etc/sshels", "/etc/crontab", "/etc/passwd", "/etc/ssl/privatekey"):
            assert risk._protected_system_match(q, "/") is False

    def test_disk_executable_name_boundaries(self):
        for name in FROZEN_DISK_EXECUTABLES:
            assert risk._is_disk_executable(name) is True
        for near in ("mkfsext4", "mkfs.ext5", "mkfs.ext4 ", " mkfs", "MKFS",
                     "mkfs\n", "ddisk", "wipefs2", ""):
            assert risk._is_disk_executable(near) is False

    def test_unbounded_name_boundaries(self):
        for name in FROZEN_UNBOUNDED_EXECUTABLES:
            assert risk._is_unbounded_executable(name) is True
        for near in ("VISUDO", "visudo\n", "passwd2", "sudoo", "chromium", ""):
            assert risk._is_unbounded_executable(near) is False


class TestEndToEndClassificationUnchanged:
    """The engine's decisions flow through the loaded tables unchanged."""

    def test_disk_commands_remain_prohibited(self, tmp_path):
        for cmd in ("mkfs.ext4 /dev/sda1", "dd if=x.iso of=/dev/sda", "wipefs /dev/sdb",
                    "sudo mkfs.ext4 /dev/sda1", "doas dd if=/dev/zero of=/dev/sda"):
            assessment = risk.classify_segment(cmd, tmp_path, [])
            assert assessment.category == "prohibited", cmd

    def test_unbounded_names_are_not_bounded(self):
        for name in ("visudo", "useradd", "mkfs", "printenv", "chroot"):
            assert risk._segment_is_bounded(name) is False

    def test_protected_paths_stay_sensitive(self):
        assert risk.sensitive_read_target(Path("/etc/shadow")) is True
        assert risk.sensitive_read_target(Path("/etc/sudoers")) is True
        assert risk.valid_scope_root(Path("/etc/ssh")) is False
        assert risk.valid_scope_root(Path("/root")) is False
        assert risk.classify_path(Path("/etc/shadow"), Path("/home/user/proj")) == "system"

    def test_safe_commands_stay_safe(self, tmp_path):
        for cmd in ("ls -la", "git status", "grep -rn todo src/"):
            assessment = risk.classify_segment(cmd, tmp_path, [])
            assert assessment.category == "safe_routine", cmd
