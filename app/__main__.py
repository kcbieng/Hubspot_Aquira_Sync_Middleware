from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "sync":
        parser = argparse.ArgumentParser(prog="python -m app sync")
        parser.add_argument("--whatif", action="store_true")
        parser.add_argument("--live", action="store_true")
        parser.add_argument("--entities", default="")
        parser.add_argument("--aquira-id", dest="aquira_id", default="")
        args = parser.parse_args(argv[1:])
        from app.db.repo import Repo
        from app.settings import get_settings
        from app.sync.orchestrator import SyncContext, SyncOrchestrator

        settings = get_settings()
        whatif = True if args.whatif else False if args.live else bool(settings.whatif)
        entities = [item.strip() for item in args.entities.split(",") if item.strip()] or None
        result = SyncOrchestrator().run(
            SyncContext(trigger="cli", whatif=whatif, entities=entities, aquira_id=args.aquira_id or None),
            repo=Repo(),
        )
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "success" else 1

    import uvicorn
    from app.main import app

    uvicorn.run(app, host="0.0.0.0", port=8080)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
