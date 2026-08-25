#!/usr/bin/env python3
"""Apply deterministic whitespace normalization after OpenAPI generation."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "frontend" / "src" / "client"

for path in sorted(CLIENT.glob("*.ts")):
    normalized = (
        "\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n"
    )
    path.write_text(normalized)
