"""praxdaily CLI — `praxdaily {serve, run-now, version}`.

The actual work happens in :mod:`praxdaily.app` (FastAPI app) and via
shell-out to ``prax`` for the ai-news-daily skill execution. This module
just routes argv and starts the local web server.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="praxdaily")
    parser.add_argument(
        "--version",
        action="version",
        version=f"praxdaily {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser(
        "serve",
        help="Start the local web panel and open it in the default browser",
    )
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=7878)
    p_serve.add_argument(
        "--no-open", action="store_true", help="Don't open the browser"
    )
    p_serve.add_argument(
        "--cwd",
        default=None,
        help="Project directory holding .prax/ (defaults to current dir)",
    )

    p_run = sub.add_parser(
        "run-now",
        help="Trigger the configured ai-news-daily job once and exit",
    )
    p_run.add_argument(
        "--cwd",
        default=None,
        help="Project directory holding .prax/ (defaults to current dir)",
    )

    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "serve":
        from .app import serve

        cwd = Path(args.cwd or Path.cwd()).resolve()
        url = f"http://{args.host}:{args.port}/"
        print(f"praxdaily {__version__} → {url}")
        print(f"workspace: {cwd}")
        print(f"(Ctrl+C to stop)")

        if not args.no_open:
            try:
                webbrowser.open(url)
            except Exception:
                pass

        serve(host=args.host, port=args.port, cwd=cwd)
        return

    if args.command == "run-now":
        from .runner import run_once

        cwd = Path(args.cwd or Path.cwd()).resolve()
        rc = run_once(cwd=cwd)
        sys.exit(rc)

    raise SystemExit(1)


if __name__ == "__main__":
    main()
