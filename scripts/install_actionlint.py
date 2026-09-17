#!/usr/bin/env python3
"""安装仓库固定版本的 actionlint 到本地工具缓存。"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "1.7.12"
AMD64_SHA256 = (
    "8aca8db96f1b94770f1b0d72b6dddcb"  # pragma: allowlist secret
    "1ebb8123cb3712530b08cc387b349a3d8"  # pragma: allowlist secret
)
ARM64_SHA256 = (
    "325e971b6ba9bfa504672e29be93c249"  # pragma: allowlist secret
    "81eeb1c07576d730e9f7c8805afff0c6"  # pragma: allowlist secret
)
ASSETS = {
    "x86_64": ("actionlint_1.7.12_linux_amd64.tar.gz", AMD64_SHA256),
    "aarch64": ("actionlint_1.7.12_linux_arm64.tar.gz", ARM64_SHA256),
    "arm64": ("actionlint_1.7.12_linux_arm64.tar.gz", ARM64_SHA256),
}


def version_matches(binary: Path) -> bool:
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return False
    result = subprocess.run([binary, "-version"], check=False, capture_output=True, text=True)
    return result.returncode == 0 and VERSION in (result.stdout + result.stderr)


def install(target: Path) -> None:
    if platform.system() != "Linux":
        raise RuntimeError(
            "actionlint installer supports the repository Linux/WSL environment only"
        )
    asset = ASSETS.get(platform.machine().lower())
    if asset is None:
        raise RuntimeError(f"unsupported actionlint architecture: {platform.machine()}")
    if version_matches(target):
        return

    filename, expected = asset
    url = f"https://github.com/rhysd/actionlint/releases/download/v{VERSION}/{filename}"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        archive = Path(temporary) / filename
        digest = hashlib.sha256()
        with urlopen(url, timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise RuntimeError(f"actionlint checksum mismatch: expected {expected}, got {actual}")
        with tarfile.open(archive, "r:gz") as bundle:
            member = bundle.getmember("actionlint")
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError("actionlint archive does not contain the executable")
            candidate = Path(temporary) / "actionlint"
            candidate.write_bytes(source.read())
        candidate.chmod(0o755)
        if not version_matches(candidate):
            raise RuntimeError("downloaded actionlint does not report the pinned version")
        os.replace(candidate, target)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: install_actionlint.py TARGET", file=sys.stderr)
        return 2
    try:
        install(Path(argv[1]))
    except (OSError, RuntimeError, tarfile.TarError) as error:
        print(f"actionlint installation failed: {error}", file=sys.stderr)
        return 1
    print(f"actionlint {VERSION}: {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
