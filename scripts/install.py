#!/usr/bin/env python3
"""Install the built simple-talk binaries into a bin directory.

Cross-platform (copy + executable bit) so `make install` works on Linux,
macOS and Windows. Configuration comes from the environment so the
Makefile can forward PREFIX / BINDIR / DESTDIR / OUT without quoting
headaches.
"""

import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BINARIES = ("simple-talk", "simple-talk-server")


def target_bindir() -> Path:
    bindir = os.environ.get("ST_BINDIR")
    destdir = os.environ.get("ST_DESTDIR", "")
    if not bindir:
        prefix = os.environ.get("ST_PREFIX") or os.path.expanduser("~/.local")
        bindir = os.path.join(prefix, "bin")
    return Path(destdir + bindir) if destdir else Path(bindir)


def main() -> int:
    out = Path(os.environ.get("ST_OUT") or (ROOT / "dist"))
    bindir = target_bindir()
    suffix = ".exe" if os.name == "nt" else ""
    uninstall = "--uninstall" in sys.argv[1:]

    if uninstall:
        removed = 0
        for name in BINARIES:
            path = bindir / (name + suffix)
            if path.exists():
                path.unlink()
                removed += 1
                print(f"removed {path}")
            else:
                print(f"not installed: {path}")
        return 0 if removed else 0

    bindir.mkdir(parents=True, exist_ok=True)

    installed = []
    for name in BINARIES:
        src = out / (name + suffix)
        if not src.exists():
            print(f"skip {name}: not built ({src}); run 'make' first", file=sys.stderr)
            continue
        dst = bindir / (name + suffix)
        shutil.copy2(src, dst)
        if os.name != "nt":
            dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        installed.append(dst)
        print(f"installed {src.name} -> {dst}")

    if not installed:
        return 1

    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if str(bindir) not in path_dirs:
        print(f"note: {bindir} is not on your PATH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())