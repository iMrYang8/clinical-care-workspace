"""Import the pinned synthetic evaluation pack into the local demo clinic."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlmodel import Session

from app.core.config import settings
from app.seed import seed_demo_data
from app.services.dataset_imports import import_evaluation_pack


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-root", type=Path, default=Path("/app/datasets/raw"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/app/datasets/manifests/evaluation-pack-v1.json"),
    )
    parser.add_argument("--synthea-limit", type=int, default=20)
    parser.add_argument("--aci-limit", type=int, default=10)
    parser.add_argument("--primock-limit", type=int, default=5)
    args = parser.parse_args()
    for name in ("synthea_limit", "aci_limit", "primock_limit"):
        if not 0 < getattr(args, name) <= 100:
            parser.error(f"--{name.replace('_', '-')} must be between 1 and 100")
    database_url = settings.MIGRATION_DATABASE_URL or settings.DATABASE_URL
    owner_engine = create_engine(str(database_url))
    try:
        with Session(owner_engine) as session:
            seed_demo_data(session)
            result = import_evaluation_pack(
                session,
                raw_root=args.raw_root.resolve(),
                manifest_path=args.manifest.resolve(),
                synthea_limit=args.synthea_limit,
                aci_limit=args.aci_limit,
                primock_limit=args.primock_limit,
            )
    finally:
        owner_engine.dispose()
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
