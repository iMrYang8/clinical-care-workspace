#!/usr/bin/env python3
"""Generate the editable and SVG Nightingale core evidence schema diagrams."""

from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
DRAWIO_PATH = ROOT / "docs" / "schema.drawio"
SVG_PATH = ROOT / "docs" / "schema.svg"
WIDTH = 1600
HEIGHT = 1120

INK = "#17324D"
MUTED = "#607386"
PAPER = "#FFFFFF"
WHITE = "#FFFFFF"
TEAL = "#0F766E"
BLUE = "#2563EB"
VIOLET = "#7C3AED"
AMBER = "#B7791F"
RED = "#BE4B5A"
LINE = "#D7E0DE"


@dataclass(frozen=True)
class Section:
    identifier: str
    title: str
    x: int
    y: int
    width: int
    height: int
    fill: str
    stroke: str


@dataclass(frozen=True)
class Node:
    identifier: str
    title: str
    table: str
    details: tuple[str, ...]
    x: int
    y: int
    width: int
    height: int
    color: str
    fill: str = WHITE
    dashed: bool = False

    @property
    def left(self) -> int:
        return self.x

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def top(self) -> int:
        return self.y

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def cx(self) -> int:
        return self.x + self.width // 2

    @property
    def cy(self) -> int:
        return self.y + self.height // 2


@dataclass(frozen=True)
class Edge:
    identifier: str
    source: str
    target: str
    label: str
    color: str
    points: tuple[tuple[int, int], ...]
    label_x: int
    label_y: int
    dashed: bool = False


SECTIONS = (
    Section(
        "record_box",
        "01  IMMUTABLE RECORD + AI-SCRIBED NOTES",
        40,
        118,
        1520,
        285,
        WHITE,
        "#C9DED9",
    ),
    Section(
        "trust_box",
        "02  SOURCE, ASSERTION + DECISION GATES",
        40,
        421,
        1520,
        275,
        WHITE,
        "#DBD1EE",
    ),
    Section(
        "sharing_box",
        "03  CLINICIAN-APPROVED PATIENT PUBLICATION",
        40,
        714,
        1520,
        166,
        WHITE,
        "#E5D7B8",
    ),
    Section(
        "learning_box",
        "04  GLANCE + BOUNDED IMPORTANCE LEARNING",
        40,
        898,
        1520,
        166,
        WHITE,
        "#CDD9E8",
    ),
)

NODES = (
    Node(
        "patient",
        "Patient",
        "patients",
        ("clinic-scoped record", "1 patient : N entries"),
        70,
        188,
        175,
        105,
        TEAL,
    ),
    Node(
        "entry",
        "Entry",
        "entries",
        ("origin + section + entry_type", "current_version_id"),
        290,
        172,
        240,
        130,
        TEAL,
    ),
    Node(
        "version",
        "EntryVersion",
        "entry_versions",
        (
            "immutable content + SHA-256",
            "version_no + author_id",
            "revert creates a new version",
        ),
        580,
        172,
        260,
        130,
        BLUE,
    ),
    Node(
        "comment",
        "Comment",
        "comments",
        (
            "entry_id + entry_version_id",
            "exact quote + offsets + hash",
            "thread / resolve / task anchor",
        ),
        890,
        172,
        240,
        130,
        VIOLET,
    ),
    Node(
        "ai_run",
        "AIRun",
        "ai_runs",
        (
            "source_entry_version_id",
            "output_entry_id + output_version_id",
            "provider/model + review state",
        ),
        1180,
        172,
        300,
        130,
        VIOLET,
    ),
    Node(
        "voice_source",
        "Voice source chain",
        "audio_assets -> transcript_revisions -> clinical_facts",
        ("optional audio/time provenance",),
        570,
        318,
        560,
        82,
        AMBER,
        WHITE,
        True,
    ),
    Node(
        "ai_note",
        "AI-scribed note = Entry stereotype",
        "not a separate table",
        ("origin=ai; section=system", "doctor / nurse / patient summary"),
        1180,
        310,
        300,
        90,
        VIOLET,
        WHITE,
        True,
    ),
    Node(
        "highlight",
        "Highlight",
        "highlights",
        (
            "patient + entry + source version",
            "base / learned / final score",
            "risk, protected and review state",
        ),
        70,
        493,
        230,
        130,
        AMBER,
    ),
    Node(
        "provenance",
        "ProvenancePointer",
        "provenance_pointers",
        (
            "entry_version + exact span/hash",
            "optional highlight/comment",
            "optional audio asset + milliseconds",
        ),
        350,
        493,
        260,
        130,
        VIOLET,
    ),
    Node(
        "assertion",
        "ClinicalFactAssertion",
        "clinical_fact_assertions",
        (
            "patient + entry + version + pointer",
            "normalized fact + polarity/status",
            "canonical source-bound claim",
        ),
        660,
        493,
        290,
        130,
        TEAL,
    ),
    Node(
        "assessment",
        "DecisionAssessment",
        "decision_assessments",
        (
            "UQ: one per Highlight",
            "optional assertion + calibration",
            "risk floor + confidence + abstention",
        ),
        1000,
        493,
        280,
        130,
        RED,
    ),
    Node(
        "calibration",
        "CalibrationReport",
        "calibration_reports",
        ("provider/model/task", "confidence band + lower bound"),
        1330,
        493,
        200,
        130,
        VIOLET,
    ),
    Node(
        "sharing_request",
        "PatientSharingRequest",
        "patient_sharing_requests",
        ("exact submitted EntryVersion", "staff request -> clinician review"),
        230,
        756,
        300,
        92,
        AMBER,
    ),
    Node(
        "publication",
        "PatientPublication",
        "patient_publications",
        ("approved result EntryVersion", "receipt, withdraw, supersede"),
        620,
        756,
        300,
        92,
        TEAL,
    ),
    Node(
        "publication_item",
        "PatientPublicationItem",
        "patient_publication_items",
        ("required ProvenancePointer", "optional Assertion + Assessment"),
        1010,
        756,
        420,
        92,
        BLUE,
    ),
    Node(
        "snapshot",
        "PatientGlanceSnapshot",
        "patient_glance_snapshots",
        (
            "encrypted precomputed projection",
            "cards / review cards / patient cards <= 5",
        ),
        70,
        934,
        300,
        100,
        TEAL,
    ),
    Node(
        "impression",
        "ImportanceImpression",
        "importance_impressions",
        ("Highlight + viewer membership", "rank, exposure, visible duration"),
        415,
        934,
        270,
        100,
        BLUE,
    ),
    Node(
        "feedback",
        "ImportanceFeedbackEvent",
        "importance_feedback_events",
        ("explicit action + reason", "Highlight + actor + applied delta"),
        730,
        934,
        285,
        100,
        VIOLET,
    ),
    Node(
        "feature_stat",
        "ImportanceFeatureStat",
        "importance_feature_stats",
        ("UQ clinic + feature_key", "bounded weight + counts"),
        1060,
        934,
        275,
        100,
        AMBER,
    ),
    Node(
        "exposure_only",
        "Exposure audit only",
        "service boundary",
        ("Impressions never update score",),
        1365,
        942,
        180,
        84,
        RED,
        WHITE,
        True,
    ),
)

NODE_BY_ID = {node.identifier: node for node in NODES}

EDGES = (
    Edge(
        "e_patient_entry",
        "patient",
        "entry",
        "1 : N",
        TEAL,
        ((245, 240), (290, 240)),
        267,
        226,
    ),
    Edge(
        "e_entry_version",
        "entry",
        "version",
        "1 : N immutable",
        BLUE,
        ((530, 237), (580, 237)),
        555,
        222,
    ),
    Edge(
        "e_version_comment",
        "version",
        "comment",
        "exact version anchor",
        VIOLET,
        ((840, 237), (890, 237)),
        865,
        222,
    ),
    Edge(
        "e_version_ai_source",
        "version",
        "ai_run",
        "source_entry_version_id",
        VIOLET,
        ((840, 195), (1150, 195), (1150, 214), (1180, 214)),
        1000,
        180,
    ),
    Edge(
        "e_ai_entry",
        "ai_run",
        "entry",
        "output_entry_id",
        VIOLET,
        ((1330, 172), (1330, 145), (410, 145), (410, 172)),
        880,
        132,
    ),
    Edge(
        "e_ai_version",
        "ai_run",
        "version",
        "output_entry_version_id",
        VIOLET,
        ((1390, 172), (1390, 158), (710, 158), (710, 172)),
        1050,
        160,
    ),
    Edge(
        "e_ai_stereotype",
        "ai_run",
        "ai_note",
        "stored as Entry + EntryVersion",
        VIOLET,
        ((1330, 302), (1330, 310)),
        1330,
        307,
        True,
    ),
    Edge(
        "e_version_highlight",
        "version",
        "highlight",
        "source_entry_version_id",
        AMBER,
        ((650, 302), (555, 302), (555, 464), (185, 464), (185, 493)),
        350,
        460,
    ),
    Edge(
        "e_version_provenance",
        "version",
        "provenance",
        "entry_version_id",
        VIOLET,
        ((620, 302), (540, 302), (540, 478), (480, 478), (480, 493)),
        510,
        475,
    ),
    Edge(
        "e_comment_provenance",
        "comment",
        "provenance",
        "comment_id (optional)",
        VIOLET,
        ((1010, 302), (1150, 302), (1150, 452), (540, 452), (540, 493)),
        850,
        449,
    ),
    Edge(
        "e_voice_provenance",
        "voice_source",
        "provenance",
        "audio/time anchor",
        AMBER,
        ((760, 400), (760, 484), (575, 484), (575, 493)),
        670,
        481,
    ),
    Edge(
        "e_highlight_provenance",
        "highlight",
        "provenance",
        "highlight_id (optional)",
        VIOLET,
        ((300, 558), (350, 558)),
        325,
        638,
    ),
    Edge(
        "e_provenance_assertion",
        "provenance",
        "assertion",
        "canonicalizes",
        TEAL,
        ((610, 558), (660, 558)),
        635,
        638,
    ),
    Edge(
        "e_assertion_assessment",
        "assertion",
        "assessment",
        "assertion_id (optional)",
        RED,
        ((950, 558), (1000, 558)),
        975,
        638,
    ),
    Edge(
        "e_highlight_assessment",
        "highlight",
        "assessment",
        "UQ highlight_id",
        RED,
        ((185, 623), (185, 662), (1140, 662), (1140, 623)),
        430,
        658,
    ),
    Edge(
        "e_calibration_assessment",
        "calibration",
        "assessment",
        "calibration_report_id",
        VIOLET,
        ((1330, 558), (1280, 558)),
        1305,
        638,
    ),
    Edge(
        "e_version_request",
        "version",
        "sharing_request",
        "submitted exact version",
        AMBER,
        (
            (840, 302),
            (1145, 302),
            (1145, 410),
            (1540, 410),
            (1540, 704),
            (380, 704),
            (380, 756),
        ),
        500,
        701,
    ),
    Edge(
        "e_request_publication",
        "sharing_request",
        "publication",
        "clinician review + gates",
        TEAL,
        ((530, 802), (620, 802)),
        575,
        865,
    ),
    Edge(
        "e_publication_item",
        "publication",
        "publication_item",
        "1 : N receipt items",
        BLUE,
        ((920, 802), (1010, 802)),
        965,
        865,
    ),
    Edge(
        "e_pointer_item",
        "provenance",
        "publication_item",
        "required FK",
        VIOLET,
        ((480, 623), (480, 690), (1080, 690), (1080, 756)),
        780,
        678,
    ),
    Edge(
        "e_assertion_item",
        "assertion",
        "publication_item",
        "optional FK",
        TEAL,
        ((805, 623), (805, 682), (1200, 682), (1200, 756)),
        1000,
        670,
    ),
    Edge(
        "e_assessment_item",
        "assessment",
        "publication_item",
        "optional FK",
        RED,
        ((1140, 623), (1140, 674), (1320, 674), (1320, 756)),
        1230,
        662,
    ),
    Edge(
        "e_highlight_snapshot",
        "highlight",
        "snapshot",
        "top-five projection",
        TEAL,
        ((95, 623), (20, 623), (20, 886), (220, 886), (220, 934)),
        130,
        883,
        True,
    ),
    Edge(
        "e_snapshot_impression",
        "snapshot",
        "impression",
        "rendered exposure",
        BLUE,
        ((370, 984), (390, 984), (390, 925), (650, 925), (650, 934)),
        580,
        922,
        True,
    ),
    Edge(
        "e_highlight_feedback",
        "highlight",
        "feedback",
        "explicit action FK",
        VIOLET,
        ((270, 623), (950, 623), (950, 890), (872, 890), (872, 934)),
        905,
        887,
    ),
    Edge(
        "e_feedback_stats",
        "feedback",
        "feature_stat",
        "feature keys + bounded delta",
        AMBER,
        ((1015, 984), (1038, 984), (1038, 925), (1197, 925), (1197, 934)),
        1120,
        922,
        True,
    ),
    Edge(
        "e_stats_snapshot",
        "feature_stat",
        "snapshot",
        "re-score Highlights -> rebuild Glance",
        TEAL,
        ((1197, 1034), (1197, 1060), (220, 1060), (220, 1034)),
        710,
        1057,
        True,
    ),
    Edge(
        "e_impression_audit",
        "impression",
        "exposure_only",
        "telemetry only",
        RED,
        ((685, 934), (685, 910), (1455, 910), (1455, 942)),
        1350,
        907,
        True,
    ),
)


def _node_value(node: Node) -> str:
    lines = [
        f'<b><font color="{node.color}" style="font-size:16px">{html.escape(node.title)}</font></b>',
        f'<font color="{MUTED}" style="font-size:11px">{html.escape(node.table)}</font>',
    ]
    lines.extend(
        f'<font color="{MUTED}" style="font-size:12px">{html.escape(detail)}</font>'
        for detail in node.details
    )
    return "<br>".join(lines)


def build_drawio() -> bytes:
    model = ET.Element(
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
            "pageWidth": str(WIDTH),
            "pageHeight": str(HEIGHT),
            "math": "0",
            "shadow": "0",
            "adaptiveColors": "auto",
            "background": PAPER,
        },
    )
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    def vertex(
        identifier: str, value: str, style: str, x: int, y: int, width: int, height: int
    ) -> None:
        cell = ET.SubElement(
            root,
            "mxCell",
            {
                "id": identifier,
                "value": value,
                "style": style,
                "vertex": "1",
                "parent": "1",
            },
        )
        ET.SubElement(
            cell,
            "mxGeometry",
            {
                "x": str(x),
                "y": str(y),
                "width": str(width),
                "height": str(height),
                "as": "geometry",
            },
        )

    vertex(
        "title",
        "Nightingale · Core Clinical Evidence Schema",
        f"text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontFamily=Times New Roman;fontSize=26;fontColor={INK};fontStyle=1;whiteSpace=wrap;",
        44,
        28,
        1512,
        42,
    )
    vertex(
        "subtitle",
        "Implemented PostgreSQL tables and service derivations from immutable note to decision, learning and patient receipt",
        f"text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontFamily=Times New Roman;fontSize=12;fontColor={MUTED};whiteSpace=wrap;",
        46,
        68,
        1508,
        30,
    )

    for section in SECTIONS:
        vertex(
            section.identifier,
            section.title,
            "rounded=0;whiteSpace=wrap;html=1;"
            f"fillColor={section.fill};strokeColor={section.stroke};strokeWidth=1.5;"
            f"verticalAlign=top;align=left;spacingTop=12;spacingLeft=16;fontFamily=Times New Roman;fontColor={INK};fontStyle=1;fontSize=14;shadow=0;",
            section.x,
            section.y,
            section.width,
            section.height,
        )

    for edge in EDGES:
        style = (
            "edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;"
            f"strokeColor={edge.color};strokeWidth=1.7;endArrow=block;endFill=1;"
            f"fontFamily=Times New Roman;fontSize=10;fontColor={MUTED};dashed={1 if edge.dashed else 0};"
        )
        cell = ET.SubElement(
            root,
            "mxCell",
            {
                "id": edge.identifier,
                "value": edge.label,
                "style": style,
                "edge": "1",
                "parent": "1",
                "source": edge.source,
                "target": edge.target,
            },
        )
        geometry = ET.SubElement(
            cell, "mxGeometry", {"relative": "1", "as": "geometry"}
        )
        points = ET.SubElement(geometry, "Array", {"as": "points"})
        for x, y in edge.points[1:-1]:
            ET.SubElement(points, "mxPoint", {"x": str(x), "y": str(y)})

    for node in NODES:
        style = (
            "rounded=0;whiteSpace=wrap;html=1;"
            f"fillColor={node.fill};strokeColor={node.color};strokeWidth=1.6;"
            "verticalAlign=middle;align=left;spacingLeft=14;spacingRight=12;"
            f"fontFamily=Times New Roman;fontColor={MUTED};fontSize=11;shadow=0;dashed={1 if node.dashed else 0};"
        )
        vertex(
            node.identifier,
            _node_value(node),
            style,
            node.x,
            node.y,
            node.width,
            node.height,
        )

    vertex(
        "legend_solid",
        "solid = stored database FK / uniqueness",
        f"rounded=0;whiteSpace=wrap;html=1;fillColor={WHITE};strokeColor={TEAL};fontFamily=Times New Roman;fontColor={TEAL};fontStyle=1;fontSize=11;",
        40,
        1078,
        310,
        28,
    )
    vertex(
        "legend_dashed",
        "dashed = service derivation / projection / telemetry",
        f"rounded=0;whiteSpace=wrap;html=1;fillColor={WHITE};strokeColor={VIOLET};fontFamily=Times New Roman;fontColor={VIOLET};fontStyle=1;fontSize=11;dashed=1;",
        370,
        1078,
        360,
        28,
    )
    vertex(
        "tenant_rule",
        "All clinical rows carry clinic_id · tenant-composite FKs + PostgreSQL RLS enforce clinic scope · encrypted payloads remain separate from immutable evidence metadata",
        f"rounded=0;whiteSpace=wrap;html=1;fillColor={WHITE};strokeColor={BLUE};fontFamily=Times New Roman;fontColor={BLUE};fontStyle=1;fontSize=11;",
        750,
        1078,
        810,
        28,
    )

    ET.indent(model, space="  ")
    return ET.tostring(model, encoding="utf-8", xml_declaration=True)


def _svg_text(
    parent: ET.Element,
    x: int,
    y: int,
    text: str,
    *,
    size: int,
    color: str,
    weight: str = "400",
    anchor: str = "start",
    family: str = "Times New Roman, Times, serif",
) -> None:
    element = ET.SubElement(
        parent,
        "text",
        {
            "x": str(x),
            "y": str(y),
            "font-family": family,
            "font-size": str(size),
            "font-weight": weight,
            "fill": color,
            "text-anchor": anchor,
        },
    )
    element.text = text


def build_svg(drawio_bytes: bytes) -> bytes:
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    svg = ET.Element(
        "{http://www.w3.org/2000/svg}svg",
        {
            "width": str(WIDTH),
            "height": str(HEIGHT),
            "viewBox": f"0 0 {WIDTH} {HEIGHT}",
            "role": "img",
            "aria-labelledby": "svg-title svg-desc",
        },
    )
    title = ET.SubElement(svg, "title", {"id": "svg-title"})
    title.text = "Nightingale · Core Clinical Evidence Schema"
    desc = ET.SubElement(svg, "desc", {"id": "svg-desc"})
    desc.text = "Implemented clinical record, provenance, decision, patient publication and bounded importance-learning relationships"
    metadata = ET.SubElement(svg, "metadata", {"id": "drawio-source-base64"})
    metadata.text = base64.b64encode(drawio_bytes).decode("ascii")

    defs = ET.SubElement(svg, "defs")
    for name, color in (
        ("teal", TEAL),
        ("blue", BLUE),
        ("violet", VIOLET),
        ("amber", AMBER),
        ("red", RED),
    ):
        marker = ET.SubElement(
            defs,
            "marker",
            {
                "id": f"arrow-{name}",
                "viewBox": "0 0 10 10",
                "refX": "9",
                "refY": "5",
                "markerWidth": "7",
                "markerHeight": "7",
                "orient": "auto-start-reverse",
            },
        )
        ET.SubElement(marker, "path", {"d": "M 0 0 L 10 5 L 0 10 z", "fill": color})

    ET.SubElement(
        svg,
        "rect",
        {"x": "0", "y": "0", "width": str(WIDTH), "height": str(HEIGHT), "fill": PAPER},
    )
    _svg_text(
        svg,
        44,
        56,
        "Nightingale · Core Clinical Evidence Schema",
        size=27,
        color=INK,
        weight="700",
    )
    _svg_text(
        svg,
        46,
        86,
        "Implemented PostgreSQL tables and service derivations from immutable note to decision, learning and patient receipt",
        size=13,
        color=MUTED,
    )

    for section in SECTIONS:
        ET.SubElement(
            svg,
            "rect",
            {
                "x": str(section.x),
                "y": str(section.y),
                "width": str(section.width),
                "height": str(section.height),
                "rx": "2",
                "fill": section.fill,
                "stroke": section.stroke,
                "stroke-width": "1.5",
            },
        )
        _svg_text(
            svg,
            section.x + 18,
            section.y + 25,
            section.title,
            size=15,
            color=INK,
            weight="700",
        )

    marker_names = {
        TEAL: "teal",
        BLUE: "blue",
        VIOLET: "violet",
        AMBER: "amber",
        RED: "red",
    }
    for edge in EDGES:
        points = " ".join(f"L {x} {y}" for x, y in edge.points[1:])
        attrs = {
            "d": f"M {edge.points[0][0]} {edge.points[0][1]} {points}",
            "fill": "none",
            "stroke": edge.color,
            "stroke-width": "2",
            "stroke-linejoin": "round",
            "stroke-linecap": "round",
            "marker-end": f"url(#arrow-{marker_names[edge.color]})",
        }
        if edge.dashed:
            attrs["stroke-dasharray"] = "7 6"
        ET.SubElement(svg, "path", attrs)
        label_width = max(58, len(edge.label) * 7 + 12)
        ET.SubElement(
            svg,
            "rect",
            {
                "x": str(edge.label_x - label_width // 2),
                "y": str(edge.label_y - 12),
                "width": str(label_width),
                "height": "17",
                "rx": "2",
                "fill": PAPER,
                "fill-opacity": "0.94",
            },
        )
        _svg_text(
            svg,
            edge.label_x,
            edge.label_y,
            edge.label,
            size=11,
            color=MUTED,
            weight="600",
            anchor="middle",
        )

    for node in NODES:
        attrs = {
            "x": str(node.x),
            "y": str(node.y),
            "width": str(node.width),
            "height": str(node.height),
            "rx": "2",
            "fill": node.fill,
            "stroke": node.color,
            "stroke-width": "1.7",
        }
        if node.dashed:
            attrs["stroke-dasharray"] = "7 5"
        ET.SubElement(svg, "rect", attrs)
        _svg_text(
            svg,
            node.x + 14,
            node.y + 25,
            node.title,
            size=18,
            color=node.color,
            weight="700",
        )
        _svg_text(
            svg,
            node.x + 14,
            node.y + 44,
            node.table,
            size=11,
            color=MUTED,
            family="Times New Roman, Times, serif",
        )
        detail_y = node.y + 66
        for detail in node.details:
            _svg_text(svg, node.x + 14, detail_y, detail, size=13, color=MUTED)
            detail_y += 17

    legends = (
        (
            40,
            1078,
            310,
            WHITE,
            TEAL,
            "solid = stored database FK / uniqueness",
            False,
        ),
        (
            370,
            1078,
            360,
            WHITE,
            VIOLET,
            "dashed = service derivation / projection / telemetry",
            True,
        ),
        (
            750,
            1078,
            810,
            WHITE,
            BLUE,
            "All clinical rows carry clinic_id · tenant-composite FKs + PostgreSQL RLS enforce scope",
            False,
        ),
    )
    for x, y, width, fill, stroke, text, dashed in legends:
        attrs = {
            "x": str(x),
            "y": str(y),
            "width": str(width),
            "height": "28",
            "rx": "2",
            "fill": fill,
            "stroke": stroke,
            "stroke-width": "1.4",
        }
        if dashed:
            attrs["stroke-dasharray"] = "6 4"
        ET.SubElement(svg, "rect", attrs)
        _svg_text(
            svg,
            x + width // 2,
            y + 19,
            text,
            size=11,
            color=stroke,
            weight="700",
            anchor="middle",
        )

    ET.indent(svg, space="  ")
    return ET.tostring(svg, encoding="utf-8", xml_declaration=True)


def main() -> None:
    drawio_bytes = build_drawio()
    svg_bytes = build_svg(drawio_bytes)
    DRAWIO_PATH.write_bytes(drawio_bytes)
    SVG_PATH.write_bytes(svg_bytes)

    ET.fromstring(drawio_bytes)
    svg_root = ET.fromstring(svg_bytes)
    metadata = next(
        element
        for element in svg_root.iter()
        if element.tag.endswith("metadata")
        and element.attrib.get("id") == "drawio-source-base64"
    )
    if base64.b64decode(metadata.text or "") != drawio_bytes:
        raise RuntimeError("SVG embedded Draw.io source does not match schema.drawio")
    print(DRAWIO_PATH)
    print(SVG_PATH)


if __name__ == "__main__":
    main()
