from __future__ import annotations

import argparse
import hashlib
import tempfile
import urllib.request
from pathlib import Path

TEST_ROM_NAME = "ucity.gbc"
TEST_ROM_TAG = "v1.3"
TEST_ROM_SHA256 = "9422ee2ca7b7ea1d46b58b2a429fff3f354dfd3e732dee1e7ae6220f148ce6e0"
TEST_ROM_URL = (
    f"https://github.com/AntonioND/ucity/releases/download/{TEST_ROM_TAG}/{TEST_ROM_NAME}"
)
TEST_ROM_RELATIVE_PATH = Path("roms") / TEST_ROM_NAME


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_test_rom_path(root: Path | None = None) -> Path:
    base = repo_root() if root is None else root
    return (base / TEST_ROM_RELATIVE_PATH).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_test_rom(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Test ROM not found: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Test ROM path is not a regular file: {resolved}")

    actual = sha256_file(resolved)
    if actual != TEST_ROM_SHA256:
        raise ValueError(
            f"Test ROM checksum mismatch for {resolved}: expected {TEST_ROM_SHA256}, got {actual}"
        )
    return resolved


def _download_bytes(*, timeout: float) -> bytes:
    request = urllib.request.Request(TEST_ROM_URL, headers={"User-Agent": "mgba-live-mcp-test-rom"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise RuntimeError(f"Failed to download test ROM: HTTP {status}")
        return response.read()


def fetch_test_rom(*, out: Path | None = None, timeout: float = 30.0) -> Path:
    target = default_test_rom_path() if out is None else out.resolve()
    if target.is_file():
        try:
            return verify_test_rom(target)
        except ValueError:
            pass

    payload = _download_bytes(timeout=timeout)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != TEST_ROM_SHA256:
        raise RuntimeError(
            f"Downloaded test ROM checksum mismatch: expected {TEST_ROM_SHA256}, got {actual}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            handle.write(payload)
            tmp_path = Path(handle.name)
        tmp_path.replace(target)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision the checksum-verified open-source test ROM for mgba-live-mcp."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Download and verify the pinned ucity.gbc test ROM.")
    p_fetch.add_argument("--out", type=Path, default=None, help="Optional target path.")
    p_fetch.add_argument("--timeout", type=float, default=30.0, help="Download timeout in seconds.")

    p_verify = sub.add_parser("verify", help="Verify the local test ROM checksum.")
    p_verify.add_argument("--path", type=Path, default=None, help="Optional path to verify.")

    sub.add_parser("path", help="Print the default repo-local ROM path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "fetch":
        print(fetch_test_rom(out=args.out, timeout=args.timeout))
        return 0
    if args.cmd == "verify":
        target = default_test_rom_path() if args.path is None else args.path
        print(verify_test_rom(target))
        return 0
    if args.cmd == "path":
        print(default_test_rom_path())
        return 0
    raise AssertionError(f"Unhandled command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
