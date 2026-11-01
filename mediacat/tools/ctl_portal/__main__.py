"""Run the MediaCat control panel:  .venv/bin/python -m tools.ctl_portal [--port N]"""

from __future__ import annotations

import argparse

import uvicorn

from tools.ctl_portal.app import DEFAULT_PORT, create_app


def main() -> None:
    p = argparse.ArgumentParser(prog="mediacat-ctl-web", description=__doc__)
    p.add_argument("--host", default="127.0.0.1", help="bind address (keep it loopback)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args()
    uvicorn.run(create_app(port=args.port), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
