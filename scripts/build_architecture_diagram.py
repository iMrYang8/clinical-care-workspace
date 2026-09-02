#!/usr/bin/env python3
"""Build the editable Nightingale architecture diagram and its SVG export.

The layout is intentionally hand-routed.  Every connector consists only of
horizontal and vertical segments, uses square corners, and stays out of every
non-endpoint node.  Keeping one declarative source for Draw.io and SVG avoids
the visual drift that previously occurred between the editable and rendered
artifacts.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
DRAWIO_PATH = ROOT / "docs" / "architecture.drawio"
SVG_PATH = ROOT / "docs" / "architecture.svg"

CANVAS_W = 1600
CANVAS_H = 1130
FONT = "Times New Roman"

WHITE = "#FFFFFF"
INK = "#17324D"
MUTED = "#607386"
TEAL = "#0F766E"
BLUE = "#2563EB"
VIOLET = "#7C3AED"
AMBER = "#B7791F"
RED = "#BE4B5A"
LINE = "#D7DEDB"


@dataclass(frozen=True)
class Box:
    id: str
    x: float
    y: float
    w: float
    h: float
    title: str
    lines: tuple[str, ...]
    color: str


@dataclass(frozen=True)
class Section:
    id: str
    x: float
    y: float
    w: float
    h: float
    title: str
    color: str


@dataclass(frozen=True)
class Edge:
    id: str
    source: str
    target: str
    points: tuple[tuple[float, float], ...]
    color: str
    label: str = ""
    label_box: tuple[float, float, float, float] | None = None
    dashed: bool = False
    exit_xy: tuple[float, float] | None = None
    entry_xy: tuple[float, float] | None = None


SECTIONS = (
    Section("exp", 44, 120, 1512, 130, "EXPERIENCE PLANE", "#D7DEDB"),
    Section("edge", 44, 330, 1512, 145, "TRUSTED EDGE & API", "#CADCE4"),
    Section("services", 44, 500, 1512, 260, "DOMAIN SERVICES", "#DCD4EB"),
    Section("infra", 44, 820, 1512, 250, "DATA, WORKERS & PROVIDERS", "#D3DDD6"),
)


BOXES = (
    Box(
        "clinician",
        70,
        160,
        330,
        70,
        "Clinician workspace",
        ("Care Note · Review Mode · provenance",),
        TEAL,
    ),
    Box(
        "patient_ui",
        425,
        160,
        330,
        70,
        "Patient My Care",
        ("Patient-safe Glance · insight · voice",),
        BLUE,
    ),
    Box(
        "admin_ui",
        780,
        160,
        330,
        70,
        "Admin console",
        ("Membership lifecycle · metadata audit",),
        VIOLET,
    ),
    Box(
        "capture",
        1135,
        160,
        395,
        70,
        "Browser voice capture",
        ("MediaRecorder · WebCrypto · IndexedDB",),
        AMBER,
    ),
    Box(
        "traefik",
        70,
        375,
        330,
        70,
        "Traefik",
        ("Local TLS · loopback exposure",),
        BLUE,
    ),
    Box(
        "fastapi",
        440,
        365,
        590,
        90,
        "FastAPI /api/v1",
        (
            "Secure HttpOnly cookie · CSRF/Origin",
            "OpenAPI contract · no-store PHI",
        ),
        TEAL,
    ),
    Box(
        "scope",
        1070,
        375,
        220,
        70,
        "Trusted request scope",
        ("User + ClinicMembership + role",),
        VIOLET,
    ),
    Box(
        "sse",
        1320,
        375,
        210,
        70,
        "SSE event stream",
        ("Last-Event-ID · re-auth per poll",),
        AMBER,
    ),
    Box(
        "care",
        70,
        545,
        330,
        82,
        "Care record",
        ("Entries · immutable versions", "ETag / If-Match · diff · revert"),
        TEAL,
    ),
    Box(
        "collab",
        70,
        655,
        330,
        72,
        "Collaboration",
        ("Anchored comments · mentions · tasks",),
        BLUE,
    ),
    Box(
        "trust",
        430,
        545,
        330,
        82,
        "Trust & Glance",
        ("Highlights · risk reason · feedback", "Immutable span/hash provenance"),
        VIOLET,
    ),
    Box(
        "glance",
        430,
        655,
        330,
        72,
        "Precomputed Glance",
        ("Clinic-scoped top 5 · patient DTO",),
        AMBER,
    ),
    Box(
        "redaction",
        790,
        545,
        330,
        82,
        "Fail-closed redaction",
        ("Known aliases + SG recognizers", "Presidio + residual scan"),
        RED,
    ),
    Box(
        "ai",
        790,
        655,
        330,
        72,
        "AI extraction pipeline",
        ("Redact → extract → review → publish",),
        VIOLET,
    ),
    Box(
        "voice",
        1150,
        545,
        370,
        82,
        "Voice pipeline",
        ("Chunk seal barrier · FFmpeg", "transcript → facts → audio anchors"),
        AMBER,
    ),
    Box(
        "events",
        1150,
        655,
        370,
        72,
        "Jobs & domain events",
        ("Lease/retry/idempotency · audit outbox",),
        BLUE,
    ),
    Box(
        "postgres",
        70,
        870,
        360,
        120,
        "PostgreSQL 16",
        (
            "Restricted non-owner runtime role",
            "clinic_id RLS + composite FKs",
            "AES-256-GCM field envelopes",
        ),
        TEAL,
    ),
    Box(
        "worker",
        470,
        870,
        310,
        120,
        "Workers",
        (
            "Job claim / lease / attempts",
            "snapshot rebuild · decay",
            "voice finalization",
        ),
        BLUE,
    ),
    Box(
        "providers",
        840,
        870,
        340,
        120,
        "Provider boundary",
        (
            "Deterministic fixture (default CI)",
            "OpenAI text/audio adapters",
            "faster-whisper / pyannote gated",
        ),
        VIOLET,
    ),
    Box(
        "archive",
        1220,
        870,
        300,
        120,
        "Durable evidence",
        (
            "Audit + immutable provenance",
            "zstd + AES-GCM cold archive",
            "checksum + rehydrate",
        ),
        AMBER,
    ),
)


EDGES = (
    Edge(
        "a1",
        "clinician",
        "traefik",
        ((235, 230), (235, 258), (250, 258), (250, 375)),
        TEAL,
        exit_xy=(0.5, 1),
        entry_xy=(180 / 330, 0),
    ),
    Edge(
        "a2",
        "patient_ui",
        "traefik",
        ((590, 230), (590, 278), (295, 278), (295, 375)),
        BLUE,
        exit_xy=(0.5, 1),
        entry_xy=(225 / 330, 0),
    ),
    Edge(
        "a3",
        "admin_ui",
        "traefik",
        ((945, 230), (945, 298), (340, 298), (340, 375)),
        VIOLET,
        exit_xy=(0.5, 1),
        entry_xy=(270 / 330, 0),
    ),
    Edge(
        "a4",
        "capture",
        "traefik",
        ((1332.5, 230), (1332.5, 318), (385, 318), (385, 375)),
        AMBER,
        exit_xy=(0.5, 1),
        entry_xy=(315 / 330, 0),
    ),
    Edge(
        "a5",
        "traefik",
        "fastapi",
        ((400, 410), (440, 410)),
        BLUE,
        "TLS",
        (405, 397, 30, 15),
        exit_xy=(1, 0.5),
        entry_xy=(0, 0.5),
    ),
    Edge(
        "a6",
        "fastapi",
        "scope",
        ((1030, 410), (1070, 410)),
        TEAL,
        "derive",
        (1032, 397, 36, 15),
        exit_xy=(1, 0.5),
        entry_xy=(0, 0.5),
    ),
    Edge(
        "a7",
        "fastapi",
        "care",
        ((470, 455), (470, 480), (235, 480), (235, 545)),
        TEAL,
        exit_xy=(30 / 590, 1),
        entry_xy=(0.5, 0),
    ),
    Edge(
        "a8",
        "fastapi",
        "trust",
        ((595, 455), (595, 545)),
        VIOLET,
        exit_xy=(155 / 590, 1),
        entry_xy=(0.5, 0),
    ),
    Edge(
        "a9",
        "fastapi",
        "redaction",
        ((900, 455), (900, 480), (955, 480), (955, 545)),
        RED,
        exit_xy=(460 / 590, 1),
        entry_xy=(0.5, 0),
    ),
    Edge(
        "a10",
        "fastapi",
        "voice",
        ((1015, 455), (1015, 490), (1335, 490), (1335, 545)),
        AMBER,
        exit_xy=(575 / 590, 1),
        entry_xy=(0.5, 0),
    ),
    Edge(
        "a11",
        "care",
        "collab",
        ((235, 627), (235, 655)),
        BLUE,
        exit_xy=(0.5, 1),
        entry_xy=(0.5, 0),
    ),
    Edge(
        "a12",
        "trust",
        "glance",
        ((595, 627), (595, 655)),
        AMBER,
        "rank",
        (602, 632, 28, 15),
        exit_xy=(0.5, 1),
        entry_xy=(0.5, 0),
    ),
    Edge(
        "a13",
        "redaction",
        "ai",
        ((955, 627), (955, 655)),
        RED,
        "safe text",
        (963, 632, 46, 15),
        exit_xy=(0.5, 1),
        entry_xy=(0.5, 0),
    ),
    Edge(
        "a14",
        "voice",
        "events",
        ((1335, 627), (1335, 655)),
        AMBER,
        "finalize",
        (1343, 632, 42, 15),
        exit_xy=(0.5, 1),
        entry_xy=(0.5, 0),
    ),
    Edge(
        "a15",
        "care",
        "postgres",
        ((70, 586), (55, 586), (55, 775), (280, 775), (280, 870)),
        TEAL,
        exit_xy=(0, 0.5),
        entry_xy=(210 / 360, 0),
    ),
    Edge(
        "a16",
        "collab",
        "postgres",
        ((360, 727), (360, 870)),
        BLUE,
        exit_xy=(290 / 330, 1),
        entry_xy=(290 / 360, 0),
    ),
    Edge(
        "a17",
        "glance",
        "postgres",
        ((450, 727), (450, 795), (410, 795), (410, 870)),
        AMBER,
        exit_xy=(20 / 330, 1),
        entry_xy=(340 / 360, 0),
    ),
    Edge(
        "a18",
        "ai",
        "worker",
        ((800, 727), (800, 775), (600, 775), (600, 870)),
        VIOLET,
        "enqueue",
        (665, 762, 46, 15),
        exit_xy=(10 / 330, 1),
        entry_xy=(130 / 310, 0),
    ),
    Edge(
        "a19",
        "events",
        "worker",
        ((1170, 727), (1170, 805), (760, 805), (760, 870)),
        BLUE,
        "lease",
        (960, 792, 30, 15),
        exit_xy=(20 / 370, 1),
        entry_xy=(290 / 310, 0),
    ),
    Edge(
        "a20",
        "worker",
        "providers",
        ((780, 900), (840, 900)),
        VIOLET,
        "contract",
        (787, 884, 46, 15),
        dashed=True,
        exit_xy=(1, 0.25),
        entry_xy=(0, 0.25),
    ),
    Edge(
        "a21",
        "worker",
        "postgres",
        ((470, 950), (430, 950)),
        TEAL,
        "write",
        (435, 934, 30, 15),
        exit_xy=(0, 2 / 3),
        entry_xy=(1, 2 / 3),
    ),
    Edge(
        "a22",
        "postgres",
        "archive",
        ((250, 990), (250, 1030), (1370, 1030), (1370, 990)),
        AMBER,
        "cold",
        (1095, 1017, 30, 15),
        exit_xy=(0.5, 1),
        entry_xy=(0.5, 1),
    ),
    Edge(
        "a23",
        "events",
        "sse",
        ((1520, 690), (1540, 690), (1540, 410), (1530, 410)),
        AMBER,
        "publish",
        (1490, 485, 42, 15),
        exit_xy=(1, 0.5),
        entry_xy=(1, 0.5),
    ),
    Edge(
        "a24",
        "sse",
        "clinician",
        (
            (1500, 375),
            (1500, 300),
            (1575, 300),
            (1575, 105),
            (300, 105),
            (300, 160),
        ),
        AMBER,
        "invalidate",
        (1400, 92, 48, 15),
        dashed=True,
        exit_xy=(180 / 210, 0),
        entry_xy=(230 / 330, 0),
    ),
)


LEGENDS = (
    ("legend1", 1004, 1085, 145, 30, "trusted boundary", TEAL),
    ("legend2", 1160, 1085, 145, 30, "async / optional", VIOLET),
    ("legend3", 1316, 1085, 204, 30, "synthetic-data demo only", AMBER),
)


def style(**values: str | int | float) -> str:
    return ";".join(f"{key}={value}" for key, value in values.items()) + ";"


def add_geometry(
    cell: ET.Element,
    *,
    x: float | None = None,
    y: float | None = None,
    w: float | None = None,
    h: float | None = None,
    relative: bool = False,
) -> ET.Element:
    attrs: dict[str, str] = {"as": "geometry"}
    if x is not None:
        attrs["x"] = f"{x:g}"
    if y is not None:
        attrs["y"] = f"{y:g}"
    if w is not None:
        attrs["width"] = f"{w:g}"
    if h is not None:
        attrs["height"] = f"{h:g}"
    if relative:
        attrs["relative"] = "1"
    return ET.SubElement(cell, "mxGeometry", attrs)


def html_value(box: Box) -> str:
    body = "<br>".join(box.lines)
    return (
        f'<b><font color="{box.color}" style="font-size:15px">'
        f"{box.title}</font></b><br>"
        f'<font color="{MUTED}" style="font-size:11.5px">{body}</font>'
    )


def build_drawio() -> bytes:
    mxfile = ET.Element(
        "mxfile",
        {
            "host": "app.diagrams.net",
            "agent": "Codex",
            "version": "24.7.17",
            "type": "device",
        },
    )
    diagram = ET.SubElement(
        mxfile,
        "diagram",
        {"id": "nightingale-architecture-straight", "name": "Architecture"},
    )
    model = ET.SubElement(
        diagram,
        "mxGraphModel",
        {
            "dx": "1422",
            "dy": "794",
            "grid": "1",
            "gridSize": "10",
            "guides": "1",
            "tooltips": "1",
            "connect": "1",
            "arrows": "1",
            "fold": "1",
            "page": "1",
            "pageScale": "1",
            "pageWidth": str(CANVAS_W),
            "pageHeight": str(CANVAS_H),
            "math": "0",
            "shadow": "0",
            "adaptiveColors": "auto",
            "background": WHITE,
        },
    )
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    title = ET.SubElement(
        root,
        "mxCell",
        {
            "id": "title",
            "value": "Nightingale · Trustworthy Clinical Memory",
            "style": style(
                text="",
                html=1,
                strokeColor="none",
                fillColor="none",
                align="left",
                verticalAlign="middle",
                fontSize=26,
                fontColor=INK,
                fontStyle=1,
                whiteSpace="wrap",
                fontFamily=FONT,
                rounded=0,
            ),
            "vertex": "1",
            "parent": "1",
        },
    )
    add_geometry(title, x=44, y=26, w=1512, h=42)

    subtitle = ET.SubElement(
        root,
        "mxCell",
        {
            "id": "subtitle",
            "value": "System architecture and trust boundaries",
            "style": style(
                text="",
                html=1,
                strokeColor="none",
                fillColor="none",
                align="left",
                verticalAlign="middle",
                fontSize=12,
                fontColor=MUTED,
                whiteSpace="wrap",
                fontFamily=FONT,
                rounded=0,
            ),
            "vertex": "1",
            "parent": "1",
        },
    )
    add_geometry(subtitle, x=46, y=68, w=1508, h=24)

    for section in SECTIONS:
        cell = ET.SubElement(
            root,
            "mxCell",
            {
                "id": section.id,
                "value": section.title,
                "style": style(
                    rounded=0,
                    whiteSpace="wrap",
                    html=1,
                    fillColor=WHITE,
                    strokeColor=section.color,
                    strokeWidth=1.5,
                    verticalAlign="top",
                    align="left",
                    spacingTop=14,
                    spacingLeft=16,
                    fontColor=INK,
                    fontStyle=1,
                    fontSize=14,
                    shadow=0,
                    fontFamily=FONT,
                ),
                "vertex": "1",
                "parent": "1",
            },
        )
        add_geometry(cell, x=section.x, y=section.y, w=section.w, h=section.h)

    for edge in EDGES:
        edge_style = {
            "edgeStyle": "orthogonalEdgeStyle",
            "rounded": 0,
            "curved": 0,
            "orthogonalLoop": 1,
            "jettySize": 10,
            "html": 1,
            "strokeColor": edge.color,
            "strokeWidth": 1.8,
            "endArrow": "block",
            "endFill": 1,
            "fontSize": 10,
            "fontColor": MUTED,
            "dashed": int(edge.dashed),
            "fontFamily": FONT,
        }
        if edge.exit_xy:
            edge_style.update(
                {
                    "exitX": f"{edge.exit_xy[0]:g}",
                    "exitY": f"{edge.exit_xy[1]:g}",
                    "exitDx": 0,
                    "exitDy": 0,
                }
            )
        if edge.entry_xy:
            edge_style.update(
                {
                    "entryX": f"{edge.entry_xy[0]:g}",
                    "entryY": f"{edge.entry_xy[1]:g}",
                    "entryDx": 0,
                    "entryDy": 0,
                }
            )
        cell = ET.SubElement(
            root,
            "mxCell",
            {
                "id": edge.id,
                "value": "",
                "style": style(**edge_style),
                "edge": "1",
                "parent": "1",
                "source": edge.source,
                "target": edge.target,
            },
        )
        geometry = add_geometry(cell, relative=True)
        if len(edge.points) > 2:
            array = ET.SubElement(geometry, "Array", {"as": "points"})
            for point_x, point_y in edge.points[1:-1]:
                ET.SubElement(
                    array,
                    "mxPoint",
                    {"x": f"{point_x:g}", "y": f"{point_y:g}"},
                )

    for box in BOXES:
        cell = ET.SubElement(
            root,
            "mxCell",
            {
                "id": box.id,
                "value": html_value(box),
                "style": style(
                    rounded=0,
                    whiteSpace="wrap",
                    html=1,
                    fillColor=WHITE,
                    strokeColor=box.color,
                    strokeWidth=1.7,
                    align="left",
                    verticalAlign="middle",
                    spacingLeft=13,
                    spacingRight=10,
                    spacingTop=6,
                    fontSize=11.5,
                    fontColor=MUTED,
                    shadow=0,
                    fontFamily=FONT,
                ),
                "vertex": "1",
                "parent": "1",
            },
        )
        add_geometry(cell, x=box.x, y=box.y, w=box.w, h=box.h)

    for edge in EDGES:
        if not edge.label or not edge.label_box:
            continue
        x, y, w, h = edge.label_box
        label = ET.SubElement(
            root,
            "mxCell",
            {
                "id": f"label_{edge.id}",
                "value": edge.label,
                "style": style(
                    text="",
                    html=1,
                    strokeColor="none",
                    fillColor=WHITE,
                    align="center",
                    verticalAlign="middle",
                    fontSize=10,
                    fontColor=MUTED,
                    whiteSpace="wrap",
                    fontFamily=FONT,
                    rounded=0,
                ),
                "vertex": "1",
                "parent": "1",
            },
        )
        add_geometry(label, x=x, y=y, w=w, h=h)

    for legend_id, x, y, w, h, text, color in LEGENDS:
        legend = ET.SubElement(
            root,
            "mxCell",
            {
                "id": legend_id,
                "value": text,
                "style": style(
                    rounded=0,
                    whiteSpace="wrap",
                    html=1,
                    fillColor=WHITE,
                    strokeColor=color,
                    strokeWidth=1.5,
                    fontColor=color,
                    fontStyle=1,
                    fontSize=11,
                    align="center",
                    verticalAlign="middle",
                    fontFamily=FONT,
                ),
                "vertex": "1",
                "parent": "1",
            },
        )
        add_geometry(legend, x=x, y=y, w=w, h=h)

    ET.indent(mxfile, space="  ")
    return ET.tostring(mxfile, encoding="utf-8", xml_declaration=True)


def svg_element(parent: ET.Element, tag: str, **attrs: str) -> ET.Element:
    return ET.SubElement(parent, tag, attrs)


def add_svg_text(
    parent: ET.Element,
    text: str,
    x: float,
    y: float,
    *,
    size: float,
    color: str,
    weight: str = "normal",
    anchor: str = "start",
    italic: bool = False,
) -> ET.Element:
    attrs = {
        "x": f"{x:g}",
        "y": f"{y:g}",
        "font-family": "Times New Roman, Times, serif",
        "font-size": f"{size:g}",
        "font-weight": weight,
        "fill": color,
        "text-anchor": anchor,
    }
    if italic:
        attrs["font-style"] = "italic"
    element = svg_element(parent, "text", **attrs)
    element.text = text
    return element


def build_svg(drawio_bytes: bytes) -> bytes:
    svg = ET.Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "width": str(CANVAS_W),
            "height": str(CANVAS_H),
            "viewBox": f"0 0 {CANVAS_W} {CANVAS_H}",
            "role": "img",
            "aria-label": "Nightingale system architecture and trust boundaries",
        },
    )
    metadata = svg_element(svg, "metadata", id="drawio-source-base64")
    metadata.text = base64.b64encode(drawio_bytes).decode("ascii")

    defs = svg_element(svg, "defs")
    for color_name, color in (
        ("teal", TEAL),
        ("blue", BLUE),
        ("violet", VIOLET),
        ("amber", AMBER),
        ("red", RED),
    ):
        marker = svg_element(
            defs,
            "marker",
            id=f"arrow-{color_name}",
            markerWidth="7",
            markerHeight="7",
            refX="7",
            refY="3.5",
            orient="auto",
            markerUnits="strokeWidth",
        )
        svg_element(marker, "path", d="M 0 0 L 7 3.5 L 0 7 z", fill=color)

    svg_element(
        svg, "rect", x="0", y="0", width=str(CANVAS_W), height=str(CANVAS_H), fill=WHITE
    )

    add_svg_text(
        svg,
        "Nightingale · Trustworthy Clinical Memory",
        44,
        58,
        size=28,
        color=INK,
        weight="bold",
    )
    add_svg_text(
        svg, "System architecture and trust boundaries", 46, 87, size=13, color=MUTED
    )

    for section in SECTIONS:
        svg_element(
            svg,
            "rect",
            x=f"{section.x:g}",
            y=f"{section.y:g}",
            width=f"{section.w:g}",
            height=f"{section.h:g}",
            fill=WHITE,
            stroke=section.color,
            **{"stroke-width": "1.5"},
        )
        add_svg_text(
            svg,
            section.title,
            section.x + 16,
            section.y + 29,
            size=14,
            color=INK,
            weight="bold",
        )

    arrow_ids = {
        TEAL: "teal",
        BLUE: "blue",
        VIOLET: "violet",
        AMBER: "amber",
        RED: "red",
    }
    for edge in EDGES:
        attrs = {
            "points": " ".join(f"{x:g},{y:g}" for x, y in edge.points),
            "fill": "none",
            "stroke": edge.color,
            "stroke-width": "1.8",
            "stroke-linecap": "square",
            "stroke-linejoin": "miter",
            "marker-end": f"url(#arrow-{arrow_ids[edge.color]})",
            "vector-effect": "non-scaling-stroke",
        }
        if edge.dashed:
            attrs["stroke-dasharray"] = "7 6"
        svg_element(svg, "polyline", **attrs)

    for box in BOXES:
        svg_element(
            svg,
            "rect",
            x=f"{box.x:g}",
            y=f"{box.y:g}",
            width=f"{box.w:g}",
            height=f"{box.h:g}",
            fill=WHITE,
            stroke=box.color,
            **{"stroke-width": "1.8"},
        )
        title_y = box.y + (27 if box.h <= 82 else 28)
        add_svg_text(
            svg,
            box.title,
            box.x + 14,
            title_y,
            size=15,
            color=box.color,
            weight="bold",
        )
        body_start = title_y + 22
        for index, line in enumerate(box.lines):
            add_svg_text(
                svg,
                line,
                box.x + 14,
                body_start + 16 * index,
                size=11.5,
                color=MUTED,
            )

    for edge in EDGES:
        if not edge.label or not edge.label_box:
            continue
        x, y, w, h = edge.label_box
        svg_element(
            svg,
            "rect",
            x=f"{x:g}",
            y=f"{y:g}",
            width=f"{w:g}",
            height=f"{h:g}",
            fill=WHITE,
        )
        add_svg_text(
            svg,
            edge.label,
            x + w / 2,
            y + h - 3,
            size=10,
            color=MUTED,
            anchor="middle",
        )

    for _legend_id, x, y, w, h, text, color in LEGENDS:
        svg_element(
            svg,
            "rect",
            x=f"{x:g}",
            y=f"{y:g}",
            width=f"{w:g}",
            height=f"{h:g}",
            fill=WHITE,
            stroke=color,
            **{"stroke-width": "1.5"},
        )
        add_svg_text(
            svg,
            text,
            x + w / 2,
            y + 20,
            size=11,
            color=color,
            weight="bold",
            anchor="middle",
        )

    ET.indent(svg, space="  ")
    return ET.tostring(svg, encoding="utf-8", xml_declaration=True)


def segment_intersects_box(
    p1: tuple[float, float],
    p2: tuple[float, float],
    box: Box,
) -> bool:
    x1, y1 = p1
    x2, y2 = p2
    left, right = box.x, box.x + box.w
    top, bottom = box.y, box.y + box.h
    if x1 == x2:
        return left < x1 < right and max(min(y1, y2), top) < min(max(y1, y2), bottom)
    if y1 == y2:
        return top < y1 < bottom and max(min(x1, x2), left) < min(max(x1, x2), right)
    raise ValueError(f"non-orthogonal connector segment: {p1} -> {p2}")


def segment_contact(
    first: tuple[tuple[float, float], tuple[float, float]],
    second: tuple[tuple[float, float], tuple[float, float]],
) -> str | None:
    (x1, y1), (x2, y2) = first
    (u1, v1), (u2, v2) = second
    first_vertical = x1 == x2
    second_vertical = u1 == u2
    if first_vertical and not second_vertical:
        if min(u1, u2) < x1 < max(u1, u2) and min(y1, y2) < v1 < max(y1, y2):
            return "cross"
        if min(u1, u2) <= x1 <= max(u1, u2) and min(y1, y2) <= v1 <= max(y1, y2):
            return "touch"
    elif not first_vertical and second_vertical:
        return segment_contact(second, first)
    elif first_vertical and second_vertical and x1 == u1:
        lower = max(min(y1, y2), min(v1, v2))
        upper = min(max(y1, y2), max(v1, v2))
        if lower < upper:
            return "overlap"
        if lower == upper:
            return "touch"
    elif not first_vertical and not second_vertical and y1 == v1:
        lower = max(min(x1, x2), min(u1, u2))
        upper = min(max(x1, x2), max(u1, u2))
        if lower < upper:
            return "overlap"
        if lower == upper:
            return "touch"
    return None


def validate_routes() -> None:
    boxes = {box.id: box for box in BOXES}
    if len(boxes) != len(BOXES):
        raise RuntimeError("duplicate box id")
    for edge in EDGES:
        if edge.source not in boxes or edge.target not in boxes:
            raise RuntimeError(f"unknown endpoint in {edge.id}")
        for p1, p2 in zip(edge.points, edge.points[1:]):
            if p1[0] != p2[0] and p1[1] != p2[1]:
                raise RuntimeError(f"{edge.id} has a diagonal segment: {p1} -> {p2}")
            for box in BOXES:
                if box.id in {edge.source, edge.target}:
                    continue
                if segment_intersects_box(p1, p2, box):
                    raise RuntimeError(f"{edge.id} crosses node {box.id}: {p1} -> {p2}")
    for index, first in enumerate(EDGES):
        for second in EDGES[index + 1 :]:
            for first_segment in zip(first.points, first.points[1:]):
                for second_segment in zip(second.points, second.points[1:]):
                    contact = segment_contact(first_segment, second_segment)
                    if contact:
                        raise RuntimeError(
                            f"{first.id} and {second.id} {contact}: "
                            f"{first_segment} vs {second_segment}"
                        )


def main() -> None:
    validate_routes()
    drawio_bytes = build_drawio()
    svg_bytes = build_svg(drawio_bytes)
    ET.fromstring(drawio_bytes)
    svg = ET.fromstring(svg_bytes)
    metadata = next(
        element
        for element in svg.iter()
        if element.tag.endswith("metadata")
        and element.attrib.get("id") == "drawio-source-base64"
    )
    if base64.b64decode(metadata.text or "") != drawio_bytes:
        raise RuntimeError("SVG embedded Draw.io source mismatch")
    DRAWIO_PATH.write_bytes(drawio_bytes)
    SVG_PATH.write_bytes(svg_bytes)
    print(DRAWIO_PATH)
    print(SVG_PATH)


if __name__ == "__main__":
    main()
