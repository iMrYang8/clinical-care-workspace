from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from pathlib import Path

from sqlalchemy import create_engine
from sqlmodel import Session

from app.api.deps import RequestContext
from app.core.config import settings
from app.core.db import set_rls_clinic
from app.models import ClinicMembership, User
from app.seed import demo_id
from app.services.trust_evaluation import (
    run_fact_evaluation,
    run_redaction_evaluation,
    run_voice_evaluation,
)


def _commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "uncommitted"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run evidence-gated trust evaluations")
    parser.add_argument("suite", choices=["redaction", "voice", "facts", "all"])
    parser.add_argument("--raw-root", type=Path, default=Path("../datasets/raw"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("../datasets/manifests/evaluation-pack-v1.json"),
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("../artifacts/evaluation")
    )
    parser.add_argument("--transcribe-model", default="gpt-4o-transcribe-diarize")
    parser.add_argument("--extract-model", default="gpt-5.1")
    args = parser.parse_args()
    api_key = os.getenv("OPENAI_API_KEY", "")
    if args.suite in {"voice", "facts", "all"} and not api_key:
        parser.error("OPENAI_API_KEY must be set locally for real provider evaluation")
    engine = create_engine(
        str(settings.MIGRATION_DATABASE_URL or settings.DATABASE_URL)
    )
    with Session(engine) as session:
        # Evaluations run as the restricted application database role.  Select
        # the synthetic clinic before reading or writing tenant-scoped rows;
        # environment provisioning remains the responsibility of prestart.
        # This keeps the evaluator from attempting cross-clinic seed writes or
        # weakening RLS merely to persist calibration evidence.
        set_rls_clinic(session, demo_id("clinic-primary"))
        membership = session.get(ClinicMembership, demo_id("membership-clinician"))
        user = session.get(User, demo_id("user-clinician"))
        if membership is None or user is None:
            raise RuntimeError(
                "Synthetic clinician fixture missing; start the local environment "
                "and complete prestart before running evaluations"
            )
        context = RequestContext(
            user=user,
            membership=membership,
        )
        if args.suite in {"redaction", "all"}:
            run_redaction_evaluation(
                session, context=context, output_dir=args.output_dir
            )
        if args.suite in {"voice", "all"}:
            asyncio.run(
                run_voice_evaluation(
                    session,
                    context=context,
                    raw_root=args.raw_root.resolve(),
                    manifest_path=args.manifest.resolve(),
                    output_dir=args.output_dir.resolve(),
                    api_key=api_key,
                    model=args.transcribe_model,
                    code_commit=_commit(),
                )
            )
        if args.suite in {"facts", "all"}:
            asyncio.run(
                run_fact_evaluation(
                    session,
                    context=context,
                    raw_root=args.raw_root.resolve(),
                    manifest_path=args.manifest.resolve(),
                    output_dir=args.output_dir.resolve(),
                    api_key=api_key,
                    model=args.extract_model,
                    code_commit=_commit(),
                )
            )
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
