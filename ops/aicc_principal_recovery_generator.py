#!/usr/bin/python3
"""Generate early-boot recovery only while an install journal is pending."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

STATE_DIR = Path("/var/lib/aicc-principal-isolation")

# Named tuple so the py314-target formatter cannot rewrite the except clause
# into the PEP 758 unparenthesized form, which is a SyntaxError on Python 3.13.
_JOURNAL_READ_ERRORS = (
    FileNotFoundError,
    KeyError,
    OSError,
    ValueError,
    json.JSONDecodeError,
)


def generate(
    destination: Path, state_dir: Path = STATE_DIR, *, expected_uid: int = 0
) -> bool:
    pending = state_dir / "pending.json"
    try:
        pending_info = pending.lstat()
        payload = json.loads(pending.read_text(encoding="utf-8"))
        recovery_path = Path(payload["recovery"])
        info = recovery_path.lstat()
        recovery = str(recovery_path.resolve(strict=True))
    except _JOURNAL_READ_ERRORS:
        return False
    expected_prefix = re.escape(str(state_dir.resolve()))
    pattern = re.compile(rf"{expected_prefix}/generation-[a-f0-9]{{16}}/recovery\.py")
    if (
        not stat.S_ISREG(pending_info.st_mode)
        or stat.S_ISLNK(pending_info.st_mode)
        or pending_info.st_uid != expected_uid
        or stat.S_IMODE(pending_info.st_mode) != 0o600
        or not pattern.fullmatch(recovery)
        or str(recovery_path) != recovery
        or not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != expected_uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        return False
    destination.mkdir(parents=True, exist_ok=True)
    unit = destination / "aicc-principal-recovery.service"
    temporary = destination / f".{unit.name}.{os.getpid()}"
    temporary.write_text(
        "[Unit]\n"
        "Description=Recover interrupted AICC principal-isolation generation\n"
        "DefaultDependencies=no\n"
        "Before=basic.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/usr/bin/python3 {recovery} recover --state-dir {state_dir}\n"
        "NoNewPrivileges=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=yes\n"
        "PrivateTmp=yes\n"
        "ReadWritePaths=/etc/aicc /etc/systemd/system "
        "/usr/lib/systemd/system-generators /usr/lib/sysusers.d "
        "/usr/lib/tmpfiles.d /usr/libexec /var/lib/aicc-agent "
        "/var/lib/aicc-principal-isolation\n",
        encoding="utf-8",
    )
    temporary.chmod(0o644)
    os.replace(temporary, unit)
    return True


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    generate(Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
