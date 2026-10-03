"""CLI: PIEFRONT_SERVER=piefed.example python -m piefront serve"""

from __future__ import annotations

import argparse
import logging
import os

from .config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="piefront")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the frontend")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("PIEFRONT_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port, proxy_headers=True)


if __name__ == "__main__":
    main()
