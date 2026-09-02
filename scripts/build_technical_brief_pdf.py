#!/usr/bin/env python3
"""Build the fixed three-page Nightingale technical brief PDF."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

from validate_release_evidence import (
    technical_brief_bound_artifacts,
    validate_release_evidence,
    write_pdf_binding,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path(
    os.environ.get(
        "NIGHTINGALE_PDF_OUTPUT",
        ROOT / "output" / "pdf" / "Nightingale_Technical_Brief.pdf",
    )
)
TMP = ROOT / "tmp" / "pdfs"
EVIDENCE_ROOT = Path(
    os.environ.get("NIGHTINGALE_EVIDENCE_DIR", ROOT / "docs" / "evidence")
)
EVALUATION_ROOT = Path(
    os.environ.get("NIGHTINGALE_EVALUATION_DIR", ROOT / "artifacts" / "evaluation")
)
EVALUATION_MANIFEST = ROOT / "datasets" / "manifests" / "evaluation-pack-v1.json"
DEMO_VIDEO = Path(
    os.environ.get(
        "NIGHTINGALE_DEMO_VIDEO",
        ROOT / "output" / "demo" / "Nightingale_Final_Demo_EN_Samantha.mp4",
    )
)
DEMO_METADATA_PATH = Path(
    os.environ.get(
        "NIGHTINGALE_DEMO_METADATA",
        ROOT / "output" / "demo" / "Nightingale_Final_Demo_EN_Samantha_metadata.json",
    )
)
DEMO_SHA256_PATH = Path(
    os.environ.get(
        "NIGHTINGALE_DEMO_SHA256",
        ROOT / "output" / "demo" / "Nightingale_Final_Demo_EN_Samantha_SHA256.txt",
    )
)
DEMO_SRT_PATH = Path(
    os.environ.get(
        "NIGHTINGALE_DEMO_SRT",
        ROOT / "output" / "demo" / "Nightingale_Final_Demo_EN.srt",
    )
)


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object: {path}")
    return payload


def load_sha256_manifest(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"invalid demo SHA-256 manifest: {path}") from exc
    manifest: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise ValueError(f"invalid demo SHA-256 manifest line: {line!r}")
        filename = parts[1].lstrip("* ")
        if filename in manifest:
            raise ValueError(f"duplicate demo SHA-256 manifest entry: {filename}")
        manifest[filename] = parts[0]
    return manifest


def load_demo_metadata() -> dict[str, object]:
    metadata = load_json_object(DEMO_METADATA_PATH, "demo metadata")
    if not DEMO_VIDEO.is_file() or DEMO_VIDEO.stat().st_size == 0:
        raise ValueError(f"final narrated demo is missing: {DEMO_VIDEO}")
    if not DEMO_SRT_PATH.is_file() or DEMO_SRT_PATH.stat().st_size == 0:
        raise ValueError(f"final demo SRT is missing: {DEMO_SRT_PATH}")

    video_sha256 = sha256_file(DEMO_VIDEO)
    srt_sha256 = sha256_file(DEMO_SRT_PATH)
    metadata_sha256 = sha256_file(DEMO_METADATA_PATH)
    if metadata.get("output_sha256") != video_sha256:
        raise ValueError("demo video SHA-256 does not match its metadata")
    if metadata.get("srt_sha256") != srt_sha256:
        raise ValueError("demo SRT SHA-256 does not match its metadata")

    sha_manifest = load_sha256_manifest(DEMO_SHA256_PATH)
    expected_manifest = {
        DEMO_VIDEO.name: video_sha256,
        DEMO_SRT_PATH.name: srt_sha256,
        DEMO_METADATA_PATH.name: metadata_sha256,
    }
    for filename, expected_sha256 in expected_manifest.items():
        if sha_manifest.get(filename) != expected_sha256:
            raise ValueError(
                f"demo SHA-256 manifest does not match {filename}: "
                f"{sha_manifest.get(filename)!r}"
            )

    expected = {
        "language": "en",
        "narration": True,
        "narration_voice": "Samantha",
        "narration_engine": "macOS say",
        "duration_seconds": 720.0,
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_sample_rate": 48_000,
        "audio_channels": 2,
        "output": DEMO_VIDEO.name,
        "srt": DEMO_SRT_PATH.name,
    }
    require_report_fields("demo metadata", metadata, expected)
    qa = metadata.get("qa")
    if not isinstance(qa, dict):
        raise ValueError("demo metadata does not contain a QA record")
    require_report_fields(
        "demo metadata QA",
        qa,
        {
            "cue_alignment": "passed",
            "duration": "passed",
            "one_h264_video_stream": "passed",
            "one_aac_audio_stream": "passed",
            "subtitle_track": "burned in source video",
        },
    )

    return metadata


def load_evaluation_report(
    filename: str,
    *,
    expected_provider: str | None = None,
    expected_model: str | None = None,
    expected_task: str | None = None,
) -> dict[str, object]:
    path = EVALUATION_ROOT / filename
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid evaluation report: {path}") from exc
    if not isinstance(report, dict):
        raise ValueError(f"evaluation report must be an object: {path}")
    expected = {
        "provider": expected_provider,
        "exact_model_id": expected_model,
        "task": expected_task,
    }
    for key, value in expected.items():
        if value is not None and report.get(key) != value:
            raise ValueError(
                f"{filename} {key} mismatch: expected {value!r}, "
                f"got {report.get(key)!r}"
            )
    return report


def require_report_fields(
    filename: str, report: dict[str, object], expected: dict[str, object]
) -> None:
    for key, value in expected.items():
        if report.get(key) != value:
            raise ValueError(
                f"{filename} {key} mismatch: expected {value!r}, "
                f"got {report.get(key)!r}"
            )


def require_metrics(
    filename: str, report: dict[str, object], expected: dict[str, object]
) -> dict[str, object]:
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError(f"{filename} must contain a metrics object")
    require_report_fields(f"{filename} metrics", metrics, expected)
    return metrics


VALIDATED_EVIDENCE = validate_release_evidence(EVIDENCE_ROOT)
FACT_EVALUATION = load_evaluation_report(
    "fact-calibration.json",
    expected_provider="openai",
    expected_model="gpt-5.1",
    expected_task="clinical_fact_extraction",
)
VOICE_EVALUATION = load_evaluation_report(
    "voice-calibration.json",
    expected_provider="openai",
    expected_model="gpt-4o-transcribe-diarize",
    expected_task="voice_transcription",
)
REDACTION_EVALUATION = load_evaluation_report("redaction-v2.json")
DEMO_METADATA = load_demo_metadata()
if not EVALUATION_MANIFEST.is_file():
    raise ValueError(f"evaluation manifest is missing: {EVALUATION_MANIFEST}")
EVALUATION_MANIFEST_SHA256 = sha256_file(EVALUATION_MANIFEST)
for filename, report, sample_count, consultation_count in (
    ("fact-calibration.json", FACT_EVALUATION, 176, 40),
    ("voice-calibration.json", VOICE_EVALUATION, 2206, 17),
):
    require_report_fields(
        filename,
        report,
        {
            "dataset_manifest_sha256": EVALUATION_MANIFEST_SHA256,
            "sample_count": sample_count,
            "consultation_count": consultation_count,
            "confidence_band": "low",
            "negative_results_are_preserved": True,
        },
    )
    require_metrics(filename, report, {"sample_count": sample_count})

require_report_fields(
    "redaction-v2.json",
    REDACTION_EVALUATION,
    {
        "redactor_version": "nightingale-redaction-v2",
        "dataset_sha256": (
            "36726d0bf3d2212869ef46b070c13329cc3148558cd34df7d1b50f7c673507ef"
        ),
        "sample_count": 500,
        "phi_recall": 1.0,
        "residual_phi_count": 0,
        "clinical_span_damage_count": 0,
        "passed": True,
    },
)
REDACTION_METRICS = require_metrics(
    "redaction-v2.json",
    REDACTION_EVALUATION,
    {
        "expected_phi_spans": 2500,
        "detected_phi_spans": 2500,
        "false_negatives": 0,
        "clinical_span_damage": 0,
    },
)
per_class = REDACTION_METRICS.get("per_class")
if not isinstance(per_class, dict) or set(per_class) != {
    "email",
    "mrn",
    "name",
    "nric_fin",
    "phone",
}:
    raise ValueError("redaction-v2.json has an unexpected PHI class set")
for label, raw_metrics in per_class.items():
    if not isinstance(raw_metrics, dict):
        raise ValueError(f"redaction-v2.json {label} metrics must be an object")
    require_report_fields(
        f"redaction-v2.json {label}",
        raw_metrics,
        {
            "true_positive": 500,
            "false_positive": 0,
            "false_negative": 0,
            "precision": 1.0,
            "recall": 1.0,
        },
    )

FACT_METRICS = FACT_EVALUATION["metrics"]
VOICE_METRICS = VOICE_EVALUATION["metrics"]
RELEASE = VALIDATED_EVIDENCE["release"]
BENCHMARK = VALIDATED_EVIDENCE["benchmark"]
CANDIDATE_SHA = RELEASE["source_commit"]
CANDIDATE_SHORT = CANDIDATE_SHA[:9]
IMAGE_ID = RELEASE["verified_backend_image_id"]
IMAGE_SHORT = IMAGE_ID.split(":", 1)[-1][:12]
VERIFY_DATE = RELEASE["verification_date_utc"]
BACKEND_MATCH = re.fullmatch(
    r"(\d+)_passed_(\d+)_skipped_coverage_(\d+)_percent",
    RELEASE["backend"],
)
if not BACKEND_MATCH:
    raise ValueError("release-candidate backend result has an unexpected format")
BACKEND_PASSED, BACKEND_SKIPPED, BACKEND_COVERAGE = BACKEND_MATCH.groups()
FRONTEND_PASSED = RELEASE["frontend_unit"].split("_", 1)[0]
BROWSER_PASSED = RELEASE["playwright_scenarios_a_to_f_repeat_3"].split("_", 1)[0]
BROWSER_PER_RUN = int(BROWSER_PASSED) // 3
GLANCE_LATENCY = BENCHMARK["latency_ms"]
GLANCE_TARGET = BENCHMARK["target"]
GLANCE_CONFIG = BENCHMARK["config"]
DEMO_DURATION_SECONDS = int(DEMO_METADATA["duration_seconds"])
DEMO_DURATION_LABEL = f"{DEMO_DURATION_SECONDS // 60}:{DEMO_DURATION_SECONDS % 60:02d}"
DEMO_VOICE = str(DEMO_METADATA["narration_voice"])
DEMO_FILE_SHA256 = str(DEMO_METADATA["output_sha256"])
DEMO_FILE_SHA256_SHORT = DEMO_FILE_SHA256[:12]

PAGE_W, PAGE_H = A4
MARGIN = 34

WHITE = colors.HexColor("#FFFFFF")
PAPER = WHITE
INK = colors.HexColor("#1A1A1A")
MUTED = colors.HexColor("#4D4D4D")
LINE = colors.HexColor("#B8B8B8")
TEAL = colors.HexColor("#0B6F66")
TEAL_SOFT = WHITE
BLUE = colors.HexColor("#2459C4")
BLUE_SOFT = WHITE
VIOLET = colors.HexColor("#6542C7")
VIOLET_SOFT = WHITE
AMBER = colors.HexColor("#9A5A00")
AMBER_SOFT = WHITE
RED = colors.HexColor("#B33F4E")
RED_SOFT = WHITE


def register_fonts() -> None:
    font_dir = Path("/System/Library/Fonts/Supplemental")
    choices = {
        "Body": font_dir / "Times New Roman.ttf",
        "BodyBold": font_dir / "Times New Roman Bold.ttf",
        "BodyItalic": font_dir / "Times New Roman Italic.ttf",
        "BodyBoldItalic": font_dir / "Times New Roman Bold Italic.ttf",
        "Display": font_dir / "Times New Roman.ttf",
        "DisplayBold": font_dir / "Times New Roman Bold.ttf",
    }
    missing = [str(path) for path in choices.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Times New Roman font files are required: " + ", ".join(sorted(missing))
        )
    for name, path in choices.items():
        pdfmetrics.registerFont(TTFont(name, str(path)))
    pdfmetrics.registerFontFamily(
        "Body",
        normal="Body",
        bold="BodyBold",
        italic="BodyItalic",
        boldItalic="BodyBoldItalic",
    )
    pdfmetrics.registerFontFamily(
        "Display",
        normal="Display",
        bold="DisplayBold",
        italic="BodyItalic",
        boldItalic="BodyBoldItalic",
    )


def style(
    size: float,
    leading: float | None = None,
    color: colors.Color = INK,
    font: str = "Body",
    align: int = TA_LEFT,
) -> ParagraphStyle:
    return ParagraphStyle(
        name=f"s-{size}-{font}-{align}",
        fontName=font,
        fontSize=size,
        leading=leading or size * 1.28,
        textColor=color,
        alignment=align,
        spaceAfter=0,
        spaceBefore=0,
        allowWidows=0,
        allowOrphans=0,
    )


BODY_8 = style(8.1, 10.5, MUTED)
BODY_9 = style(9.0, 12.2, MUTED)
BODY_10 = style(10.0, 13.4, INK)
SMALL = style(8.0, 9.7, MUTED)
TINY = style(8.0, 9.2, MUTED)
CARD_TITLE = style(9.5, 11.2, INK, "BodyBold")


def draw_paragraph(
    c: canvas.Canvas,
    text: str,
    x: float,
    top: float,
    width: float,
    paragraph_style: ParagraphStyle,
    max_height: float = 500,
) -> float:
    paragraph = Paragraph(text, paragraph_style)
    _, height = paragraph.wrap(width, max_height)
    paragraph.drawOn(c, x, top - height)
    return top - height


def rounded_card(
    c: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    height: float,
    fill: colors.Color = WHITE,
    stroke: colors.Color = LINE,
    radius: float = 10,
) -> None:
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(0.8)
    c.rect(x, y, width, height, fill=1, stroke=1)


def label(c: canvas.Canvas, text: str, x: float, y: float, color: colors.Color) -> None:
    c.setFillColor(color)
    c.setFont("BodyBold", 8.0)
    c.drawString(x, y + 5, text)


def page_frame(c: canvas.Canvas, section: str, page_number: int) -> None:
    c.setFillColor(PAPER)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.setFillColor(INK)
    c.setFont("BodyBold", 8.5)
    c.drawString(MARGIN, PAGE_H - 30, "NIGHTINGALE TECHNICAL BRIEF")
    c.setFillColor(MUTED)
    c.setFont("Body", 8.0)
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - 30, section.upper())
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(MARGIN, PAGE_H - 39, PAGE_W - MARGIN, PAGE_H - 39)
    c.setFont("Body", 8.0)
    c.setFillColor(MUTED)
    c.drawString(
        MARGIN,
        24,
        f"Synthetic data only  |  Candidate snapshot: {CANDIDATE_SHORT}  |  {VERIFY_DATE}",
    )
    c.drawRightString(PAGE_W - MARGIN, 24, f"{page_number} / 3")


def page_title(c: canvas.Canvas, eyebrow: str, title: str, subtitle: str) -> None:
    c.setFillColor(TEAL)
    c.setFont("BodyBold", 8.2)
    c.drawString(MARGIN, PAGE_H - 65, eyebrow.upper())
    c.setFillColor(INK)
    c.setFont("DisplayBold", 23)
    c.drawString(MARGIN, PAGE_H - 94, title)
    draw_paragraph(c, subtitle, MARGIN, PAGE_H - 108, PAGE_W - 2 * MARGIN, BODY_9)


def render_svg(svg: Path, png: Path) -> None:
    configured_converter = os.environ.get("RSVG_CONVERT_BIN")
    converter = shutil.which(configured_converter or "rsvg-convert")
    if not converter:
        raise RuntimeError(
            "rsvg-convert is required; install librsvg or set RSVG_CONVERT_BIN"
        )
    if not Path(converter).is_file() or not os.access(converter, os.X_OK):
        raise RuntimeError(f"rsvg-convert is not executable: {converter}")
    png.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [converter, "-w", "2400", str(svg), "-o", str(png)],
        check=True,
    )


def draw_image_contain(
    c: canvas.Canvas,
    image_path: Path,
    x: float,
    y: float,
    width: float,
    height: float,
    padding: float = 6,
) -> None:
    rounded_card(c, x, y, width, height, WHITE, LINE, 12)
    image = ImageReader(str(image_path))
    iw, ih = image.getSize()
    available_w = width - 2 * padding
    available_h = height - 2 * padding
    scale = min(available_w / iw, available_h / ih)
    draw_w = iw * scale
    draw_h = ih * scale
    c.drawImage(
        image,
        x + (width - draw_w) / 2,
        y + (height - draw_h) / 2,
        draw_w,
        draw_h,
        preserveAspectRatio=True,
        mask="auto",
    )


def draw_page_one(c: canvas.Canvas, architecture_png: Path) -> None:
    page_frame(c, "Product and architecture", 1)
    page_title(
        c,
        "1. Product and architecture",
        "Evidence before summary",
        "A clinic-scoped care-note workspace where every high-value card can resolve to an immutable source.",
    )

    gap = 10
    card_w = (PAGE_W - 2 * MARGIN - gap) / 2
    rounded_card(c, MARGIN, 586, card_w, 102, TEAL_SOFT, colors.HexColor("#BADBD5"))
    label(c, "1.1  PROBLEM", MARGIN + 12, 659, TEAL)
    draw_paragraph(
        c,
        "Long records become risky when summaries hide their source, AI overwrites human notes, or a tenant and role exist only as UI labels.",
        MARGIN + 12,
        649,
        card_w - 24,
        BODY_9,
    )
    rounded_card(
        c,
        MARGIN + card_w + gap,
        586,
        card_w,
        102,
        VIOLET_SOFT,
        colors.HexColor("#D2C8F6"),
    )
    label(c, "1.2  DESIGN RESPONSE", MARGIN + card_w + gap + 12, 659, VIOLET)
    draw_paragraph(
        c,
        "Precomputed Glance, immutable versions, exact-span provenance, calibrated abstention, auditable importance feedback, and clinician-approved patient publication.",
        MARGIN + card_w + gap + 12,
        649,
        card_w - 24,
        BODY_9,
    )

    draw_image_contain(c, architecture_png, MARGIN, 222, PAGE_W - 2 * MARGIN, 344, 4)
    draw_paragraph(
        c,
        "<b>Figure 1.</b> Nightingale system architecture and trust boundaries.",
        MARGIN,
        216,
        PAGE_W - 2 * MARGIN,
        style(8.0, 9.4, INK, "Body", TA_CENTER),
    )

    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(MARGIN, 193, PAGE_W - MARGIN, 193)
    label(c, "1.3  ASSUMPTIONS AND TRADE-OFFS", MARGIN, 179, TEAL)
    draw_paragraph(
        c,
        "The architecture deliberately exchanges write cost, storage, and review time for predictable reading, auditability, and safer patient delivery.",
        MARGIN + 184,
        184,
        PAGE_W - 2 * MARGIN - 184,
        BODY_8,
        18,
    )

    tradeoff_gap = 8
    tradeoff_w = (PAGE_W - 2 * MARGIN - tradeoff_gap) / 2
    tradeoffs = [
        (
            "Precomputed Glance",
            "More transactional rebuild work in exchange for stable, fast reads during care review.",
            TEAL,
        ),
        (
            "Immutable versions",
            "More retained storage in exchange for exact provenance, diff, audit, and recovery.",
            BLUE,
        ),
        (
            "Fail-closed decisions",
            "More clinician review when evidence is weak in exchange for blocking unsafe patient output.",
            RED,
        ),
        (
            "Clinic-level learning",
            "Less individual personalization in exchange for bounded feedback without a hidden staff profile.",
            VIOLET,
        ),
    ]
    for index, (title, body, accent) in enumerate(tradeoffs):
        col = index % 2
        row = index // 2
        trust_card(
            c,
            MARGIN + col * (tradeoff_w + tradeoff_gap),
            108 - row * 57,
            tradeoff_w,
            49,
            title,
            body,
            accent,
            WHITE,
        )
    c.showPage()


def mini_step(
    c: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    number: str,
    title: str,
    detail: str,
    accent: colors.Color,
) -> None:
    rounded_card(c, x, y, width, 55, WHITE, LINE, 8)
    c.setFillColor(accent)
    c.rect(x + 7, y + 32, 14, 14, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont("BodyBold", 8.0)
    c.drawCentredString(x + 14, y + 35.5, number)
    c.setFillColor(INK)
    c.setFont("BodyBold", 8.0)
    c.drawString(x + 25, y + 36, title)
    draw_paragraph(c, detail, x + 9, y + 27, width - 18, TINY, 22)


def trust_card(
    c: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    height: float,
    title: str,
    body: str,
    accent: colors.Color,
    fill: colors.Color,
) -> None:
    rounded_card(c, x, y, width, height, fill, accent, 9)
    c.setFillColor(accent)
    c.setFont("BodyBold", 8.8)
    c.drawString(x + 11, y + height - 19, title)
    draw_paragraph(c, body, x + 11, y + height - 27, width - 22, SMALL, height - 31)


def draw_page_two(c: canvas.Canvas, schema_png: Path) -> None:
    page_frame(c, "Trust, privacy, and data", 2)
    page_title(
        c,
        "2. Trust, privacy, and data",
        "Trust is stored, not implied",
        "Tenant identity, role, immutable evidence, redaction state, and review status survive beyond the browser session.",
    )

    steps = [
        ("01", "SCOPE", "membership + role", TEAL),
        ("02", "SOURCE", "version or audio", BLUE),
        ("03", "REDACT", "evaluated + fail closed", RED),
        ("04", "ASSESS", "support + risk floor", VIOLET),
        ("05", "REVIEW", "abstain / correct / approve", AMBER),
        ("06", "PROJECT", "Glance + patient receipt", TEAL),
    ]
    step_gap = 6
    step_w = (PAGE_W - 2 * MARGIN - 5 * step_gap) / 6
    for index, (number, title, detail, accent) in enumerate(steps):
        mini_step(
            c,
            MARGIN + index * (step_w + step_gap),
            626,
            step_w,
            number,
            title,
            detail,
            accent,
        )
        if index < len(steps) - 1:
            c.setStrokeColor(LINE)
            c.setLineWidth(1.2)
            x1 = MARGIN + (index + 1) * step_w + index * step_gap
            c.line(x1 + 1, 653, x1 + step_gap - 1, 653)

    trust_gap = 9
    trust_w = (PAGE_W - 2 * MARGIN - trust_gap) / 2
    trust_card(
        c,
        MARGIN,
        544,
        trust_w,
        69,
        "Tenant and role",
        "The server ignores client clinic, actor, and role authority. Composite foreign keys plus a non-owner NOBYPASSRLS runtime role enforce clinic scope.",
        TEAL,
        TEAL_SOFT,
    )
    trust_card(
        c,
        MARGIN + trust_w + trust_gap,
        544,
        trust_w,
        69,
        "Encrypted payloads",
        "AES-256-GCM protects notes, patient identifiers, clinic API keys, comments, snapshots, redaction maps, transcripts, facts, and audio. AAD binds clinic context.",
        BLUE,
        BLUE_SOFT,
    )
    trust_card(
        c,
        MARGIN,
        466,
        trust_w,
        69,
        "Browser boundary",
        "Secure HttpOnly SameSite cookie, Origin checks, no-store PHI responses, short-poll SSE reauthorization, and bounded IndexedDB cleanup.",
        VIOLET,
        VIOLET_SOFT,
    )
    trust_card(
        c,
        MARGIN + trust_w + trust_gap,
        466,
        trust_w,
        69,
        "Decision and provider boundary",
        "Deterministic risk floors cannot be lowered. Redaction and exact-model calibration must match; Low or unavailable confidence abstains and patient sharing stays blocked.",
        RED,
        RED_SOFT,
    )

    draw_image_contain(c, schema_png, MARGIN, 77, PAGE_W - 2 * MARGIN, 375, 4)

    draw_paragraph(
        c,
        "<b>Figure 2.</b> Core clinical evidence schema. Patient &gt; Entry &gt; immutable EntryVersion &gt; exact span/hash &gt; DecisionAssessment; voice adds an audio/time anchor, and publication adds a clinician approval receipt.",
        MARGIN,
        66,
        PAGE_W - 2 * MARGIN,
        style(8.0, 9.4, MUTED, "BodyItalic", TA_CENTER),
    )
    c.showPage()


def scenario_item(
    c: canvas.Canvas,
    x: float,
    top: float,
    width: float,
    letter: str,
    title: str,
    detail: str,
    accent: colors.Color,
) -> None:
    c.setFillColor(accent)
    c.rect(x, top - 15, 18, 15, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont("BodyBold", 8.0)
    c.drawCentredString(x + 9, top - 11, letter)
    c.setFillColor(INK)
    c.setFont("BodyBold", 8.7)
    c.drawString(x + 28, top - 11, title)
    draw_paragraph(
        c,
        detail,
        x,
        top - 22,
        width,
        style(8.0, 9.5, MUTED),
        18,
    )
    c.setStrokeColor(LINE)
    c.setLineWidth(0.45)
    c.line(x, top - 38, x + width, top - 38)


def capability_row(
    c: canvas.Canvas,
    top: float,
    title: str,
    body: str,
    accent: colors.Color,
) -> None:
    content_width = PAGE_W - 2 * MARGIN
    title_width = 145
    c.setStrokeColor(LINE)
    c.setLineWidth(0.45)
    c.line(MARGIN, top, PAGE_W - MARGIN, top)
    c.setStrokeColor(accent)
    c.setLineWidth(2.0)
    c.line(MARGIN, top - 5, MARGIN, top - 35)
    draw_paragraph(
        c,
        title,
        MARGIN + 10,
        top - 5,
        title_width - 16,
        style(8.6, 10.0, accent, "BodyBold"),
        30,
    )
    draw_paragraph(
        c,
        body,
        MARGIN + title_width,
        top - 5,
        content_width - title_width,
        style(8.0, 9.5, MUTED),
        32,
    )
    c.setStrokeColor(LINE)
    c.setLineWidth(0.45)
    c.line(MARGIN, top - 40, PAGE_W - MARGIN, top - 40)


def compact_card(
    c: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    height: float,
    title: str,
    body: str,
    accent: colors.Color,
) -> None:
    rounded_card(c, x, y, width, height, WHITE, LINE, 7)
    c.setFillColor(accent)
    c.rect(x, y, 3, height, fill=1, stroke=0)
    draw_paragraph(
        c,
        title,
        x + 9,
        y + height - 7,
        width - 18,
        style(8.2, 9.4, accent, "BodyBold"),
        14,
    )
    draw_paragraph(
        c, body, x + 9, y + height - 20, width - 18, style(8.0, 9.2, MUTED), height - 23
    )


def decision_matrix(c: canvas.Canvas, top: float) -> None:
    x = MARGIN
    width = PAGE_W - 2 * MARGIN
    # Give the signal names enough room to remain whole at the document's
    # 8 pt readability floor (rather than splitting CONFIDENCE/IMPORTANCE).
    columns = (76, 131, 151, width - 358)
    headers = ("SIGNAL", "WHAT IS IT?", "HOW COULD IT BE WRONG?", "WHAT HAPPENS THEN?")
    rows = (
        (
            "RISK",
            "max(rule floor, model proposal), with rule IDs and version stored.",
            "Inspect triggered rules and exact-source conflicts; regression-test allergy, status, dose, route, and frequency.",
            "The model cannot lower the floor. High/Critical stays visible, requires review, and blocks sharing.",
            RED,
        ),
        (
            "CONFIDENCE",
            "Holdout lower-bound band bound to provider, exact model, task, parameters, dataset hash, and expiry.",
            "Missing, mismatched, expired, undersized, or weak evaluation evidence makes it Unavailable.",
            "Low/Unavailable AI abstains and is not publishable; only supported High/Medium evidence may enter clinician approval.",
            VIOLET,
        ),
        (
            "IMPORTANCE",
            "Bounded clinic-level rank from recency, open work, confirmation, risk, and explicit reasoned feedback.",
            "Audit visible impressions, exposure probability, and feedback reasons; no click is not negative feedback.",
            "Critical, unresolved, and confirmed items stay protected; top four are deterministic and slot five explores at most 10%.",
            TEAL,
        ),
    )
    header_h, row_h = 22, 46
    total_h = header_h + len(rows) * row_h
    bottom = top - total_h
    rounded_card(c, x, bottom, width, total_h, WHITE, LINE, 6)
    cursor = x
    for index, (header, col_w) in enumerate(zip(headers, columns, strict=True)):
        if index:
            c.setStrokeColor(LINE)
            c.line(cursor, bottom, cursor, top)
        draw_paragraph(
            c,
            header,
            cursor + 5,
            top - 6,
            col_w - 10,
            style(8.0, 9.0, INK, "BodyBold"),
            15,
        )
        cursor += col_w
    c.setStrokeColor(LINE)
    c.line(x, top - header_h, x + width, top - header_h)
    for row_index, row in enumerate(rows):
        row_top = top - header_h - row_index * row_h
        if row_index:
            c.line(x, row_top, x + width, row_top)
        cursor = x
        values = row[:4]
        for col_index, (value, col_w) in enumerate(zip(values, columns, strict=True)):
            cell_style = style(
                8.0,
                9.1,
                row[4] if col_index == 0 else MUTED,
                "BodyBold" if col_index == 0 else "Body",
            )
            draw_paragraph(
                c, value, cursor + 5, row_top - 6, col_w - 10, cell_style, row_h - 10
            )
            cursor += col_w


def draw_page_three(c: canvas.Canvas) -> None:
    page_frame(c, "Verification and claim boundaries", 3)
    page_title(
        c,
        "3. Verification and claim boundaries",
        "A reproducible candidate, with honest limits",
        "The gate tests the same source revision and OCI image across API, browser, worker, media, benchmark, and production topology.",
    )

    c.setStrokeColor(LINE)
    c.line(MARGIN, 690, PAGE_W - MARGIN, 690)
    label(c, "3.1  RELEASE-GATE EVIDENCE", MARGIN, 670, TEAL)
    draw_paragraph(
        c,
        (
            f"<b>One bound candidate.</b> Backend: {BACKEND_PASSED} passed, {BACKEND_SKIPPED} skipped, {BACKEND_COVERAGE}% coverage plus static checks and Alembic roundtrip. "
            f"Frontend gates passed typecheck, Biome, production build, and tracked OpenAPI generation, together with {FRONTEND_PASSED}/{FRONTEND_PASSED} Vitest tests. "
            f"Playwright: {BROWSER_PASSED}/{BROWSER_PASSED} HTTPS checks ({BROWSER_PER_RUN} per run x 3). Glance: {GLANCE_TARGET['card_count']}/{GLANCE_TARGET['expected_card_count']} cards, local warm-read p95 {GLANCE_LATENCY['p95']:.3f} ms. "
            f"Revision {CANDIDATE_SHORT} promoted OCI sha256:{IMAGE_SHORT}... unchanged into the production-topology smoke test; this is not a hosted-latency or clinical-performance claim."
        ),
        MARGIN,
        657,
        PAGE_W - 2 * MARGIN,
        style(8.2, 9.8, MUTED),
        64,
    )

    label(c, "3.2  DECISION INTEGRITY", MARGIN, 583, TEAL)
    decision_matrix(c, 572)

    label(c, "3.3  BOUNDED LEARNING AND RETENTION", MARGIN, 395, TEAL)
    bonus_gap = 9
    bonus_w = (PAGE_W - 2 * MARGIN - bonus_gap) / 2
    compact_card(
        c,
        MARGIN,
        325,
        bonus_w,
        60,
        "Self-learning: logged, not personal",
        "importance_impressions stores a de-duplicated view_event_id only after at least 50% visibility for at least 2 s, plus exposure probability and explicit feedback reason. Only Not relevant/Outdated changes non-protected ranking; no per-user behavior model.",
        TEAL,
    )
    compact_card(
        c,
        MARGIN + bonus_w + bonus_gap,
        325,
        bonus_w,
        60,
        "Data decay: reversible archive",
        "Pinned, open, confirmed, and unresolved-conflict records are protected. Eligible bodies move to AES-GCM archive; checksum, immutable versions, provenance, and audit remain, and authorized rehydrate verifies integrity.",
        AMBER,
    )

    label(c, "3.4  SCENARIO A-F PATHS", MARGIN, 309, TEAL)
    scenario_specs = (
        ("A  Evidence", "Glance -> exact immutable source", TEAL),
        ("B  Collaboration", "selection comment -> mention/task -> diff/restore", BLUE),
        ("C  Retention", "archive preview -> checksum -> rehydrate", AMBER),
        ("D  Concurrency", "stale edit -> 409; tenant boundary", RED),
        ("E  Patient safety", "request -> approval -> receipt/withdrawal", VIOLET),
        ("F  Voice review", "speaker/time source -> reviewed fact", TEAL),
    )
    scenario_gap = 7
    scenario_w = (PAGE_W - 2 * MARGIN - 2 * scenario_gap) / 3
    for index, (title, body, accent) in enumerate(scenario_specs):
        compact_card(
            c,
            MARGIN + (index % 3) * (scenario_w + scenario_gap),
            260 - (index // 3) * 43,
            scenario_w,
            37,
            title,
            body,
            accent,
        )

    truth_gap = 7
    truth_w = (PAGE_W - 2 * MARGIN - 2 * truth_gap) / 3
    compact_card(
        c,
        MARGIN,
        157,
        truth_w,
        52,
        "VERIFIED",
        "Deterministic controls, exact provenance, versioning, abstention, publication gates, browser paths, and the promoted image.",
        TEAL,
    )
    compact_card(
        c,
        MARGIN + truth_w + truth_gap,
        157,
        truth_w,
        52,
        "MEASURED - LOW",
        f"OpenAI on mock/synthetic holdouts: facts accuracy {float(FACT_METRICS['accuracy']):.3f}; voice WER {float(VOICE_METRICS['wer']):.3f}. Low means review/abstain, not publication.",
        VIOLET,
    )
    compact_card(
        c,
        MARGIN + 2 * (truth_w + truth_gap),
        157,
        truth_w,
        52,
        "NOT CLINICALLY VALIDATED",
        f"Redaction passed {int(REDACTION_EVALUATION['sample_count'])} synthetic cases; unseen-data safety, compliance, hosted performance, and clinical validity remain unproven.",
        RED,
    )

    index_gap = 9
    index_left_w = 230
    index_right_w = PAGE_W - 2 * MARGIN - index_gap - index_left_w
    compact_card(
        c,
        MARGIN,
        58,
        index_left_w,
        88,
        "EVIDENCE INDEX (repository paths)",
        "docs/evidence/release-candidate.txt<br/>docs/evidence/glance-benchmark.json<br/>docs/evidence/ffmpeg-container-version.txt<br/>artifacts/evaluation/fact-calibration.json<br/>artifacts/evaluation/voice-calibration.json<br/>artifacts/evaluation/redaction-v2.json",
        BLUE,
    )
    compact_card(
        c,
        MARGIN + index_left_w + index_gap,
        58,
        index_right_w,
        88,
        "DELIVERY FILE INDEX",
        f"Technical Brief (+ binding); narrated Demo MP4 (+ .en.srt)<br/>DEMO_RUNBOOK.md; editable architecture.drawio + schema.drawio<br/>ATTRIBUTION.txt; THIRD_PARTY_NOTICES.md;<br/>THIRD_PARTY_LICENSES/DISTRIBUTION_NOTICES.md<br/><b>Binding:</b> {CANDIDATE_SHORT} / sha256:{IMAGE_SHORT}...; demo sha256:{DEMO_FILE_SHA256_SHORT}...<br/>Synthetic collaboration release; not an EHR, medical device, or certification.",
        AMBER,
    )
    c.showPage()


def build() -> Path:
    register_fonts()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    architecture_png = TMP / "architecture.png"
    schema_png = TMP / "schema.png"
    render_svg(ROOT / "docs" / "architecture.svg", architecture_png)
    render_svg(ROOT / "docs" / "schema.svg", schema_png)

    pdf = canvas.Canvas(
        str(OUTPUT),
        pagesize=A4,
        pageCompression=1,
        initialFontName="Body",
        initialFontSize=12,
        initialLeading=14.4,
    )
    pdf.setTitle("Nightingale Technical Brief")
    pdf.setAuthor("Nightingale contributors")
    pdf.setSubject(
        "Clinic-scoped healthcare collaboration architecture and release evidence"
    )
    pdf.setKeywords(
        "Nightingale, FastAPI, clinical collaboration, provenance, synthetic data"
    )
    draw_page_one(pdf, architecture_png)
    draw_page_two(pdf, schema_png)
    draw_page_three(pdf)
    pdf.save()
    write_pdf_binding(
        OUTPUT,
        EVIDENCE_ROOT,
        VALIDATED_EVIDENCE,
        bound_artifacts=technical_brief_bound_artifacts(ROOT),
    )
    return OUTPUT


if __name__ == "__main__":
    print(build())
