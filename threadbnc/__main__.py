"""CLI: python -m threadbnc {serve|bouncer|archive URL|follow !name@host|sync}"""

from __future__ import annotations

import argparse
import logging
import os

from .bouncer import Bouncer
from .config import load_settings
from .db import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="threadbnc")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the archive UI (with the bouncer embedded by default)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    sub.add_parser("bouncer", help="run only the bouncer worker")
    a = sub.add_parser("archive", help="archive a post URL now")
    a.add_argument("url")
    f = sub.add_parser("follow", help="follow a community")
    f.add_argument("community")
    f.add_argument("--every", type=int, default=None, help="poll interval in minutes")
    f.add_argument("--keep-days", default=None, help="retention for auto-captured posts, or 'forever'")
    sub.add_parser("sync", help="run one bouncer pass and exit")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("THREADBNC_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    if args.cmd == "serve":
        import uvicorn

        from .web import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port, proxy_headers=True)
        return

    bouncer = Bouncer(Database(settings.db_path), settings)
    if args.cmd == "bouncer":
        bouncer.run_forever()
    elif args.cmd == "archive":
        print(f"thread {bouncer.ingest_url(args.url)}")
    elif args.cmd == "follow":
        days: int | None = -1
        if args.keep_days is not None:
            days = None if args.keep_days.lower() == "forever" else int(args.keep_days)
        print(f"community {bouncer.follow_community(args.community, args.every, days)}")
    elif args.cmd == "sync":
        bouncer.tick()


if __name__ == "__main__":
    main()
