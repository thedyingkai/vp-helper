"""Queue an ICPC-style source listing for delayed printing in CUPS."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time


ICPC_PRINTFILE = Path("/usr/local/bin/printfile.icpc")


def _formatter(a2ps: str, filenames: list[str]) -> list[str]:
    # The ICPC shell script uses unquoted $*. Use its a2ps options directly
    # when a filename needs quoting, or when the workstation script is absent.
    if all(re.fullmatch(r"[A-Za-z0-9_./+-]+", name) for name in filenames):
        try:
            script = ICPC_PRINTFILE.read_text(encoding="ascii")
            commands = [
                line.strip()
                for line in script.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if commands == ["a2ps -1 --line-numbers=1 $*"] and os.access(ICPC_PRINTFILE, os.X_OK):
                return [str(ICPC_PRINTFILE)]
        except (OSError, UnicodeError):
            pass
    return [a2ps, "-1", "--line-numbers=1"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="printfile", description="Print source files after a random 1–4 minute delay.")
    parser.add_argument("-P", "--printer", metavar="QUEUE", help="use this CUPS queue instead of the default")
    parser.add_argument("files", metavar="FILE", nargs="+")
    args = parser.parse_args(argv)
    for filename in args.files:
        if not Path(filename).is_file():
            parser.error(f"not a regular file: {filename}")
    a2ps = shutil.which("a2ps")
    lp = shutil.which("lp")
    if not a2ps or not lp:
        print("printfile: Install a2ps and cups-client, then add a CUPS printer.", file=sys.stderr)
        return 1

    # The temporary directory has a simple name for the original ICPC script.
    title = ", ".join(Path(filename).name for filename in args.files)
    filenames = ["./-" if filename == "-" else filename for filename in args.files]
    try:
        with tempfile.TemporaryDirectory(prefix="vp-printfile-", dir="/tmp") as directory:
            listing = Path(directory) / "listing.ps"
            formatter = _formatter(a2ps, filenames)
            subprocess.run(
                [*formatter, "--quiet", "--delegate=no", f"--output={listing}", "--", *filenames],
                check=True,
            )
            with listing.open("rb") as output:
                if output.read(4) != b"%!PS":
                    raise OSError("a2ps did not produce a printable listing")

            now = time.time()
            release = random.randint(math.ceil(now + 60), math.floor(now + 240))
            due = datetime.fromtimestamp(release, timezone.utc)
            command = [lp, "-H", due.strftime("%H:%M:%S"), "-t", title, "-o", "job-sheets=none"]
            if args.printer:
                command.extend(("-d", args.printer))
            subprocess.run([*command, "--", str(listing)], check=True)
    except OSError as exc:
        print(f"printfile: {exc.strerror or str(exc)}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"printfile: {Path(exc.cmd[0]).name} exited with status {exc.returncode}", file=sys.stderr)
        return 1

    minutes, seconds = divmod(math.ceil(release - now), 60)
    print(f"Scheduled for {due.astimezone():%H:%M:%S} (in {minutes}m {seconds:02d}s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
