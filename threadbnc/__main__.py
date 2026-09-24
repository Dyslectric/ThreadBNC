"""CLI: python -m threadbnc {serve|bouncer|archive URL|follow !name@host|sync}"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .bouncer import Bouncer
from .config import load_settings
from .db import open_database


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
    f.add_argument("--poll", action="store_true",
                   help="check a Lemmy/PieFed community on a schedule (when your server can't get it pushed)")
    sub.add_parser("sync", help="run one bouncer pass and exit")
    check = sub.add_parser("integrity", help="check database relationships and archived media")
    check.add_argument("--deep", action="store_true", help="also hash every archived media file")
    export = sub.add_parser("export", help="write a credential-free portable archive ZIP")
    export.add_argument("output", type=Path)
    verify = sub.add_parser("verify-export", help="verify a portable archive ZIP and its checksums")
    verify.add_argument("archive", type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("THREADBNC_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "verify-export":
        from .portable import verify as verify_portable

        result = verify_portable(args.archive)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not result["ok"]:
            raise SystemExit(1)
        return

    settings = load_settings()

    if args.cmd == "serve":
        import uvicorn

        from .web import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port, proxy_headers=True)
        return

    if args.cmd in ("integrity", "export"):
        db = open_database(settings)
        media_dir = settings.media_dir or settings.data_dir / "media"
        if args.cmd == "integrity":
            from .integrity import check as check_integrity

            with db.connect() as conn:
                report = check_integrity(db, conn, media_dir, args.deep)
            print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
            if not report.ok:
                raise SystemExit(1)
        else:
            from .portable import create as create_portable

            manifest = create_portable(db, media_dir, args.output)
            print(json.dumps({"output": str(args.output.resolve()), **manifest}, indent=2, ensure_ascii=False))
        return

    bouncer = Bouncer(open_database(settings), settings)
    if args.cmd in ("bouncer", "sync"):
        from .inbox import attach as attach_inbox
        from .private import attach

        attach(bouncer, settings)
        attach_inbox(bouncer, settings)
    if args.cmd == "bouncer":
        bouncer.run_forever()
    elif args.cmd == "archive":
        print(f"thread {bouncer.ingest_url(args.url)}")
    elif args.cmd == "follow":
        days: int | None = -1
        if args.keep_days is not None:
            days = None if args.keep_days.lower() == "forever" else int(args.keep_days)
        print(f"community {bouncer.follow_community(args.community, args.every, days, polling=args.poll)}")
    elif args.cmd == "sync":
        bouncer.tick()


if __name__ == "__main__":
    main()
