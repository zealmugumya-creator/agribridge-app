#!/usr/bin/env python3
# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Record the built legal document versions in the database (service role).

Idempotent: re-running with unchanged documents is a no-op, and the database
refuses to alter a version that was already published (migration 0007). The API
also does this lazily on the first acceptance, so running this is optional but
recommended at deploy time so the publication is logged with who ran it.

Needs SUPABASE_URL and SUPABASE_KEY (service role) in the environment. Never
commit those. Usage:  python scripts/publish_legal.py [--actor <admin-uuid>]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.legal import LegalService  # noqa: E402
from services.supabase_client import SupabaseClient  # noqa: E402


def main(argv: list[str]) -> int:
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_KEY", "")
    if not (url and key):
        print("SUPABASE_URL and SUPABASE_KEY must be set", file=sys.stderr)
        return 2
    actor = argv[argv.index("--actor") + 1] if "--actor" in argv else None
    db = SupabaseClient(url, key)
    svc = LegalService(db)
    res = svc.ensure_published()
    if not res.ok:
        print(f"publish failed: {res.error}", file=sys.stderr)
        return 1
    log_rows = [{"doc_type": d["doc_type"], "version": d["version"], "actor": actor,
                 "action": "published", "note": "scripts/publish_legal.py"}
                for d in svc.manifest["documents"].values()]
    existing = {(r["doc_type"], r["version"]) for r in
                db.select("legal_publication_log", {"select": "doc_type,version"}, limit=1000)}
    fresh = [r for r in log_rows if (r["doc_type"], r["version"]) not in existing]
    if fresh and not db.insert("legal_publication_log", fresh).ok:
        print("versions published, but the publication log write failed", file=sys.stderr)
        return 1
    print(f"published {len(log_rows)} documents ({len(fresh)} newly logged)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
