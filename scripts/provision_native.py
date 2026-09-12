"""Build a pinned Qt/Lua mGBA; dependencies must already be installed (see README)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

MGBA_COMMIT = "543a197582c30364584d773a974d7f991892fa43"
MGBA_REPOSITORY = "https://github.com/mgba-emu/mgba.git"
CMAKE_FLAGS = [
    "-DCMAKE_BUILD_TYPE=Release",
    "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
    "-DBUILD_QT=ON",
    "-DBUILD_SDL=OFF",
    "-DENABLE_SCRIPTING=ON",
    "-DUSE_LUA=5.4",
    "-DUSE_PNG=ON",
    "-DUSE_ZLIB=ON",
    "-DBUILD_GL=ON",
    "-DBUILD_GLES2=ON",
    "-DBUILD_GLES3=ON",
    "-DUSE_EPOXY=ON",
    "-DUSE_FFMPEG=OFF",
    "-DUSE_LIBZIP=OFF",
    "-DFORCE_QT_VERSION=5",
    "-DUSE_MINIZIP=OFF",
    "-DUSE_LZMA=OFF",
    "-DUSE_SQLITE3=ON",
    "-DUSE_ELF=OFF",
    "-DUSE_DISCORD_RPC=OFF",
    "-DUSE_FREETYPE=OFF",
    "-DUSE_JSON_C=OFF",
    "-DBUILD_LTO=OFF",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(".native/mgba"),
        help="New build directory; never reuses an unknown build or binary",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    commands = []

    def run(name: str, command: list[str], timeout: int = 120) -> str:
        commands.append(command)
        print(f"Native provisioning: {name}", flush=True)
        with (root / f"{name}.log").open("w") as log:
            subprocess.run(
                command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout
            )
        return (root / f"{name}.log").read_text()

    source, build = root / "source", root / "build"
    try:
        run("git-init", ["git", "init", str(source)])
        run(
            "git-fetch",
            ["git", "-C", str(source), "fetch", "--depth", "1", MGBA_REPOSITORY, MGBA_COMMIT],
        )
        run("git-checkout", ["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"])
        revision = run("revision", ["git", "-C", str(source), "rev-parse", "HEAD"]).strip()
        if revision != MGBA_COMMIT:
            raise RuntimeError(f"Unexpected mGBA revision: {revision}")
        # Linux AppStream generation requires a release tag, even for a commit build.
        release = "26b7884bc25a5933960f3cdcd98bac1ae14d42e2"
        run(
            "release-fetch",
            ["git", "-C", str(source), "fetch", "--depth", "1", MGBA_REPOSITORY, release],
        )
        run("release-tag", ["git", "-C", str(source), "tag", "0.10.5", release])
        run("configure", ["cmake", "-S", str(source), "-B", str(build), *CMAKE_FLAGS])
        flags = (build / "include/mgba/flags.h").read_text()
        for flag in ("ENABLE_SCRIPTING", "USE_LUA", "USE_PNG"):
            if f"#define {flag}" not in flags:
                raise RuntimeError(f"CMake disabled required capability: {flag}")
        run(
            "build",
            ["cmake", "--build", str(build), "--target", "mgba-qt", "--parallel", "2"],
            timeout=900,
        )
        binary = (
            build / "qt/mGBA.app/Contents/MacOS/mGBA"
            if platform.system() == "Darwin"
            else build / "qt/mgba-qt"
        )
        version = run("version", [str(binary), "--version"]).strip()
        if MGBA_COMMIT not in version:
            raise RuntimeError(f"Unexpected native version: {version}")
        help_text = run("help", [str(binary), "--help"])
        if "--script" not in help_text:
            raise RuntimeError("Built Qt frontend does not expose --script")
        for name in ("CMakeCache.txt", "include/mgba/flags.h", "version.c"):
            shutil.copyfile(build / name, root / Path(name).name)
        dependencies = run(
            "dependencies",
            ["otool", "-L", str(binary)] if platform.system() == "Darwin" else ["ldd", str(binary)],
        )
        if platform.system() == "Linux":
            run("os-packages", ["dpkg-query", "-W"])
            shutil.copyfile("/etc/os-release", root / "os-release")
        manifest = {
            "repository": MGBA_REPOSITORY,
            "commit": revision,
            "version": version,
            "binary": str(binary),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "commands": commands,
            "platform": platform.platform(),
            "dependencies": dependencies,
            "environment": {
                k: os.environ.get(k)
                for k in ("CC", "CXX", "SDKROOT", "CMAKE_PREFIX_PATH", "PKG_CONFIG_PATH")
            },
        }
        (root / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Native provisioning complete: {binary}", flush=True)
    finally:
        (root / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")


if __name__ == "__main__":
    main()
