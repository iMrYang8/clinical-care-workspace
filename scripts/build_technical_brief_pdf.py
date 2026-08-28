#!/usr/bin/env python3
"""Build the fixed three-page Nightingale technical brief PDF."""

from __future__ import annotations

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
from reportlab.platypus import Paragraph, Table, TableStyle

from validate_release_evidence import (
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


VALIDATED_EVIDENCE = validate_release_evidence(EVIDENCE_ROOT)
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

PAGE_W, PAGE_H = A4
MARGIN = 34

PAPER = colors.HexColor("#F8F5EE")
WHITE = colors.HexColor("#FFFFFF")
INK = colors.HexColor("#183247")
MUTED = colors.HexColor("#5C7180")
LINE = colors.HexColor("#D7E0DE")
TEAL = colors.HexColor("#0F7A70")
TEAL_SOFT = colors.HexColor("#E6F3F0")
BLUE = colors.HexColor("#3369E8")
BLUE_SOFT = colors.HexColor("#EAF0FD")
VIOLET = colors.HexColor("#7652E8")
VIOLET_SOFT = colors.HexColor("#F0ECFD")
AMBER = colors.HexColor("#B96D08")
AMBER_SOFT = colors.HexColor("#FBF0DC")
RED = colors.HexColor("#C94A5A")
RED_SOFT = colors.HexColor("#FBEAEC")


def register_fonts() -> None:
    font_dir = Path("/System/Library/Fonts/Supplemental")
    choices = {
        "Body": font_dir / "Arial.ttf",
        "BodyBold": font_dir / "Arial Bold.ttf",
        "BodyItalic": font_dir / "Arial Italic.ttf",
        "Display": font_dir / "Georgia.ttf",
        "DisplayBold": font_dir / "Georgia Bold.ttf",
    }
    for name, path in choices.items():
        if path.exists():
            pdfmetrics.registerFont(TTFont(name, str(path)))
        else:
            fallback = {
                "Body": "Helvetica",
                "BodyBold": "Helvetica-Bold",
                "BodyItalic": "Helvetica-Oblique",
                "Display": "Times-Roman",
                "DisplayBold": "Times-Bold",
            }[name]
            pdfmetrics.registerFontAlias(name, fallback)


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
SMALL = style(7.2, 9.0, MUTED)
TINY = style(6.4, 8.0, MUTED)
CARD_TITLE = style(9.5, 11.2, INK, "BodyBold")
TABLE_HEAD = style(7.2, 8.4, WHITE, "BodyBold")
TABLE_BODY = style(7.1, 8.6, INK)


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
    c.roundRect(x, y, width, height, radius, fill=1, stroke=1)


def label(c: canvas.Canvas, text: str, x: float, y: float, color: colors.Color) -> None:
    width = c.stringWidth(text, "BodyBold", 7.0) + 16
    c.setFillColor(color)
    c.roundRect(x, y, width, 17, 8.5, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont("BodyBold", 7.0)
    c.drawString(x + 8, y + 5, text)


def page_frame(c: canvas.Canvas, section: str, page_number: int) -> None:
    c.setFillColor(PAPER)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.setFillColor(INK)
    c.setFont("BodyBold", 8.5)
    c.drawString(MARGIN, PAGE_H - 30, "NIGHTINGALE")
    c.setFillColor(MUTED)
    c.setFont("Body", 7.6)
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - 30, section.upper())
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(MARGIN, PAGE_H - 39, PAGE_W - MARGIN, PAGE_H - 39)
    c.setFont("Body", 7.0)
    c.setFillColor(MUTED)
    c.drawString(
        MARGIN,
        24,
        f"Synthetic data only  |  Candidate snapshot: {CANDIDATE_SHORT}  |  {VERIFY_DATE}",
    )
    c.drawRightString(PAGE_W - MARGIN, 24, f"{page_number} / 3")


def page_title(c: canvas.Canvas, eyebrow: str, title: str, subtitle: str) -> None:
    c.setFillColor(TEAL)
    c.setFont("BodyBold", 7.5)
    c.drawString(MARGIN, PAGE_H - 65, eyebrow.upper())
    c.setFillColor(INK)
    c.setFont("DisplayBold", 24)
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


def metric_card(
    c: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    value: str,
    caption: str,
    accent: colors.Color,
) -> None:
    rounded_card(c, x, y, width, 72, WHITE, LINE, 9)
    c.setFillColor(accent)
    c.roundRect(x + 9, y + 54, 26, 4, 2, fill=1, stroke=0)
    c.setFillColor(INK)
    c.setFont("DisplayBold", 15)
    c.drawString(x + 9, y + 34, value)
    draw_paragraph(c, caption, x + 9, y + 28, width - 18, SMALL, 30)


def draw_page_one(c: canvas.Canvas, architecture_png: Path) -> None:
    page_frame(c, "Product and architecture", 1)
    page_title(
        c,
        "72-hour build candidate",
        "Evidence before summary",
        "A clinic-scoped care-note workspace where every high-value card can resolve to an immutable source.",
    )

    gap = 10
    card_w = (PAGE_W - 2 * MARGIN - gap) / 2
    rounded_card(c, MARGIN, 586, card_w, 102, TEAL_SOFT, colors.HexColor("#BADBD5"))
    label(c, "THE PROBLEM", MARGIN + 12, 659, TEAL)
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
    label(c, "THE RESPONSE", MARGIN + card_w + gap + 12, 659, VIOLET)
    draw_paragraph(
        c,
        "Precomputed Glance, immutable versions, exact-span provenance, bounded learning, patient-safe projections, and explicit review states.",
        MARGIN + card_w + gap + 12,
        649,
        card_w - 24,
        BODY_9,
    )

    draw_image_contain(c, architecture_png, MARGIN, 222, PAGE_W - 2 * MARGIN, 344, 4)

    values = [
        (
            BACKEND_PASSED,
            f"backend tests passed; {BACKEND_SKIPPED} skipped, {BACKEND_COVERAGE}% coverage",
            TEAL,
        ),
        (
            f"{BROWSER_PASSED} / {BROWSER_PASSED}",
            "Scenario A-F browser checks across three runs",
            BLUE,
        ),
        (
            f"{GLANCE_LATENCY['p95']:.3f} ms",
            f"Glance p95; {GLANCE_TARGET['card_count']}/{GLANCE_TARGET['expected_card_count']} expected cards",
            VIOLET,
        ),
        ("1 image", "same OCI artifact through production smoke", AMBER),
    ]
    metric_gap = 8
    metric_w = (PAGE_W - 2 * MARGIN - 3 * metric_gap) / 4
    for index, (value, caption, accent) in enumerate(values):
        metric_card(
            c,
            MARGIN + index * (metric_w + metric_gap),
            111,
            metric_w,
            value,
            caption,
            accent,
        )

    draw_paragraph(
        c,
        "The default checkout is offline and deterministic. External providers are optional boundaries, never prerequisites for the core demo.",
        MARGIN,
        98,
        PAGE_W - 2 * MARGIN,
        style(7.6, 9.2, MUTED, "BodyItalic", TA_CENTER),
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
    c.circle(x + 14, y + 39, 7, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont("BodyBold", 6.4)
    c.drawCentredString(x + 14, y + 37, number)
    c.setFillColor(INK)
    c.setFont("BodyBold", 7.7)
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
        "Defense in depth",
        "Trust is stored, not implied",
        "Tenant identity, role, immutable evidence, redaction state, and review status survive beyond the browser session.",
    )

    steps = [
        ("01", "SCOPE", "membership + role", TEAL),
        ("02", "SOURCE", "version or audio", BLUE),
        ("03", "REDACT", "fail closed", RED),
        ("04", "DERIVE", "fenced job", VIOLET),
        ("05", "REVIEW", "human decision", AMBER),
        ("06", "GLANCE", "precomputed", TEAL),
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
        "AES-256-GCM protects notes, comments, snapshots, redaction maps, transcripts, facts, and audio. HKDF derives clinic-specific keys and AAD binds context.",
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
        "Provider boundary",
        "Known aliases + SG recognizers + Presidio + residual scan. Errors or residual identifiers route to deterministic fallback and needs_review.",
        RED,
        RED_SOFT,
    )

    draw_image_contain(c, schema_png, MARGIN, 77, PAGE_W - 2 * MARGIN, 375, 4)

    draw_paragraph(
        c,
        "Central evidence chain: Patient > Entry > EntryVersion > exact span/hash. Voice adds audio asset and millisecond range. Patient and clinical Glance projections are stored together, then separated by role-safe API DTOs.",
        MARGIN,
        66,
        PAGE_W - 2 * MARGIN,
        style(7.5, 9.1, MUTED, "BodyItalic", TA_CENTER),
    )
    c.showPage()


def table_paragraph(text: str, bold: bool = False, white: bool = False) -> Paragraph:
    return Paragraph(
        text,
        TABLE_HEAD
        if bold and white
        else style(7.1, 8.6, INK, "BodyBold" if bold else "Body"),
    )


def scenario_card(
    c: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    height: float,
    letter: str,
    title: str,
    detail: str,
    accent: colors.Color,
) -> None:
    rounded_card(c, x, y, width, height, WHITE, LINE, 8)
    c.setFillColor(accent)
    c.roundRect(x + 8, y + height - 22, 18, 14, 7, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont("BodyBold", 7.0)
    c.drawCentredString(x + 17, y + height - 18, letter)
    c.setFillColor(INK)
    c.setFont("BodyBold", 8.2)
    c.drawString(x + 32, y + height - 19, title)
    draw_paragraph(c, detail, x + 9, y + height - 28, width - 18, TINY, height - 31)


def draw_page_three(c: canvas.Canvas) -> None:
    page_frame(c, "Verification and claim boundaries", 3)
    page_title(
        c,
        "Release confidence",
        "A reproducible candidate, with honest limits",
        "The gate tests the same source revision and OCI image across API, browser, worker, media, benchmark, and production topology.",
    )

    evidence_rows = [
        ["Gate", "Result", "What it establishes"],
        [
            "Backend",
            f"{BACKEND_PASSED} pass / {BACKEND_SKIPPED} skip",
            f"Ruff, format, mypy, ty, pytest, {BACKEND_COVERAGE}% coverage, Alembic roundtrip",
        ],
        [
            "Frontend",
            f"{FRONTEND_PASSED} / {FRONTEND_PASSED}",
            "Typecheck, Biome, Vitest, production build, tracked OpenAPI sync",
        ],
        [
            "Browser",
            f"{BROWSER_PASSED} / {BROWSER_PASSED}",
            f"{BROWSER_PER_RUN} Chromium tests x 3 over HTTPS, including Scenarios A-F",
        ],
        [
            "Glance",
            f"p95 {GLANCE_LATENCY['p95']:.3f} ms",
            f"Alex Synthetic, {GLANCE_TARGET['card_count']}/{GLANCE_TARGET['expected_card_count']} cards, 20 warmups + 100 measured reads",
        ],
        [
            "Container",
            f"FFmpeg {RELEASE['ffmpeg'].split('-')[0]}",
            "Exact Debian arm64 build/config retained; GPL-enabled build recorded",
        ],
        [
            "Artifact",
            f"sha256:{IMAGE_SHORT}...",
            "The verified image is promoted into a separate production topology",
        ],
    ]
    table_data: list[list[Paragraph]] = []
    for row_index, row in enumerate(evidence_rows):
        table_data.append(
            [
                table_paragraph(
                    cell, bold=(row_index == 0 or col_index < 2), white=row_index == 0
                )
                for col_index, cell in enumerate(row)
            ]
        )
    table = Table(
        table_data,
        colWidths=[105, 105, PAGE_W - 2 * MARGIN - 210],
        rowHeights=[22, 29, 29, 29, 29, 29, 29],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), INK),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("GRID", (0, 0), (-1, -1), 0.45, LINE),
                ("BACKGROUND", (0, 1), (-1, -1), WHITE),
                ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#F3F7F6")),
                ("BACKGROUND", (0, 4), (-1, 4), colors.HexColor("#F3F7F6")),
                ("BACKGROUND", (0, 6), (-1, 6), colors.HexColor("#F3F7F6")),
                ("BOX", (0, 0), (-1, -1), 0.8, INK),
            ]
        )
    )
    table.wrapOn(c, PAGE_W - 2 * MARGIN, 220)
    table.drawOn(c, MARGIN, 477)

    c.setFillColor(INK)
    c.setFont("BodyBold", 9.2)
    c.drawString(MARGIN, 460, "Six demonstration paths")
    scenarios = [
        ("A", "Evidence", "Glance card > exact immutable timeline span", TEAL),
        ("B", "Collaboration", "comment, mention, task, diff, revert, audit", BLUE),
        ("C", "Retention", "preview > archive > checksum-verified rehydrate", AMBER),
        ("D", "Concurrency", "deterministic 409 plus tenant-boundary checks", RED),
        (
            "E",
            "Patient safety",
            "network payload excludes raw AI and internal data",
            VIOLET,
        ),
        ("F", "Voice review", "encrypted recovery > fact > audio > publication", TEAL),
    ]
    scenario_gap_x = 8
    scenario_gap_y = 7
    scenario_w = (PAGE_W - 2 * MARGIN - scenario_gap_x) / 2
    scenario_h = 46
    for index, scenario in enumerate(scenarios):
        col = index % 2
        row = index // 2
        scenario_card(
            c,
            MARGIN + col * (scenario_w + scenario_gap_x),
            400 - row * (scenario_h + scenario_gap_y),
            scenario_w,
            scenario_h,
            *scenario,
        )

    c.setFillColor(INK)
    c.setFont("BodyBold", 9.2)
    c.drawString(MARGIN, 284, "Capability truth")
    truth_gap = 8
    truth_w = (PAGE_W - 2 * MARGIN - 2 * truth_gap) / 3
    trust_card(
        c,
        MARGIN,
        165,
        truth_w,
        105,
        "VERIFIED",
        "Deterministic text and voice fixtures, read-only Admin oversight, dataset-import contracts, encryption, versions, provenance, jobs, decay, browser flows, release image.",
        TEAL,
        TEAL_SOFT,
    )
    trust_card(
        c,
        MARGIN + truth_w + truth_gap,
        165,
        truth_w,
        105,
        "CONTRACT-TESTED",
        "OpenAI text, review, final-audio, and provisional live-transcription adapters use mocked transport and explicit error states. This validates contracts, not model quality.",
        VIOLET,
        VIOLET_SOFT,
    )
    trust_card(
        c,
        MARGIN + 2 * (truth_w + truth_gap),
        165,
        truth_w,
        105,
        "NOT LIVE VERIFIED",
        "OpenAI calls, HF model acquisition, faster-whisper runtime, pyannote diarization, remote registry, hosted deployment, and clinical validity.",
        RED,
        RED_SOFT,
    )

    rounded_card(
        c,
        MARGIN,
        78,
        PAGE_W - 2 * MARGIN,
        69,
        AMBER_SOFT,
        colors.HexColor("#E4C78C"),
        9,
    )
    c.setFillColor(AMBER)
    c.setFont("BodyBold", 8.8)
    c.drawString(MARGIN + 12, 129, "DELIVERY CONTENTS")
    draw_paragraph(
        c,
        "Runnable source + full Git history bundle + synthetic seed/importer + Scenario A-F runbook + English-captioned silent final demo + editable diagrams + machine-readable evidence + full notices + this brief. Remote publication remains an operator action if no authenticated GitHub session is available.",
        MARGIN + 12,
        120,
        PAGE_W - 2 * MARGIN - 24,
        BODY_8,
        42,
    )

    c.setFillColor(MUTED)
    c.setFont("BodyItalic", 7.2)
    c.drawCentredString(
        PAGE_W / 2,
        61,
        "This is a synthetic collaboration candidate, not a production EHR, medical device, or compliance certification.",
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

    pdf = canvas.Canvas(str(OUTPUT), pagesize=A4, pageCompression=1)
    pdf.setTitle("Nightingale Technical Brief")
    pdf.setAuthor("Nightingale contributors")
    pdf.setSubject(
        "Synthetic healthcare collaboration candidate architecture and evidence"
    )
    pdf.setKeywords(
        "Nightingale, FastAPI, clinical collaboration, provenance, synthetic data"
    )
    draw_page_one(pdf, architecture_png)
    draw_page_two(pdf, schema_png)
    draw_page_three(pdf)
    pdf.save()
    write_pdf_binding(OUTPUT, EVIDENCE_ROOT, VALIDATED_EVIDENCE)
    return OUTPUT


if __name__ == "__main__":
    print(build())
