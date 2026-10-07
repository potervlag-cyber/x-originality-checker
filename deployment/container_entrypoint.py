"""Start the search service on the port assigned by a container platform."""
from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webcheck_server import main as server_main


def parse_port(settings=None):
    settings = os.environ if settings is None else settings
    value = settings.get("PORT", "8787")
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("invalid_port")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("invalid_port")
    return port


def main():
    try:
        port = parse_port()
    except ValueError:
        # Never include environment contents in configuration diagnostics.
        print("PORT 必须是 1–65535 的整数。", file=sys.stderr)
        return 2
    # make_server validates the explicit Host, Origin, and access token before
    # binding publicly. Do not infer trust from forwarded request headers.
    sys.argv = ["webcheck_server.py", "--host", "0.0.0.0", "--port", str(port)]
    server_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
