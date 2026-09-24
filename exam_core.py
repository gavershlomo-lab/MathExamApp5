"""Core logic for the math exam generator (V5).

This module has no Streamlit UI code so that it can be unit-tested on its own.
Sections:
  1. Data models (Pydantic)            5. Safe math-expression parser
  2. Gemini service (retry/fallback)   6. Figure rendering (bidi-aware)
  3. Scoring + validation              7. Word (OOXML) generation, RTL-correct
  4. Image processing                  8. PDF conversion via LibreOffice
"""
from __future__ import annotations

import hashlib
import html.entities
import io
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field, ValidationError

import docx
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Pt, RGBColor

import latex2mathml.converter
import mathml2omml

import matplotlib

import diagram_engine
from diagram_engine import safe_math as _sm
from diagram_engine.schemas import DiagramRecord

matplotlib.use("Agg")
from matplotlib import font_manager  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402  (OO API: thread-safe, no pyplot state)
from matplotlib.patches import Arc, Circle, Polygon  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

try:
    from bidi import get_display as _bidi_get_display  # python-bidi >= 0.5
except Exception:  # pragma: no cover
    try:
        from bidi.algorithm import get_display as _bidi_get_display
    except Exception:
        _bidi_get_display = None

try:
    import arabic_reshaper
except Exception:  # pragma: no cover
    arabic_reshaper = None


# ============================================================
# Configuration
# ============================================================
APP_TITLE = "מחולל מבחנים במתמטיקה"
APP_VERSION = "5.5.0"
DEFAULT_MODEL = "gemini-3.8-flash"
FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
TARGET_SCORE = Decimal("100")
POINT_TOLERANCE = Decimal("0.02")
MAX_IMAGE_SIDE = 2600
BODY_FONT = "Arial"
RTL_LANGUAGES = {"עברית", "ערבית"}
BIDI_LANG = {"עברית": "he-IL", "ערבית": "ar-SA"}
UNREADABLE_MARK = "[דרוש אימות מורה]"
SUPPORTED_FIGURE_TYPES = [
    "source_crop", "function_graph", "geometry", "bar_chart", "line_chart", "pie_chart", "generic_diagram",
]
RTL_CHARS = re.compile(r"[\u0590-\u05FF\u0600-\u06FF\u0750-\u077F\uFB1D-\uFDFF\uFE70-\uFEFF]")
ARABIC_CHARS = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


# ============================================================
# 1. Data models
# ============================================================
# NOTE: Models sent to Gemini as response_schema must NOT contain free-form dict fields
# (they produce `additionalProperties`, which the Gemini Developer API rejects).
# Numeric ranges are NOT enforced here: out-of-range model output is clamped afterwards
# instead of failing the whole question.

class DocumentLabels(BaseModel):
    exam_form: str
    solutions: str
    rubric: str
    question: str
    section: str
    points: str
    instructions: str
    final_answer: str
    full_credit: str
    partial_credit: str
    zero_credit: str
    carried_error: str
    common_errors: str
    teacher: str
    grade: str
    level: str
    exam_date: str
    duration: str
    minutes: str
    section_stage: str
    student_name: str
    class_name: str
    page: str
    of: str
    figure: str
    total: str


HEBREW_LABELS = DocumentLabels(
    exam_form="טופס בחינה", solutions="פתרון מלא", rubric="מחוון בדיקה", question="שאלה", section="סעיף",
    points="נקודות", instructions="הוראות לנבחן", final_answer="תשובה סופית", full_credit="ניקוד מלא",
    partial_credit="ניקוד חלקי", zero_credit="אפס נקודות", carried_error="טעות נגררת",
    common_errors="טעויות נפוצות", teacher="מורה", grade="שכבה", level="רמה", exam_date="תאריך",
    duration="משך הבחינה", minutes="דקות", section_stage="סעיף / שלב", student_name="שם התלמיד/ה",
    class_name="כיתה", page="עמוד", of="מתוך", figure="איור", total='סה"כ',
)

ENGLISH_LABELS = DocumentLabels(
    exam_form="Exam", solutions="Full Solutions", rubric="Marking Rubric", question="Question", section="Part",
    points="points", instructions="Instructions", final_answer="Final answer", full_credit="Full credit",
    partial_credit="Partial credit", zero_credit="Zero credit", carried_error="Carried-forward error",
    common_errors="Common errors", teacher="Teacher", grade="Grade", level="Level", exam_date="Date",
    duration="Duration", minutes="minutes", section_stage="Part / stage", student_name="Student name",
    class_name="Class", page="Page", of="of", figure="Figure", total="Total",
)


class CommonError(BaseModel):
    error: str
    severity: str = ""
    deduction_percent: float = 0.0


class RubricStep(BaseModel):
    section_id: str = ""
    stage_desc: str
    percentage: float
    full_credit: str = ""
    partial_credit: str = ""
    zero_credit: str = ""
    carried_over_error_policy: str = ""
    common_errors: list[CommonError] = Field(default_factory=list)


class SolutionStep(BaseModel):
    section_id: str = ""
    step_title: str
    content: str
    final_answer: str = ""


class QuestionSection(BaseModel):
    section_id: str
    text: str
    points: float


class FigureRef(BaseModel):
    description: str
    source_image_index: int = 1
    # Normalized [ymin, xmin, ymax, xmax] in 0..1000; empty list = whole image.
    bbox: list[int] = Field(default_factory=list)
    figure_type: str = "source_crop"
    rebuild_required: bool = False
    rebuild_confidence: float = 0.0
    render_engine: str = "source_crop"
    geogebra_commands: list[str] = Field(default_factory=list)
    spec_json: str = ""
    figure_id: str = ""


class QuestionAI(BaseModel):
    """What Gemini returns for ONE question."""
    topic: str
    text: str
    sections: list[QuestionSection] = Field(default_factory=list)
    figures: list[FigureRef] = Field(default_factory=list)
    solution_steps: list[SolutionStep] = Field(default_factory=list)
    rubric_steps: list[RubricStep] = Field(default_factory=list)


class ExamHeaderAI(BaseModel):
    translated_exam_name: str
    translated_grade: str
    translated_level: str
    translated_instructions: str
    labels: DocumentLabels


class VerificationItem(BaseModel):
    section_id: str = ""
    independent_final_answer: str
    proposed_final_answer: str = ""
    agrees: bool
    reasoning_agrees: bool
    source_text_agrees: bool
    comment: str = ""


class VerificationAI(BaseModel):
    items: list[VerificationItem] = Field(default_factory=list)
    source_reconstruction_agrees: bool
    reasoning_agrees: bool
    overall_agrees: bool
    notes: str = ""


class QuestionAnalysis(QuestionAI):
    question_number: int
    points: float
    verification: VerificationAI | None = None
    verification_error: str = ""
    teacher_verified: bool = False
    analysis_error: str = ""
    error_detail: str = ""
    # figure_id -> diagram-engine record (spec, validation, comparison, decision, teacher review, audit)
    diagrams: dict[str, DiagramRecord] = Field(default_factory=dict)


class ExamAnalysis(BaseModel):
    translated_exam_name: str
    translated_grade: str
    translated_level: str
    translated_instructions: str
    labels: DocumentLabels
    questions: list[QuestionAnalysis]
    model_used: str = ""


def empty_question(number: int, points: float, error: str = "") -> QuestionAnalysis:
    return QuestionAnalysis(question_number=number, points=points, topic="", text="", analysis_error=error)


# ============================================================
# 2. Gemini service
# ============================================================
QUESTION_SYSTEM_PROMPT = r"""
You are a senior mathematics assessment editor for the Israeli school system.
You receive photos of ONE exam question. Reconstruct it faithfully and produce a full solution and a marking rubric.

ACCURACY RULES
- Never invent unreadable information. Write exactly [דרוש אימות מורה] wherever a symbol, number, label or word cannot be read reliably.
- Preserve mathematical meaning, numbering, given data, restrictions and logical dependencies of the source.
- 'text' is the stem only. Put lettered/numbered sub-parts in 'sections' (section_id like א, ב, ג or a, b, c). Do not duplicate them in the stem.
- The sum of section points must equal the question's authoritative total points given to you.
- Put every mathematical expression in LaTeX: $...$ inline, $$...$$ for display math. Do not use $ for anything else.
- Write question text, sections, figure descriptions, solutions and rubric in the requested target language, at a professional academic level.
- solution_steps: explain reasoning and calculations fully. Set section_id on every step. Put the final answer of each section in final_answer of its last step.
- rubric_steps: percentages across the whole question must sum to exactly 100. Set section_id on each stage. The stages of one section should sum to (section points / question points * 100).
- Each rubric stage: full/partial/zero-credit criteria, carried-forward-error policy (credit later correct reasoning based on an earlier error unless the task became trivial), common errors with severity and deduction percent.

FIGURES (you EXTRACT structure; you never draw. A deterministic engine renders, validates and compares.)
- For every essential drawing/graph/chart add a FigureRef. source_image_index is 1-based within this question. bbox is [ymin,xmin,ymax,xmax] in 0..1000 tightly around the figure; [] for the whole image.
- Always fill spec_json with a JSON STRING of this form (omit the blocks that do not apply):
  {"diagram_type": "graph|geometry|chart|generic|unknown", "confidence": 0..1,
   "labels": [{"text":"A","bbox":[ymin,xmin,ymax,xmax],"confidence":0..1,"alternatives":["4"]}],
   "observed": {"point_labels":[...], "num_points":n, "num_segments":n, "num_circles":n, "num_curves":n,
                "right_angle_marks":n, "equal_mark_groups":n, "parallel_mark_groups":n, "angle_values":["40"],
                "x_intercepts":[...], "y_intercept":v, "open_endpoints":[[x,y]], "closed_endpoints":[[x,y]], "num_bars":n},
   "graph": {"axes":{"x_min":..,"x_max":..,"y_min":..,"y_max":..,"x_step":1,"y_step":1,"x_label":"x","y_label":"y","show_grid":true},
             "curves":[{"id":"f","label":"f(x)","expression":"x^2-4",
                        "pieces":[{"expression":"x+1","x_from":-2,"x_to":1,"left_closed":true,"right_closed":false}]}],
             "points":[{"name":"A","x":2,"y":0,"on_curve":"f","style":"closed|open"}],
             "asymptotes":[{"kind":"vertical|horizontal","value":3}]},
   "geometry": {"points":[{"id":"A","x":0,"y":0}], "segments":[{"a":"A","b":"B","style":"solid|dashed"}], "lines":[], "rays":[],
                "circles":[{"center":"O","through":"A"}], "arcs":[{"center":"O","start":"A","end":"B"}],
                "angle_marks":[{"vertex":"B","a":"A","b":"C","kind":"arc|right","value":"40"}],
                "equal_marks":[{"segments":[["A","C"],["B","C"]],"ticks":1}], "parallel_marks":[{"segments":[["A","B"],["C","D"]],"arrows":1}],
                "length_labels":[{"a":"A","b":"B","text":"5"}], "constraints":[]},
   "chart": {"kind":"bar|histogram|pie|line|frequency_table|two_way_table","categories":[],"values":[],"bins":[],
             "row_labels":[],"col_labels":[],"table":[[...]],"title":"","x_label":"","y_label":"","show_percentages":false},
   "generic": {"width":100,"height":60,"shapes":[{"kind":"rect|ellipse","x":..,"y":..,"w":..,"h":..,"text":""}],"arrows":[{"x1":..,"y1":..,"x2":..,"y2":..,"label":""}],
               "labels":[{"text":"A","x":..,"y":..}],"dimensions":[{"x1":..,"y1":..,"x2":..,"y2":..,"text":"8 מטרים","attach":["A","G"]}]},
   "subtype": "formula_graph|qualitative_graph|multi_choice_graphs|triangle_geometry|circle_geometry|polygon_geometry|analytic_geometry|coordinate_circle|graph_with_geometry|scatter_plot|histogram|bar_chart|pie_chart|normal_distribution_schematic|numeric_table|frequency_table|two_way_table|voxel_structure|cuboid|cylinder|cylinder_in_box|vector_box",
   "graph_topology" (graph WITHOUT a formula - never invent an equation): {"function_label":"f(x)","axes":{...,"show_numbers":false},
             "landmarks":[{"x":6,"y":1,"label":"(6 , a)","kind":"max|min|x_intercept|y_intercept|marked","style":"closed|open|none"}],
             "asymptotes":[...],"branches":[{"landmarks":[0,1,2],"left":{"toward":"asymptote|plus_inf|minus_inf|stop","value":0},"right":{...}}]},
   "multi_graph" (options I, II, III, IV - keep them SEPARATE): {"options":[{"label":"I","topology":{...}} or {"label":"I","formula":{...graph...}}]},
   "mixed" (curve + construction in one coordinate system): {"graph":{...},"geometry":{...,"constraints":[{"type":"on_curve","points":["A"],"curve":"f"}]}},
   "scatter": {"axes":{...},"points":[[x,y],...]},  "normal": {"percentages":["0.5%",...],"boxed_regions":[0,1],"answer_boxes":11},
   "table": {"header_rows":1,"header_columns":1,"rows":[[{"text":"..."}]]},
   "spatial": {"voxel":{"plate":[4,4],"columns":[{"x":0,"y":0,"height":2}],"ambiguous":false},
               "solids":[{"id":"box","kind":"cuboid|cylinder|polyhedron","dims":{"width":..,"depth":..,"height":..,"radius":..},
                          "vertices":{"A":[x,y,z]},"edges":[["A","B"]],"hidden_edges":[["A","B"]],"face_text":""}],
               "dimensions":[{"solid":"box","measure":"width|depth|height|radius|diameter","text":"24 ס\"מ"}],
               "vectors":[{"from":"A","to":"B","label":"u"}],"points_on_edges":[{"id":"F","a":"B","b":"C","ratio":0.5}],
               "relations":[{"type":"inside|touching_base|touching_side|separate","a":"cyl","b":"box"}]}}
- Geometry relations you may report as constraints ONLY when they are written in the question or marked by an explicit symbol:
  equal_length, parallel, perpendicular, right_angle, midpoint, point_order ["B","C","D"], point_on_circle, diameter, tangent,
  equilateral, isosceles, ... Incidence you SEE (a point on a line/circle, the order of points on a line) may be reported with
  source "image". A circle may be given by "through_points" when its centre is not marked.
- Cube structures: if some columns are hidden and their height cannot be seen, set voxel.ambiguous=true (never guess hidden cubes).
- Radius vs diameter: report exactly what the dimension line measures in the source.
- STRICT RULES:
  * Report ONLY what is visible. Never add points, segments, marks, labels, values or equations that are not in the source.
  * "observed" = counts/facts of the SOURCE image exactly as seen; it is used to check the reconstruction.
  * Every label: its confidence; if a character could be another one (6/8, B/8, l/1) set confidence < 0.8 and list alternatives.
  * Graph expression: calculator syntax in x (+ - * / ^ ( ) sqrt cbrt abs ln log exp sin cos tan pi e), NO LaTeX. Give an
    expression only if it is stated in the question or unambiguous; otherwise leave curves empty and set confidence low.
  * Geometry coordinates: approximate positions of the drawing in any units, y pointing UP. Drawings are often NOT to scale:
    never infer equal lengths/parallel/right angles from appearance; report them only as explicit marks.
  * Chart values: use the numbers written in the source/question; do not estimate from bar heights unless no numbers exist
    (then confidence <= 0.7). Pie: show_percentages=true only if percentages are printed in the source.
  * If unsure what the figure is, use diagram_type "unknown" - the original image will be used.
- figure_type (legacy) may be source_crop; rebuild_required/rebuild_confidence are ignored by the engine.
- geogebra_commands: optional construction commands (teacher reference only).
""".strip()

HEADER_SYSTEM_PROMPT = """
You translate exam header metadata for a mathematics exam. Translate the exam title, grade, level and the general
instructions into the requested target language, and provide document labels in that language.
Do not translate proper names (school name, teacher name).
""".strip()

VERIFY_SYSTEM_PROMPT = r"""
You are an independent mathematics examiner checking another teacher's answer key.
1) First compare the reconstructed stem, every section, all numbers, symbols, domains/restrictions and figure-dependent data with the original photos. source_reconstruction_agrees=false if anything material is missing, altered or unreadable.
2) Using the original photos, solve every section yourself from scratch before judging the proposed solution.
3) Check the proposed reasoning step-by-step, not only its final answer. reasoning_agrees=false if a step is invalid, unjustified, circular, uses a false assumption or loses/extraneously adds solutions, even when the final answer happens to match.
4) Then compare the final answer of EVERY section with your independent final answer. Treat mathematically equivalent forms as agreeing (e.g. 0.5 and 1/2, x=2 or x=-3 and x∈{-3,2}).
5) Return exactly one VerificationItem for every section_id in the question. If the question has no explicit sections, return exactly one item with section_id="".
6) For each item set agrees=false whenever the final answers differ or the proposed answer is missing; set source_text_agrees and reasoning_agrees independently.
7) overall_agrees may be true only if source reconstruction, reasoning and every final answer all agree.
Use LaTeX with $...$ for math. Write comments in the requested target language, briefly.
""".strip()


class GeminiError(Exception):
    pass


GEMINI_HTTP_TIMEOUT_MS = 180_000   # a stuck request must never freeze the analysis (retries/fallback handle the rest)


def make_client(api_key: str):  # patched in tests
    from google import genai

    try:
        from google.genai import types

        return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_HTTP_TIMEOUT_MS))
    except (ImportError, AttributeError, TypeError):  # older SDKs without HttpOptions
        return genai.Client(api_key=api_key)


def _error_code(exc: Exception) -> int | None:
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    match = re.match(r"\s*(\d{3})\b", str(exc))
    return int(match.group(1)) if match else None


def _is_not_found(exc: Exception) -> bool:
    return _error_code(exc) == 404 or "NOT_FOUND" in str(exc)


def _is_overloaded(exc: Exception) -> bool:
    text = str(exc)
    return _error_code(exc) in (500, 502, 503, 504) or any(k in text for k in ("UNAVAILABLE", "overloaded", "INTERNAL"))


def _is_retryable(exc: Exception) -> bool:
    code = _error_code(exc)
    text = str(exc)
    return code in (408, 429, 500, 502, 503, 504) or any(
        k in text for k in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL", "timed out")
    )


def _strip_json_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


def parse_quota_error(exc: Exception) -> dict[str, Any]:
    """Extract Google quota details from a 429 error (works on the SDK exception text / details)."""
    text = str(exc) + " " + str(getattr(exc, "details", "") or "")
    info: dict[str, Any] = {"per_day": False, "limit": None, "retry_after": None, "free_tier": "free_tier" in text.lower() or "FreeTier" in text}
    quota_ids = re.findall(r"quotaId'?\"?\s*:\s*'?\"?([A-Za-z-]+)", text)
    info["per_day"] = any("PerDay" in q for q in quota_ids)
    m = re.search(r"quotaValue'?\"?\s*:\s*'?\"?(\d+)", text) or re.search(r"limit:\s*(\d+)", text)
    if m:
        info["limit"] = int(m.group(1))
    m = re.search(r"retryDelay'?\"?\s*:\s*'?\"?([\d.]+)s", text) or re.search(r"retry in ([\d.]+)\s*s", text)
    if m:
        info["retry_after"] = float(m.group(1))
    return info


def friendly_error(exc: Exception | str) -> str:
    """Short Hebrew explanation of a Gemini failure (the raw text stays available for the technical details)."""
    text = str(exc)
    code = _error_code(exc) if isinstance(exc, Exception) else None
    if code is None:
        m = re.search(r"\b(4\d\d|5\d\d)\b", text)
        code = int(m.group(1)) if m else None
    if code == 429 or "RESOURCE_EXHAUSTED" in text:
        info = parse_quota_error(exc if isinstance(exc, Exception) else Exception(text))
        if info["per_day"]:
            return ("המכסה היומית של מפתח ה-Gemini נוצלה עבור כל המודלים הזמינים. אפשר לנסות שוב מחר, "
                    "להשתמש במפתח אחר, או להפעיל חיוב (Billing) בפרויקט ב-Google AI Studio.")
        tier = " (השכבה החינמית)" if info["free_tier"] else ""
        limit = f" — עד {info['limit']} בקשות בדקה" if info["limit"] else ""
        return (f"חריגה ממכסת הבקשות של Gemini{tier}{limit}. המערכת מאטה אוטומטית את הקצב; "
                "אם השגיאה חוזרת, המתינו דקה ולחצו 'נסה שוב רק את השאלות שנכשלו'.")
    if code in (401, 403) or "API_KEY_INVALID" in text or "API key not valid" in text or "PERMISSION_DENIED" in text:
        return "מפתח ה-API של Gemini אינו תקין או שאין לו הרשאה. בדקו את המפתח ב-Google AI Studio."
    if code == 404 or "NOT_FOUND" in text:
        return "המודל שהוגדר אינו זמין למפתח זה, ולא נמצא מודל חלופי."
    if code == 400 and ("location" in text.lower() or "FAILED_PRECONDITION" in text):
        return "Gemini API אינו זמין באזור או בפרויקט הזה (FAILED_PRECONDITION)."
    if code in (500, 502, 503, 504) or "UNAVAILABLE" in text:
        return ("שרתי Gemini עמוסים כרגע (שגיאת שרת של Google, לא תקלה במפתח או במכסה). המערכת ניסתה גם מודלים חלופיים. "
                "נסו שוב בעוד כמה דקות, או הזינו בשדה 'מודל Gemini' את gemini-3.6-flash ולחצו 'נסה שוב רק את השאלות שנכשלו'.")
    if "MAX_TOKENS" in text:
        return "התשובה של Gemini ארוכה מדי ונקטעה. נסו לפצל את השאלה לשתי תמונות/שאלות."
    return "קריאת Gemini נכשלה. פרטים טכניים מופיעים למטה."


class GeminiService:
    """Gemini wrapper: structured output, retries, model fallback and an adaptive rate limiter.

    The limiter starts unlimited (paid keys stay fast). On the first per-minute 429 it learns the quota from the error
    (e.g. 5 requests/minute on the free tier) and the requested retry delay, and paces every thread accordingly.
    """

    WINDOW = 60.0

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, client: Any = None, max_attempts: int = 4,
                 rpm: int | None = None, sleep: Callable[[float], None] | None = None):
        self.client = client or make_client(api_key)
        self.model = model or DEFAULT_MODEL
        self.max_attempts = max_attempts
        self.rpm = rpm
        self.status = ""
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._calls: list[float] = []
        self._not_before = 0.0
        self._models_tried: set[str] = set()
        self._server_errors: dict[str, int] = {}
        self.notes: list[str] = []
        self.rate_limit_events = 0

    # ---------------- rate limiting
    def _acquire(self) -> None:
        while True:
            with self._rate_lock:
                now = time.monotonic()
                wait = max(0.0, self._not_before - now)
                if not wait and self.rpm:
                    self._calls = [t for t in self._calls if now - t < self.WINDOW]
                    if len(self._calls) >= self.rpm:
                        wait = self.WINDOW - (now - self._calls[0]) + 0.5
                if not wait:
                    self._calls.append(now)
                    self.status = ""
                    return
            self.status = f"ממתין למכסת Gemini ({int(wait) + 1} שניות)…"
            self._sleep(min(wait, 2.0))

    def _on_rate_limit(self, exc: Exception, failed_model: str | None = None) -> None:
        info = parse_quota_error(exc)
        with self._rate_lock:
            self.rate_limit_events += 1
            if info["limit"]:
                self.rpm = min(self.rpm or info["limit"], info["limit"])
            elif not self.rpm:
                self.rpm = 5
            delay = info["retry_after"] if info["retry_after"] is not None else 10.0
            # A late 429 from a model that another worker already abandoned must not pause the new model.
            if failed_model is None or failed_model == self.model:
                self._not_before = max(self._not_before, time.monotonic() + delay + 1.0)

    def _next_model(self, failed_model: str | None = None) -> str | None:
        with self._lock:
            failed_model = failed_model or self.model
            self._models_tried.add(failed_model)
            candidates = [m for m in FALLBACK_MODELS if m not in self._models_tried]
            try:
                available = {m.name.split("/")[-1] for m in self.client.models.list()}
                candidates = [m for m in candidates if m in available] or sorted(
                    (m for m in available if "flash" in m and not any(t in m for t in ("lite", "image", "tts", "live", "transcribe", "omni"))
                     and m not in self._models_tried),
                    reverse=True,
                )
            except Exception:
                pass
            if not candidates:
                return None
            self.model = candidates[0]
            with self._rate_lock:  # quotas are per model: start the new model without the old pause
                self._calls, self._not_before = [], 0.0
            return self.model

    # ---------------- main call
    def generate(
        self,
        parts: list,
        schema: type[BaseModel],
        system: str,
        max_output_tokens: int = 32768,
        thinking_level: str | None = None,
    ) -> BaseModel:
        from google.genai import types

        last_exc: Exception | None = None
        attempt = 0
        rate_waits = 0
        while attempt < self.max_attempts:
            attempt += 1
            model = self.model
            try:
                self._acquire()
                config_kwargs: dict[str, Any] = dict(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=schema,
                    max_output_tokens=max_output_tokens,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                )
                if hasattr(types, "MediaResolution"):
                    config_kwargs["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_HIGH
                if thinking_level and hasattr(types, "ThinkingConfig"):
                    config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
                response = self.client.models.generate_content(
                    model=model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                candidates = getattr(response, "candidates", None) or []
                finish = str(getattr(candidates[0], "finish_reason", "") or "") if candidates else ""
                if "MAX_TOKENS" in finish:
                    raise GeminiError("התשובה של Gemini נקטעה (MAX_TOKENS).")
                if not candidates:
                    feedback = getattr(response, "prompt_feedback", None)
                    raise GeminiError(f"Gemini לא החזיר תשובה ({feedback}).")
                parsed = getattr(response, "parsed", None)
                if isinstance(parsed, schema):
                    return parsed
                if parsed is not None:
                    return schema.model_validate(parsed)
                return schema.model_validate_json(_strip_json_fences(response.text or ""))
            except GeminiError as exc:
                last_exc = exc
                if "MAX_TOKENS" in str(exc) and max_output_tokens < 65536:
                    max_output_tokens = 65536
                    continue
                if attempt >= self.max_attempts:
                    break
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                # ValueError also covers SDK-side schema/config problems; retry is harmless.
                last_exc = exc
                if "additionalProperties" in str(exc):
                    break
            except Exception as exc:  # network / API errors
                last_exc = exc
                if _error_code(exc) == 429 or "RESOURCE_EXHAUSTED" in str(exc):
                    info = parse_quota_error(exc)
                    if info["per_day"]:
                        # Daily quota of this model is gone; other models have their own quota.
                        if self._next_model(model):
                            attempt -= 1
                            continue
                        break
                    self._on_rate_limit(exc, model)
                    rate_waits += 1
                    if rate_waits <= 12:
                        attempt -= 1  # waiting for quota is not a failed attempt
                        continue
                    break
                if _is_not_found(exc):
                    if self._next_model(model):
                        attempt -= 1  # a model switch does not consume an attempt
                        continue
                    break
                if not _is_retryable(exc):
                    break
                if _is_overloaded(exc):
                    # 500/503 "overloaded" is per model (typical right after a model launch):
                    # after 2 failures on the same model, move to the next Flash model instead of giving up.
                    self._server_errors[model] = self._server_errors.get(model, 0) + 1
                    if self._server_errors[model] >= 2 and self.model == model and self._next_model(model):
                        self.notes.append(f"המודל {model} היה עמוס — הפענוח עבר אוטומטית ל-{self.model}.")
                        attempt -= 1
                        continue
                self._sleep(min(30.0, 3 * 2 ** attempt + random.random()))
        raise GeminiError(f"קריאת Gemini נכשלה (מודל {self.model}): {last_exc}")


def _meta_for_prompt(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "school_name": meta.get("school_name", ""),
        "exam_name": meta.get("exam_name", ""),
        "grade": meta.get("grade", ""),
        "level": meta.get("level", ""),
        "language": meta.get("language", "עברית"),
    }


def _image_parts(question: dict[str, Any]) -> list:
    from google.genai import types

    parts = []
    for idx, image_bytes in enumerate(question.get("images", []), 1):
        parts.append(types.Part.from_text(text=f"Source image {idx}:"))
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))
    return parts


def analyze_question(service: GeminiService, meta: dict[str, Any], question: dict[str, Any]) -> QuestionAnalysis:
    from google.genai import types

    q_num = int(question["question_number"])
    points = float(question["points"])
    language = meta.get("language", "עברית")
    intro = (
        f"Question number: {q_num}\n"
        f"Authoritative total points for this question: {points:g}\n"
        f"Target language: {language}\n"
        f"Exam context: {json.dumps(_meta_for_prompt(meta), ensure_ascii=False)}\n"
        "Reconstruct this single question from the attached photos."
    )
    parts = [types.Part.from_text(text=intro), *_image_parts(question)]
    # High thinking is intentionally used for mathematical solving. It is slower, but materially safer.
    ai = service.generate(parts, QuestionAI, QUESTION_SYSTEM_PROMPT, thinking_level="high")
    q = postprocess_question(ai, q_num, points, len(question.get("images", [])))
    attach_diagrams(q, question.get("images", []), ai_model=service.model)
    return q


def analyze_header(service: GeminiService, meta: dict[str, Any]) -> ExamHeaderAI:
    from google.genai import types

    payload = {
        "target_language": meta.get("language"),
        "exam_name": meta.get("exam_name", ""),
        "grade": meta.get("grade", ""),
        "level": meta.get("level", ""),
        "instructions": meta.get("instructions", ""),
        "english_label_examples": ENGLISH_LABELS.model_dump(),
    }
    parts = [types.Part.from_text(text=json.dumps(payload, ensure_ascii=False, indent=2))]
    return service.generate(parts, ExamHeaderAI, HEADER_SYSTEM_PROMPT, max_output_tokens=8192)


def verify_question(service: GeminiService, meta: dict[str, Any], question: dict[str, Any], q: QuestionAnalysis) -> VerificationAI:
    from google.genai import types

    proposed = {
        "stem": q.text,
        "sections": [s.model_dump() for s in q.sections],
        "proposed_solution_steps": [s.model_dump() for s in q.solution_steps],
    }
    intro = (
        f"Target language: {meta.get('language', 'עברית')}\n"
        "Reconstructed question and proposed answer key (verify against the photos):\n"
        + json.dumps(proposed, ensure_ascii=False, indent=2)
    )
    parts = [types.Part.from_text(text=intro), *_image_parts(question)]
    return service.generate(parts, VerificationAI, VERIFY_SYSTEM_PROMPT, max_output_tokens=32768, thinking_level="high")


def default_header(meta: dict[str, Any]) -> ExamHeaderAI:
    language = meta.get("language", "עברית")
    return ExamHeaderAI(
        translated_exam_name=str(meta.get("exam_name", "")),
        translated_grade=str(meta.get("grade", "")),
        translated_level=str(meta.get("level", "")),
        translated_instructions=str(meta.get("instructions", "")),
        labels=HEBREW_LABELS if language == "עברית" else ENGLISH_LABELS,
    )


def run_full_analysis(
    service: GeminiService,
    meta: dict[str, Any],
    questions_data: list[dict[str, Any]],
    verify: bool = True,
    progress: Callable[[float, str], None] | None = None,
    max_workers: int = 3,
    existing: ExamAnalysis | None = None,
    only: set[int] | None = None,
) -> tuple[ExamAnalysis, list[str]]:
    """Analyze each question separately (parallel, rate-limited), then optionally verify each one independently.

    `existing` + `only` re-run just some questions (e.g. the ones that failed on quota) and keep the rest.
    Always returns an ExamAnalysis; failed questions carry `analysis_error` so the teacher can retry them.
    """
    from concurrent.futures import FIRST_COMPLETED, wait

    notes: list[str] = []
    language = meta.get("language", "עברית")
    todo = [q for q in questions_data if only is None or int(q["question_number"]) in only]
    need_header = existing is None and language != "עברית"
    total_jobs = len(todo) * (2 if verify else 1) + (1 if need_header else 0)
    done = 0
    last_msg = "מתחיל…"

    def report(msg: str | None = None) -> None:
        nonlocal last_msg
        if msg:
            last_msg = msg
        if progress:
            shown = service.status or last_msg
            progress(min(1.0, done / max(1, total_jobs)), shown)

    def drain(futures: dict, on_done: Callable[[Any, Any], str]) -> None:
        nonlocal done
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for fut in finished:
                msg = on_done(fut, futures[fut])
                done += 1
                report(msg)
            if not finished:
                report()

    if existing is not None:
        header = ExamHeaderAI(
            translated_exam_name=existing.translated_exam_name, translated_grade=existing.translated_grade,
            translated_level=existing.translated_level, translated_instructions=existing.translated_instructions,
            labels=existing.labels,
        )
    else:
        header = default_header(meta)
    report("שולח את השאלות ל-Gemini…")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        if need_header:
            fut = pool.submit(analyze_header, service, meta)

            def header_done(f, _):
                nonlocal header
                try:
                    header = f.result()
                except Exception as exc:
                    notes.append(f"תרגום כותרות נכשל ({friendly_error(exc)}); נעשה שימוש בתוויות באנגלית.")
                return "תורגמו כותרות המסמך"

            drain({fut: None}, header_done)

        results: dict[int, QuestionAnalysis] = {}

        def q_done(f, q):
            num = int(q["question_number"])
            try:
                results[num] = f.result()
            except Exception as exc:
                failed = empty_question(num, float(q["points"]), f"הפענוח נכשל: {friendly_error(exc)}")
                failed.error_detail = str(exc)
                results[num] = failed
            return f"פוענחה שאלה {num}"

        drain({pool.submit(analyze_question, service, meta, q): q for q in todo}, q_done)

        if verify:
            by_num = {int(q["question_number"]): q for q in todo}
            ok = {num: qa for num, qa in results.items() if not qa.analysis_error}
            done += len(results) - len(ok)  # skipped verifications

            def v_done(f, num):
                try:
                    results[num].verification = f.result()
                except Exception as exc:
                    results[num].verification_error = f"האימות נכשל: {friendly_error(exc)}"
                return f"אומתה שאלה {num}"

            drain({pool.submit(verify_question, service, meta, by_num[num], qa): num for num, qa in ok.items()}, v_done)

    merged: dict[int, QuestionAnalysis] = {q.question_number: q for q in existing.questions} if existing else {}
    merged.update(results)
    notes.extend(dict.fromkeys(service.notes))
    if service.rate_limit_events:
        notes.append(
            f"Gemini הגביל את קצב הבקשות (עד {service.rpm} בדקה), ולכן הפענוח הואט אוטומטית."
        )
    exam = ExamAnalysis(
        translated_exam_name=header.translated_exam_name,
        translated_grade=header.translated_grade,
        translated_level=header.translated_level,
        translated_instructions=header.translated_instructions,
        labels=header.labels,
        questions=[merged[k] for k in sorted(merged)],
        model_used=service.model,
    )
    return exam, notes


def estimate_calls(n_questions: int, verify: bool, language: str) -> int:
    return n_questions * (2 if verify else 1) + (0 if language == "עברית" else 1)


def postprocess_question(ai: QuestionAI, number: int, points: float, image_count: int) -> QuestionAnalysis:
    q = QuestionAnalysis(question_number=number, points=points, **ai.model_dump())
    for sec in q.sections:
        sec.section_id = clean_section_id(sec.section_id)
        sec.points = max(0.0, float(sec.points))
    for step in q.solution_steps:
        step.section_id = clean_section_id(step.section_id)
    for rub in q.rubric_steps:
        rub.section_id = clean_section_id(rub.section_id)
        rub.percentage = min(100.0, max(0.0, float(rub.percentage)))
        for err in rub.common_errors:
            err.deduction_percent = min(100.0, max(0.0, float(err.deduction_percent)))
    seen_ids: set[str] = set()
    for i, fig in enumerate(q.figures, 1):
        if not fig.figure_id or fig.figure_id in seen_ids:
            fig.figure_id = f"q{number}f{i}"
        seen_ids.add(fig.figure_id)
    for fig in q.figures:
        fig.source_image_index = min(max(1, int(fig.source_image_index)), max(1, image_count))
        fig.rebuild_confidence = min(1.0, max(0.0, float(fig.rebuild_confidence)))
        if fig.figure_type not in SUPPORTED_FIGURE_TYPES:
            fig.figure_type = "source_crop"
            fig.rebuild_required = False
        if fig.bbox and (len(fig.bbox) != 4 or not bbox_is_valid(fig.bbox)):
            fig.bbox = []
    return q


def clean_section_id(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^(?:סעיף|תת[- ]?סעיף|section|part)\s+", "", text, flags=re.IGNORECASE)
    return text.strip().strip(".):-–' ").strip()


# ============================================================
# 3. Scoring + validation
# ============================================================
def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        if result != result:  # NaN
            return default
        return result
    except (TypeError, ValueError):
        return default


def normalize_decimal(value: Any) -> Decimal:
    return Decimal(str(safe_float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt_points(value: Any) -> str:
    d = normalize_decimal(value)
    return f"{d:f}".rstrip("0").rstrip(".") if "." in f"{d:f}" else f"{d:f}"


def bbox_is_valid(bbox: list[int]) -> bool:
    if len(bbox) != 4:
        return False
    y1, x1, y2, x2 = bbox
    return all(0 <= v <= 1000 for v in bbox) and y1 < y2 and x1 < x2


def validate_choice_groups(num_questions: int, groups: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    all_expected = set(range(1, num_questions + 1))
    seen: list[int] = []
    for i, group in enumerate(groups, 1):
        questions = [int(x) for x in group.get("questions", [])]
        required = int(group.get("required", 0))
        if not questions:
            errors.append(f"קבוצת בחירה {i} ריקה.")
            continue
        if required < 1 or required > len(questions):
            errors.append(f"בקבוצה {i} מספר השאלות הנדרש אינו חוקי.")
        seen.extend(questions)
    duplicated = sorted({q for q in seen if seen.count(q) > 1})
    missing = sorted(all_expected - set(seen))
    extra = sorted(set(seen) - all_expected)
    if duplicated:
        errors.append(f"השאלות הבאות מופיעות ביותר מקבוצה אחת: {duplicated}")
    if missing:
        errors.append(f"השאלות הבאות אינן משויכות לקבוצת בחירה: {missing}")
    if extra:
        errors.append(f"מספרי שאלות לא חוקיים בקבוצות: {extra}")
    return errors


def suggested_points(meta: dict[str, Any]) -> dict[int, float]:
    """Default points. With no choice the remainder goes to the last question so the total is exactly 100."""
    n = int(meta.get("num_questions", 1))
    groups = meta.get("choice_groups", []) or [{"questions": list(range(1, n + 1)), "required": n}]
    total_required = sum(int(g.get("required", 0)) for g in groups) or n
    each = (TARGET_SCORE / Decimal(total_required)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    result = {q: float(each) for q in range(1, n + 1)}
    no_choice = all(int(g.get("required", 0)) == len(g.get("questions", [])) for g in groups)
    if no_choice and n:
        result[n] = float(TARGET_SCORE - each * (n - 1))
    return result


def route_tolerance(groups: list[dict[str, Any]]) -> Decimal:
    """Allow rounding of 0.005 per required question (e.g. 7 × 14.29 = 100.03) but never less than 0.02."""
    required = sum(int(g.get("required", 0)) for g in groups)
    return max(POINT_TOLERANCE, Decimal("0.005") * required + Decimal("0.001"))


def validate_points_structure(meta: dict[str, Any], points_by_q: dict[int, Any]) -> list[str]:
    errors: list[str] = []
    groups = meta.get("choice_groups", [])
    route_total = Decimal("0")
    for idx, group in enumerate(groups, 1):
        nums = [int(x) for x in group.get("questions", [])]
        required = int(group.get("required", 0))
        pts = [normalize_decimal(points_by_q[n]) for n in nums if n in points_by_q]
        if len(pts) != len(nums):
            continue
        if required < len(nums):
            if len(set(pts)) != 1:
                errors.append(
                    f"קבוצת בחירה {idx}: כאשר בוחרים {required} מתוך {len(nums)}, כל השאלות בקבוצה חייבות להיות באותו ניקוד."
                )
            elif pts:
                route_total += pts[0] * required
        else:
            route_total += sum(pts, Decimal("0"))
    if groups and abs(route_total - TARGET_SCORE) > route_tolerance(groups):
        errors.append(f"סך הניקוד במסלול בחירה חוקי הוא {route_total} ולא 100.")
    return errors


def question_texts(q: QuestionAnalysis) -> str:
    return "\n".join(
        [
            q.text, q.topic,
            *(s.text for s in q.sections),
            *(s.content for s in q.solution_steps),
            *(s.final_answer for s in q.solution_steps),
            *(r.stage_desc for r in q.rubric_steps),
            *(r.full_credit for r in q.rubric_steps),
            *(r.partial_credit for r in q.rubric_steps),
            *(r.zero_credit for r in q.rubric_steps),
            *(r.carried_over_error_policy for r in q.rubric_steps),
            *(f.description for f in q.figures),
        ]
    )


def validate_exam(exam: ExamAnalysis, meta: dict[str, Any], questions_data: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    expected_n = int(meta.get("num_questions", 0))
    actual_numbers = [q.question_number for q in exam.questions]
    if len(exam.questions) != expected_n or set(actual_numbers) != set(range(1, expected_n + 1)):
        errors.append(f"מספור השאלות ({sorted(actual_numbers)}) אינו תואם את מבנה הבחינה ({expected_n} שאלות).")

    source_by_num = {int(q["question_number"]): q for q in questions_data}
    for q in exam.questions:
        num = q.question_number
        source = source_by_num.get(num, {})
        if q.analysis_error:
            errors.append(f"שאלה {num}: {q.analysis_error} — יש לפענח אותה מחדש.")
            continue
        total = normalize_decimal(q.points)
        if not q.text.strip() and not q.sections:
            errors.append(f"שאלה {num}: אין נוסח שאלה.")
        section_sum = sum((normalize_decimal(s.points) for s in q.sections), Decimal("0"))
        if q.sections and abs(section_sum - total) > POINT_TOLERANCE:
            errors.append(f"שאלה {num}: סכום ניקוד הסעיפים הוא {section_sum}, אך ניקוד השאלה הוא {total}.")
        ids = [s.section_id for s in q.sections]
        if len(ids) != len(set(ids)):
            errors.append(f"שאלה {num}: יש מזהי סעיפים כפולים.")
        rubric_sum = sum((normalize_decimal(r.percentage) for r in q.rubric_steps), Decimal("0"))
        if q.rubric_steps and abs(rubric_sum - Decimal("100")) > POINT_TOLERANCE:
            errors.append(f"שאלה {num}: סכום אחוזי המחוון הוא {rubric_sum}% במקום 100%.")
        if not q.solution_steps:
            errors.append(f"שאלה {num}: לא קיימים שלבי פתרון.")
        if not q.rubric_steps:
            errors.append(f"שאלה {num}: לא קיימים שלבי מחוון.")

        # A solution is not complete until every requested part has an explicit final answer.
        expected_solution_ids = [s.section_id for s in q.sections] if q.sections else [""]
        for section_id in expected_solution_ids:
            matching = [s for s in q.solution_steps if s.section_id == section_id]
            label = f", סעיף {section_id}" if section_id else ""
            if not matching:
                errors.append(f"שאלה {num}{label}: חסר פתרון המשויך לסעיף.")
            elif not any(s.final_answer.strip() for s in matching):
                errors.append(f"שאלה {num}{label}: חסרה תשובה סופית מפורשת.")

        # Rubric per section must match section points (warning — a stage may legitimately span sections).
        if q.sections and q.rubric_steps and total > 0:
            for sec in q.sections:
                stages = [r for r in q.rubric_steps if r.section_id == sec.section_id]
                if not stages:
                    warnings.append(f"שאלה {num}, סעיף {sec.section_id}: אין שלבי מחוון המשויכים לסעיף.")
                    continue
                pct = sum((normalize_decimal(r.percentage) for r in stages), Decimal("0"))
                pts = (pct * total / Decimal("100")).quantize(Decimal("0.01"))
                if abs(pts - normalize_decimal(sec.points)) > Decimal("0.05"):
                    warnings.append(
                        f"שאלה {num}, סעיף {sec.section_id}: המחוון מקצה {pts} נק' אך הסעיף שווה {normalize_decimal(sec.points)} נק'."
                    )
        image_count = len(source.get("images", []))
        for fig_idx, fig in enumerate(q.figures, 1):
            if fig.source_image_index < 1 or fig.source_image_index > image_count:
                errors.append(f"שאלה {num}, תרשים {fig_idx}: תמונת מקור {fig.source_image_index} אינה קיימת.")
            if fig.bbox and not bbox_is_valid(fig.bbox):
                errors.append(f"שאלה {num}, תרשים {fig_idx}: bbox אינו חוקי.")
            rec = q.diagrams.get(fig.figure_id)
            if rec is not None and rec.spec is not None and rec.spec.diagram_type != "unknown":
                if rec.review.status == "pending" and rec.decision.action in ("review", "draft", "high_confidence_preview"):
                    warnings.append(f"שאלה {num}, תרשים {fig_idx}: השחזור ממתין לאישור מורה — עד לאישור ישולב השרטוט המקורי.")
                elif rec.decision.action == "original" and rec.review.status != "approved":
                    reason = rec.decision.reasons[0] if rec.decision.reasons else ""
                    warnings.append(f"שאלה {num}, תרשים {fig_idx}: השחזור לא אושר ({reason}) — ישולב השרטוט המקורי.")

        if UNREADABLE_MARK in question_texts(q):
            errors.append(f"שאלה {num}: קיימים פרטים המסומנים {UNREADABLE_MARK} — יש לתקן לפני הפקה.")

        if not q.teacher_verified:
            if q.verification_error:
                errors.append(f"שאלה {num}: {q.verification_error} — נדרש אימות ידני לפני הפקה.")
            elif q.verification is None:
                errors.append(f"שאלה {num}: לא בוצעה בדיקה עצמאית — נדרש אימות ידני לפני הפקה.")
            else:
                expected_ids = [clean_section_id(s.section_id) for s in q.sections] if q.sections else [""]
                returned_ids = [clean_section_id(i.section_id) for i in q.verification.items]
                missing = [sid for sid in expected_ids if sid not in returned_ids]
                duplicates = sorted({sid for sid in returned_ids if returned_ids.count(sid) > 1})
                extras = [sid for sid in returned_ids if sid not in expected_ids]
                if missing or duplicates or extras or len(returned_ids) != len(expected_ids):
                    details = []
                    if missing:
                        details.append("חסרים: " + ", ".join(s or "כללי" for s in missing))
                    if duplicates:
                        details.append("כפולים: " + ", ".join(s or "כללי" for s in duplicates))
                    if extras:
                        details.append("לא צפויים: " + ", ".join(s or "כללי" for s in extras))
                    errors.append(f"שאלה {num}: הבדיקה העצמאית אינה מכסה בדיוק את כל הסעיפים ({'; '.join(details)}).")

                bad = [i for i in q.verification.items if not (i.agrees and i.reasoning_agrees and i.source_text_agrees)]
                if (
                    bad
                    or not q.verification.overall_agrees
                    or not q.verification.reasoning_agrees
                    or not q.verification.source_reconstruction_agrees
                ):
                    ids_txt = ", ".join(i.section_id or "כללי" for i in bad) or "כללי"
                    errors.append(
                        f"שאלה {num}: הבדיקה העצמאית מצאה אי-התאמה במקור, בדרך הפתרון או בתשובה "
                        f"(סעיפים: {ids_txt}). בדוק וסמן 'אימתתי ידנית'."
                    )

    errors.extend(validate_points_structure(meta, {q.question_number: q.points for q in exam.questions}))
    return errors, warnings


def rebalance_question(q: QuestionAnalysis) -> None:
    """Scale section points to the question total and rubric percentages to 100 (exact after rounding)."""
    total = normalize_decimal(q.points)
    if q.sections:
        raw = [normalize_decimal(s.points) for s in q.sections]
        s = sum(raw, Decimal("0"))
        if s > 0:
            new = [(r * total / s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for r in raw]
            new[-1] += total - sum(new, Decimal("0"))
            for sec, v in zip(q.sections, new):
                sec.points = float(v)
    if q.rubric_steps:
        raw = [normalize_decimal(r.percentage) for r in q.rubric_steps]
        s = sum(raw, Decimal("0"))
        if s > 0:
            new = [(r * Decimal("100") / s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for r in raw]
            new[-1] += Decimal("100") - sum(new, Decimal("0"))
            for rub, v in zip(q.rubric_steps, new):
                rub.percentage = float(v)


def allocate_rubric_points(total_points: float, percentages: list[float]) -> list[Decimal]:
    total = normalize_decimal(total_points)
    if not percentages:
        return []
    values = [
        (total * Decimal(str(p)) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for p in percentages
    ]
    pct_sum = sum((normalize_decimal(p) for p in percentages), Decimal("0"))
    if abs(pct_sum - Decimal("100")) <= POINT_TOLERANCE:
        values[-1] += total - sum(values, Decimal("0"))
    return values


def build_default_instructions(duration: int, groups: list[dict[str, Any]]) -> str:
    lines = [f"• משך הבחינה: {duration} דקות."]
    if len(groups) == 1:
        g = groups[0]
        count = len(g["questions"])
        required = int(g["required"])
        if required == count:
            lines.append(f"• מבנה המבחן: יש לענות על כל {count} השאלות.")
        else:
            lines.append(f"• מבנה המבחן: יש לענות על {required} מתוך {count} השאלות.")
    else:
        lines.append("• מבנה המבחן:")
        for idx, g in enumerate(groups, 1):
            q_text = ", ".join(str(x) for x in g["questions"])
            required = int(g["required"])
            if required == len(g["questions"]):
                lines.append(f"  ◦ קבוצה {idx} — שאלות {q_text}: יש לענות על כולן.")
            else:
                lines.append(f"  ◦ קבוצה {idx} — שאלות {q_text}: יש לענות על {required} שאלות.")
    lines += [
        "• חומר עזר מותר: מחשבון ודף נוסחאות בהתאם להנחיות המורה.",
        "• חובה להציג את דרך הפתרון ואת שלבי החישוב. תשובה ללא דרך לא תזכה במלוא הניקוד.",
        "• הקפידו על כתיבה ברורה ומסודרת, והעתיקו שרטוטים רלוונטיים למחברת כאשר נדרש.",
    ]
    return "\n".join(lines)


# ============================================================
# 4. Image processing
# ============================================================
def image_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _to_rgb(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return img.convert("RGB")


def pil_to_png_bytes(img: Image.Image, max_side: int = MAX_IMAGE_SIDE) -> bytes:
    prepared = _to_rgb(img.copy())
    if max(prepared.size) > max_side:
        prepared.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    prepared.save(out, format="PNG", compress_level=6)
    return out.getvalue()


def image_to_png_bytes(source: Any) -> bytes:
    """Normalize UploadedFile / camera / PIL / bytes into orientation-correct RGB PNG bytes."""
    if isinstance(source, Image.Image):
        return pil_to_png_bytes(source)
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    elif hasattr(source, "getvalue"):
        raw = source.getvalue()
    elif hasattr(source, "read"):
        raw = source.read()
    else:
        raise TypeError(f"Unsupported image source: {type(source)!r}")
    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        return pil_to_png_bytes(im)


def png_bytes_to_pil(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        return _to_rgb(im.copy())


def fit_for_editor(img: Image.Image, max_width: int = 760, max_height: int = 1000) -> tuple[Image.Image, float]:
    """Return a display copy and the scale factor full/display (>= 1)."""
    copy = img.copy()
    copy.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    scale = img.width / copy.width if copy.width else 1.0
    return copy, scale


def crop_full_resolution(full_bytes: bytes, box: dict[str, Any], scale: float) -> bytes:
    """Apply a crop box expressed in display coordinates to the full-resolution image."""
    img = png_bytes_to_pil(full_bytes)
    left = safe_float(box.get("left")) * scale
    top = safe_float(box.get("top")) * scale
    width = safe_float(box.get("width")) * scale
    height = safe_float(box.get("height")) * scale
    l = max(0, min(img.width - 1, round(left)))
    t = max(0, min(img.height - 1, round(top)))
    r = max(l + 1, min(img.width, round(left + width)))
    b = max(t + 1, min(img.height, round(top + height)))
    if r - l < 10 or b - t < 10:
        raise ValueError("אזור החיתוך קטן מדי.")
    return pil_to_png_bytes(img.crop((l, t, r, b)))


def apply_erase_mask(full_bytes: bytes, canvas_rgba: Any) -> tuple[bytes, float]:
    """Whiten on the full-resolution image every pixel painted on the canvas layer.

    The drawable-canvas component returns ONLY the drawing layer (RGBA, transparent where nothing
    was drawn) at display size. We scale that mask up to the full image, so no resolution is lost.
    Returns (png_bytes, fraction_of_image_erased).
    """
    arr = np.asarray(canvas_rgba)
    if arr.ndim != 3 or arr.shape[2] < 4:
        raise ValueError("פלט הקנבס אינו בפורמט RGBA.")
    alpha = arr[:, :, 3].astype(np.uint8)
    if not alpha.any():
        raise ValueError("לא סומן אזור למחיקה.")
    fraction = float((alpha > 0).mean())
    if fraction > 0.97:
        raise ValueError("כמעט כל התמונה סומנה למחיקה — הפעולה בוטלה ליתר ביטחון.")
    img = png_bytes_to_pil(full_bytes)
    mask = Image.fromarray(np.where(alpha > 0, 255, 0).astype(np.uint8), "L")
    mask = mask.resize(img.size, Image.Resampling.BILINEAR).point(lambda v: 255 if v >= 96 else 0)
    img.paste((255, 255, 255), mask=mask)
    return pil_to_png_bytes(img), fraction


def rotate_image_bytes(data: bytes, degrees: float) -> bytes:
    """Positive = counter-clockwise (PIL convention). The canvas grows so no corner is cut."""
    img = png_bytes_to_pil(data)
    if abs(degrees) % 90 == 0:
        method = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}
        k = int(round(degrees)) % 360
        return pil_to_png_bytes(img.transpose(method[k]) if k else img)
    rotated = img.rotate(float(degrees), resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(255, 255, 255))
    return pil_to_png_bytes(rotated)


def _otsu_threshold(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = gray.size
    sum_total = np.dot(np.arange(256), hist)
    w_b = sum_b = 0.0
    best_t, best_var = 127, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def estimate_skew_angle(img: Image.Image, max_angle: float = 15.0) -> float:
    """Projection-profile deskew. Returns the angle (degrees, PIL convention) that straightens the text lines."""
    gray = ImageOps.grayscale(img)
    gray.thumbnail((1000, 1000))
    arr = np.asarray(gray, dtype=np.uint8)
    # Remove slow illumination changes (phone photos / shadows) before thresholding.
    blurred = np.asarray(gray.resize((max(1, gray.width // 16), max(1, gray.height // 16))).resize(gray.size, Image.Resampling.BILINEAR), dtype=np.int16)
    flat = np.clip(arr.astype(np.int16) - blurred + 200, 0, 255).astype(np.uint8)
    t = _otsu_threshold(flat)
    ink = (flat < min(t, 185)).astype(np.uint8) * 255
    if ink.mean() < 0.3:  # almost empty page
        return 0.0
    ink_img = Image.fromarray(ink, "L")

    def score(angle: float) -> float:
        rotated = np.asarray(ink_img.rotate(angle, resample=Image.Resampling.NEAREST, expand=False), dtype=np.float64)
        profile = rotated.sum(axis=1)
        return float(np.sum(np.diff(profile) ** 2))

    coarse = np.arange(-max_angle, max_angle + 0.001, 1.0)
    best = max(coarse, key=score)
    fine = np.arange(best - 1.0, best + 1.0001, 0.1)
    best = max(fine, key=score)
    return float(round(best, 2))


def auto_straighten_bytes(data: bytes) -> tuple[bytes, float]:
    img = png_bytes_to_pil(data)
    angle = estimate_skew_angle(img)
    if abs(angle) < 0.15:
        return data, 0.0
    return rotate_image_bytes(data, angle), angle


def crop_figure_bytes(source: bytes, bbox: list[int] | None) -> bytes:
    image = png_bytes_to_pil(source)
    if bbox and bbox_is_valid(bbox):
        y1, x1, y2, x2 = bbox
        w, h = image.size
        left = max(0, min(w - 1, round(x1 / 1000 * w)))
        right = max(left + 1, min(w, round(x2 / 1000 * w)))
        top = max(0, min(h - 1, round(y1 / 1000 * h)))
        bottom = max(top + 1, min(h, round(y2 / 1000 * h)))
        image = image.crop((left, top, right, bottom))
    return pil_to_png_bytes(image)


# ============================================================
# 5. Safe math-expression parser (lives in diagram_engine.safe_math; re-exported for compatibility)
# ============================================================
latex_to_plain = _sm.latex_to_plain
safe_function = _sm.safe_function


# ============================================================
# 6. Figures: text direction helpers + deterministic diagram engine integration
# ============================================================
def has_rtl(text: str) -> bool:
    return bool(RTL_CHARS.search(text or ""))


def visual_text(text: Any) -> str:
    """Convert logical-order Hebrew/Arabic to visual order for renderers without bidi support (matplotlib)."""
    s = str(text or "")
    if not has_rtl(s):
        return s
    if ARABIC_CHARS.search(s) and arabic_reshaper is not None:
        s = arabic_reshaper.reshape(s)
    if _bidi_get_display is None:
        return s
    from diagram_engine.text_utils import mirror_brackets   # python-bidi does not mirror brackets (fixed in 5.5)

    return "\n".join(_bidi_get_display(mirror_brackets(line)) for line in s.split("\n"))


def question_text_for_diagrams(q: QuestionAnalysis) -> str:
    return "\n".join([q.text, *(f"{s.section_id}. {s.text}" for s in q.sections)])


def figure_source_crop(fig: FigureRef, images: list[bytes]) -> bytes | None:
    idx = fig.source_image_index - 1
    if 0 <= idx < len(images):
        try:
            return crop_figure_bytes(images[idx], fig.bbox)
        except Exception:
            return None
    return None


def _diagram_input_key(fig: FigureRef, images: list[bytes]) -> str:
    idx = fig.source_image_index - 1
    img = images[idx] if 0 <= idx < len(images) else b""
    raw = f"{diagram_engine.PARSER_VERSION}|{idx}|{fig.bbox}|{fig.figure_type}|{fig.rebuild_confidence}|{image_digest(img)}|{fig.spec_json}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def attach_diagrams(q: QuestionAnalysis, images: list[bytes], ai_model: str = "", allow_auto: bool = False) -> None:
    # allow_auto: kept for API compatibility with 5.4 and ignored - nothing is ever approved without a teacher.
    """Run the deterministic diagram engine for every figure whose input changed.
    An unchanged figure keeps its record (including teacher edits and approval)."""
    text = question_text_for_diagrams(q)
    new: dict[str, DiagramRecord] = {}
    for fig in q.figures:
        prior = q.diagrams.get(fig.figure_id)
        key = _diagram_input_key(fig, images)
        if prior is not None and prior.audit.get("input_key") == key and prior.audit.get("question_text") == text:
            new[fig.figure_id] = prior
            continue
        rec = diagram_engine.process(
            fig.figure_id, fig.spec_json, text, figure_source_crop(fig, images), figure_type_hint=fig.figure_type,
            fallback_confidence=fig.rebuild_confidence, ai_model=ai_model,
            prior_review=prior.review if prior else None, allow_auto=allow_auto)
        rec.audit["input_key"], rec.audit["question_text"] = key, text
        new[fig.figure_id] = rec
    q.diagrams = new


def figure_bytes_for_document(fig: FigureRef, source_images: list[bytes], record: DiagramRecord | None = None
                              ) -> tuple[bytes | None, str | None, bool]:
    """Returns (png, warning, is_rebuilt). The reconstruction is used ONLY if it is approved for this exact spec."""
    if diagram_engine.usable_in_document(record):
        try:
            _, png, _ = diagram_engine.render_spec(record.spec)
            return png, None, True
        except Exception as exc:  # never break the document because of one figure
            crop = figure_source_crop(fig, source_images)
            return crop, f"שחזור השרטוט נכשל ({type(exc).__name__}) — שולב המקור", False
    crop = figure_source_crop(fig, source_images)
    return crop, (None if crop is not None else "תמונת המקור לא נמצאה"), False


# ============================================================
# 7. Word generation (schema-ordered OOXML, correct RTL)
# ============================================================
RPR_ORDER = [
    "w:rStyle", "w:rFonts", "w:b", "w:bCs", "w:i", "w:iCs", "w:caps", "w:smallCaps", "w:strike", "w:dstrike",
    "w:outline", "w:shadow", "w:emboss", "w:imprint", "w:noProof", "w:snapToGrid", "w:vanish", "w:webHidden",
    "w:color", "w:spacing", "w:w", "w:kern", "w:position", "w:sz", "w:szCs", "w:highlight", "w:u", "w:effect",
    "w:bdr", "w:shd", "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang", "w:eastAsianLayout",
    "w:specVanish", "w:oMath",
]
PPR_ORDER = [
    "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr", "w:widowControl", "w:numPr",
    "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
    "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi", "w:adjustRightInd",
    "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
    "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr",
    "w:sectPr", "w:pPrChange",
]
TBLPR_ORDER = [
    "w:tblStyle", "w:tblpPr", "w:tblOverlap", "w:bidiVisual", "w:tblStyleRowBandSize", "w:tblStyleColBandSize",
    "w:tblW", "w:jc", "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd", "w:tblLayout", "w:tblCellMar",
    "w:tblLook", "w:tblCaption", "w:tblDescription",
]
TCPR_ORDER = [
    "w:cnfStyle", "w:tcW", "w:gridSpan", "w:hMerge", "w:vMerge", "w:tcBorders", "w:shd", "w:noWrap", "w:tcMar",
    "w:textDirection", "w:tcFitText", "w:vAlign", "w:hideMark",
]
SECTPR_ORDER = [
    "w:headerReference", "w:footerReference", "w:footnotePr", "w:endnotePr", "w:type", "w:pgSz", "w:pgMar",
    "w:paperSrc", "w:pgBorders", "w:lnNumType", "w:pgNumType", "w:cols", "w:formProt", "w:vAlign", "w:noEndnote",
    "w:titlePg", "w:textDirection", "w:bidi", "w:rtlGutter", "w:docGrid", "w:printerSettings", "w:sectPrChange",
]


def set_ordered(parent, tag: str, order: list[str], attrs: dict[str, str] | None = None):
    """Get-or-create child `tag` at its schema-correct position inside `parent`."""
    el = parent.find(qn(tag))
    if el is None:
        el = OxmlElement(tag)
        successors = {qn(t) for t in order[order.index(tag) + 1:]}
        for child in parent:
            if child.tag in successors:
                child.addprevious(el)
                break
        else:
            parent.append(el)
    for key, value in (attrs or {}).items():
        el.set(qn(key), value)
    return el


def remove_child(parent, tag: str) -> None:
    el = parent.find(qn(tag))
    if el is not None:
        parent.remove(el)


class DocContext:
    def __init__(self, language: str):
        self.language = language
        self.rtl = language in RTL_LANGUAGES
        self.bidi_lang = BIDI_LANG.get(language, "he-IL")
        self.warnings: list[str] = []


def style_run(run, ctx: DocContext, bold: bool = False, size: float = 11, color: RGBColor | None = None) -> None:
    r_pr = run._r.get_or_add_rPr()
    set_ordered(r_pr, "w:rFonts", RPR_ORDER, {"w:ascii": BODY_FONT, "w:hAnsi": BODY_FONT, "w:eastAsia": BODY_FONT, "w:cs": BODY_FONT})
    for tag in ("w:b", "w:bCs"):
        if bold:
            set_ordered(r_pr, tag, RPR_ORDER)
        else:
            remove_child(r_pr, tag)
    if color is not None:
        run.font.color.rgb = color
    half_points = str(int(round(size * 2)))
    set_ordered(r_pr, "w:sz", RPR_ORDER, {"w:val": half_points})
    set_ordered(r_pr, "w:szCs", RPR_ORDER, {"w:val": half_points})  # Hebrew/Arabic size comes from szCs
    if has_rtl(run.text):
        set_ordered(r_pr, "w:rtl", RPR_ORDER)
    set_ordered(r_pr, "w:lang", RPR_ORDER, {"w:val": "en-US", "w:bidi": ctx.bidi_lang})


def set_paragraph(paragraph, ctx: DocContext, align: str = "start", rtl: bool | None = None,
                  space_after: float = 4, keep_next: bool = False) -> None:
    """align: start | center | end. In RTL paragraphs 'start' = right; we omit jc so Word and LibreOffice agree."""
    rtl = ctx.rtl if rtl is None else rtl
    p_pr = paragraph._p.get_or_add_pPr()
    if rtl:
        remove_child(p_pr, "w:bidi")  # drop any w:val="0" left from an earlier call
        set_ordered(p_pr, "w:bidi", PPR_ORDER)
    elif ctx.rtl:
        # The Normal style is RTL in Hebrew/Arabic documents, so an LTR paragraph must say so explicitly.
        set_ordered(p_pr, "w:bidi", PPR_ORDER, {"w:val": "0"})
    else:
        remove_child(p_pr, "w:bidi")
    if keep_next:
        set_ordered(p_pr, "w:keepNext", PPR_ORDER)
    set_ordered(p_pr, "w:spacing", PPR_ORDER, {"w:after": str(int(space_after * 20)), "w:line": "276", "w:lineRule": "auto"})
    remove_child(p_pr, "w:jc")
    if align == "center":
        set_ordered(p_pr, "w:jc", PPR_ORDER, {"w:val": "center"})
    elif align == "end":
        # For bidi paragraphs Word treats left/right as start/end.
        set_ordered(p_pr, "w:jc", PPR_ORDER, {"w:val": "left" if rtl else "right"})
    elif not rtl:
        set_ordered(p_pr, "w:jc", PPR_ORDER, {"w:val": "left"})


def add_text(paragraph, text: str, ctx: DocContext, bold: bool = False, size: float = 11, color: RGBColor | None = None) -> None:
    if not text:
        return
    for idx, line in enumerate(text.split("\n")):
        if idx:
            paragraph.add_run().add_break()
        if line:
            run = paragraph.add_run(line)
            style_run(run, ctx, bold=bold, size=size, color=color)


MATH_PATTERN = re.compile(r"(\$\$.+?\$\$|\$(?!\$)(?:\\\$|[^$])+?\$|\\\[.+?\\\]|\\\(.+?\\\))", re.DOTALL)


def unwrap_math(token: str) -> tuple[str, bool]:
    if token.startswith("$$"):
        return token[2:-2], True
    if token.startswith("\\["):
        return token[2:-2], True
    if token.startswith("\\("):
        return token[2:-2], False
    return token[1:-1], False


_LATEX_PREFIX_FIXES = [  # constructs mathml2omml converts incorrectly -> equivalent ones it handles
    (re.compile(r"\\(?:vec|overrightarrow)\s*\{"), r"\\overset{\\rightarrow}{"),
    (re.compile(r"\\vec\s*([A-Za-z])"), r"\\overset{\\rightarrow}{\1}"),
    (re.compile(r"\\bar\s*\{"), r"\\overline{"),
    (re.compile(r"\\[td]frac"), r"\\frac"),
]


def _repair_omml(root) -> None:
    """mathml2omml emits <m:rad> without the mandatory <m:deg>; Word's schema requires radPr?, deg, e."""
    m = "http://schemas.openxmlformats.org/officeDocument/2006/math"
    # <m:box> wrappers (from MathML <mrow>) make LibreOffice drop the whole equation. They carry no
    # meaning here, so unwrap them: move the children of box/e into the box's place.
    for box in list(root.iter(f"{{{m}}}box")):
        parent = box.getparent()
        if parent is None:
            continue
        inner = box.find(f"{{{m}}}e")
        pos = parent.index(box)
        for child in list(inner) if inner is not None else []:
            parent.insert(pos, child)
            pos += 1
        parent.remove(box)
    for rad in root.iter(f"{{{m}}}rad"):
        if rad.find(f"{{{m}}}deg") is None:
            rad_pr = rad.find(f"{{{m}}}radPr")
            if rad_pr is None:
                rad_pr = OxmlElement("m:radPr")
                rad.insert(0, rad_pr)
            if rad_pr.find(f"{{{m}}}degHide") is None:
                hide = OxmlElement("m:degHide")
                hide.set(qn("m:val"), "1")
                rad_pr.append(hide)
            rad_pr.addnext(OxmlElement("m:deg"))


def latex_to_omml(latex: str):
    latex = latex.strip()
    for pattern, repl in _LATEX_PREFIX_FIXES:
        latex = pattern.sub(repl, latex)
    mathml = latex2mathml.converter.convert(latex)
    omml = mathml2omml.convert(mathml, html.entities.name2codepoint)
    if "xmlns:m=" not in omml:
        omml = omml.replace("<m:oMath>", f"<m:oMath {nsdecls('m')}>", 1)
    root = parse_xml(omml)
    _repair_omml(root)
    return root


def add_mixed(paragraph, text: str, ctx: DocContext, context: str, bold: bool = False, size: float = 11) -> None:
    """Plain text + $LaTeX$ → runs + native Word equations (OMML)."""
    text = text or ""
    stripped = text.strip()
    if stripped.startswith("$$") and stripped.endswith("$$") and stripped.count("$$") == 2 and not paragraph.runs:
        p_pr = paragraph._p.get_or_add_pPr()
        remove_child(p_pr, "w:jc")
        set_ordered(p_pr, "w:jc", PPR_ORDER, {"w:val": "center"})
        text = "$" + stripped[2:-2] + "$"
    cursor = 0
    for match in MATH_PATTERN.finditer(text):
        add_text(paragraph, text[cursor:match.start()], ctx, bold=bold, size=size)
        latex, display = unwrap_math(match.group(0))
        if display and paragraph.runs:
            paragraph.add_run().add_break()
        try:
            paragraph._p.append(latex_to_omml(latex))
        except Exception as exc:
            run = paragraph.add_run(f"[{latex}]")
            style_run(run, ctx, size=size)
            ctx.warnings.append(f"{context}: הנוסחה '{latex}' לא הומרה למשוואת Word ({exc}); נשמרה כטקסט.")
        if display and match.end() < len(text.rstrip()):
            paragraph.add_run().add_break()
        cursor = match.end()
    add_text(paragraph, text[cursor:], ctx, bold=bold, size=size)


def new_paragraph(container, ctx: DocContext, **kwargs):
    p = container.add_paragraph()
    set_paragraph(p, ctx, **kwargs)
    return p


def set_table_layout(table, ctx: DocContext, widths_in: list[float], borders: bool) -> None:
    tbl_pr = table._tbl.tblPr
    if ctx.rtl:
        set_ordered(tbl_pr, "w:bidiVisual", TBLPR_ORDER)
    total = sum(widths_in)
    set_ordered(tbl_pr, "w:tblW", TBLPR_ORDER, {"w:w": str(int(total * 1440)), "w:type": "dxa"})
    set_ordered(tbl_pr, "w:jc", TBLPR_ORDER, {"w:val": "center"})
    set_ordered(tbl_pr, "w:tblLayout", TBLPR_ORDER, {"w:type": "fixed"})
    if borders:
        b = set_ordered(tbl_pr, "w:tblBorders", TBLPR_ORDER)
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            el = b.find(qn(f"w:{edge}"))
            if el is None:
                el = OxmlElement(f"w:{edge}")
                b.append(el)
            el.set(qn("w:val"), "single")
            el.set(qn("w:sz"), "6")
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), "8C8C8C")
    for i, col in enumerate(table.columns):
        col.width = Inches(widths_in[i])
    for row in table.rows:
        for i, cell in enumerate(row.cells):
            tc_pr = cell._tc.get_or_add_tcPr()
            set_ordered(tc_pr, "w:tcW", TCPR_ORDER, {"w:w": str(int(widths_in[i] * 1440)), "w:type": "dxa"})


def shade_cell(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    set_ordered(tc_pr, "w:shd", TCPR_ORDER, {"w:val": "clear", "w:color": "auto", "w:fill": fill})


def fill_cell(cell, text: str, ctx: DocContext, context: str, bold: bool = False, size: float = 9.5, align: str = "start") -> None:
    cell.text = ""
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    lines = (text or "").split("\n")
    for idx, line in enumerate(lines):
        p = cell.paragraphs[0] if idx == 0 else cell.add_paragraph()
        set_paragraph(p, ctx, align=align, space_after=2)
        add_mixed(p, line, ctx, context, bold=bold, size=size)


def add_field(paragraph, instr: str, ctx: DocContext, size: float = 9) -> None:
    def fld(kind: str):
        run = paragraph.add_run()
        el = OxmlElement("w:fldChar")
        el.set(qn("w:fldCharType"), kind)
        run._r.append(el)
        return run

    fld("begin")
    r = paragraph.add_run()
    instr_el = OxmlElement("w:instrText")
    instr_el.set(qn("xml:space"), "preserve")
    instr_el.text = f" {instr} "
    r._r.append(instr_el)
    fld("separate")
    placeholder = paragraph.add_run("1")
    style_run(placeholder, ctx, size=size)
    fld("end")


def setup_document(ctx: DocContext) -> docx.Document:
    doc = docx.Document()
    for section in doc.sections:
        section.page_width = Inches(8.27)   # A4
        section.page_height = Inches(11.69)
        for side in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
            setattr(section, side, Inches(0.7))
        section.footer_distance = Inches(0.35)
        if ctx.rtl:
            set_ordered(section._sectPr, "w:bidi", SECTPR_ORDER)
    zoom = doc.settings.element.find(qn("w:zoom"))
    if zoom is not None and zoom.get(qn("w:percent")) is None:
        zoom.set(qn("w:percent"), "100")  # python-docx template omits this required attribute
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(11)
    r_pr = normal.element.get_or_add_rPr()
    set_ordered(r_pr, "w:rFonts", RPR_ORDER, {"w:ascii": BODY_FONT, "w:hAnsi": BODY_FONT, "w:eastAsia": BODY_FONT, "w:cs": BODY_FONT})
    set_ordered(r_pr, "w:szCs", RPR_ORDER, {"w:val": "22"})
    set_ordered(r_pr, "w:lang", RPR_ORDER, {"w:val": "en-US", "w:bidi": ctx.bidi_lang})
    if ctx.rtl:
        p_pr = normal.element.get_or_add_pPr()
        set_ordered(p_pr, "w:bidi", PPR_ORDER)
    return doc


def add_footer(doc: docx.Document, ctx: DocContext, labels: DocumentLabels, title: str) -> None:
    footer = doc.sections[0].footer
    p = footer.paragraphs[0]
    set_paragraph(p, ctx, align="center", space_after=0)
    add_text(p, f"{title} | {labels.page} ", ctx, size=9, color=RGBColor(110, 110, 110))
    add_field(p, "PAGE", ctx)
    add_text(p, f" {labels.of} ", ctx, size=9, color=RGBColor(110, 110, 110))
    add_field(p, "NUMPAGES", ctx)


def format_exam_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).strftime("%d/%m/%Y")
    except Exception:
        return str(value or "")


def add_header_block(doc, meta: dict[str, Any], exam: ExamAnalysis, ctx: DocContext, title: str) -> None:
    labels = exam.labels
    logo = meta.get("logo_bytes")
    widths = [5.27, 1.6] if logo else [6.87]
    table = doc.add_table(rows=1, cols=len(widths))
    set_table_layout(table, ctx, widths, borders=False)
    info = table.rows[0].cells[0]
    info.text = ""
    p = info.paragraphs[0]
    set_paragraph(p, ctx, space_after=2)
    school = str(meta.get("school_name", "")).strip()
    add_text(p, school, ctx, bold=True, size=15)
    p2 = info.add_paragraph()
    set_paragraph(p2, ctx, space_after=2)
    add_text(p2, exam.translated_exam_name, ctx, bold=True, size=13)
    details = [
        f"{labels.grade}: {exam.translated_grade}",
        f"{labels.level}: {exam.translated_level}",
        f"{labels.exam_date}: {format_exam_date(meta.get('date', ''))}",
    ]
    p3 = info.add_paragraph()
    set_paragraph(p3, ctx, space_after=2)
    add_text(p3, "   |   ".join(details), ctx, size=10)
    p4 = info.add_paragraph()
    set_paragraph(p4, ctx, space_after=2)
    teacher = str(meta.get("teacher_name", "")).strip()
    line4 = f"{labels.duration}: {meta.get('duration', '')} {labels.minutes}"
    if teacher:
        line4 += f"   |   {labels.teacher}: {teacher}"
    add_text(p4, line4, ctx, size=10)
    if logo:
        cell = table.rows[0].cells[1]
        cp = cell.paragraphs[0]
        set_paragraph(cp, ctx, align="center", space_after=0)
        try:
            cp.add_run().add_picture(io.BytesIO(logo), width=Inches(1.3))
        except Exception as exc:
            ctx.warnings.append(f"לא ניתן היה להוסיף את הלוגו: {exc}")
    # Title with a bottom rule (paragraph border, not a row of dashes).
    pt = new_paragraph(doc, ctx, align="center", space_after=8)
    p_pr = pt._p.get_or_add_pPr()
    bdr = set_ordered(p_pr, "w:pBdr", PPR_ORDER)
    bottom = OxmlElement("w:bottom")
    for k, v in {"w:val": "single", "w:sz": "8", "w:space": "4", "w:color": "1E88E5"}.items():
        bottom.set(qn(k), v)
    bdr.append(bottom)
    add_text(pt, title, ctx, bold=True, size=17, color=RGBColor(21, 101, 192))


def add_figures(doc, q: QuestionAnalysis, source: dict[str, Any], ctx: DocContext, labels: DocumentLabels,
                with_captions: bool) -> None:
    images = source.get("images", [])
    for idx, fig in enumerate(q.figures, 1):
        rec = q.diagrams.get(fig.figure_id)
        if diagram_engine.usable_in_document(rec) and rec.spec.diagram_type == "table":
            try:
                add_native_table(doc, rec.spec.table, ctx)
                if with_captions:
                    cap = new_paragraph(doc, ctx, align="center", space_after=6)
                    add_mixed(cap, f"{labels.figure} {idx} (טבלה משוחזרת ואושרה): {fig.description}", ctx, f"כיתוב איור {idx}", size=9)
                continue
            except Exception as exc:  # fall back to the image path below
                ctx.warnings.append(f"שאלה {q.question_number}, טבלה {idx}: יצירת טבלה נכשלה ({type(exc).__name__}) — שולב המקור.")
                rec = None
        data, warn, rebuilt = figure_bytes_for_document(fig, images, rec)
        if warn:
            ctx.warnings.append(f"שאלה {q.question_number}, תרשים {idx}: {warn}.")
        if data is None:
            continue
        with Image.open(io.BytesIO(data)) as im:
            px_w = im.width
        width = min(4.8, max(1.5, px_w / (430 if rebuilt else 160)))  # rebuilt PNGs are 300 dpi
        p = new_paragraph(doc, ctx, align="center", space_after=2, keep_next=with_captions)
        p.add_run().add_picture(io.BytesIO(data), width=Inches(width))
        if with_captions:
            cap = new_paragraph(doc, ctx, align="center", space_after=6)
            kind = "שוחזר ואושר" if rebuilt else "מקור"
            add_mixed(cap, f"{labels.figure} {idx} ({kind}): {fig.description}", ctx, f"כיתוב איור {idx}", size=9)
            if fig.geogebra_commands:
                geo = new_paragraph(doc, ctx, align="center", space_after=6, rtl=False)
                add_text(geo, "GeoGebra: " + " ; ".join(fig.geogebra_commands), ctx, size=8, color=RGBColor(90, 90, 90))


def add_native_table(doc, table_spec, ctx: DocContext) -> None:
    """Approved TableSpec -> real Word table (editable, sharp), RTL aware, header rows/columns in bold + shaded."""
    rows = table_spec.rows
    n_c = len(rows[0])
    width = min(6.3, max(2.0, 1.3 * n_c))
    table = doc.add_table(rows=len(rows), cols=n_c)
    set_table_layout(table, ctx, [width / n_c] * n_c, borders=True)
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            header = r < table_spec.header_rows or c < table_spec.header_columns
            fill_cell(table.rows[r].cells[c], cell.text, ctx, "טבלה משוחזרת", bold=header, size=9.5, align="center")
            if header:
                shade_cell(table.rows[r].cells[c], "F2F2F2")
            elif cell.fill:
                shade_cell(table.rows[r].cells[c], "808080")
    new_paragraph(doc, ctx, space_after=4)


def question_heading(doc, q: QuestionAnalysis, ctx: DocContext, labels: DocumentLabels, show_topic: bool) -> None:
    p = new_paragraph(doc, ctx, space_after=4, keep_next=True)
    p.paragraph_format.space_before = Pt(10)
    text = f"{labels.question} {q.question_number} ({fmt_points(q.points)} {labels.points})"
    if show_topic and q.topic.strip():
        text += f" — {q.topic.strip()}"
    add_text(p, text, ctx, bold=True, size=13, color=RGBColor(21, 101, 192))


def section_label(sec_id: str, ctx: DocContext) -> str:
    return f"{sec_id}." if sec_id else ""


def create_word_document(exam: ExamAnalysis, meta: dict[str, Any], questions_data: list[dict[str, Any]],
                         doc_type: str, show_topic_in_exam: bool = False) -> tuple[bytes, list[str]]:
    ctx = DocContext(meta.get("language", "עברית"))
    labels = exam.labels
    title = {"exam": labels.exam_form, "solution": labels.solutions, "rubric": labels.rubric}[doc_type]
    doc = setup_document(ctx)
    add_header_block(doc, meta, exam, ctx, title)
    add_footer(doc, ctx, labels, f"{exam.translated_exam_name} — {title}")
    source_by_num = {int(q["question_number"]): q for q in questions_data}

    if doc_type == "exam":
        p = new_paragraph(doc, ctx, space_after=8)
        add_text(p, f"{labels.student_name}: ______________________      {labels.class_name}: ________", ctx, size=11)
        if exam.translated_instructions.strip():
            ph = new_paragraph(doc, ctx, space_after=2, keep_next=True)
            add_text(ph, labels.instructions + ":", ctx, bold=True, size=12)
            for line_no, line in enumerate(exam.translated_instructions.splitlines(), 1):
                if line.strip():
                    pl = new_paragraph(doc, ctx, space_after=1)
                    add_mixed(pl, line, ctx, f"הוראות, שורה {line_no}", size=10.5)

    for q in sorted(exam.questions, key=lambda x: x.question_number):
        source = source_by_num.get(q.question_number, {})
        question_heading(doc, q, ctx, labels, show_topic=(doc_type != "exam" or show_topic_in_exam))

        if doc_type == "exam":
            if q.text.strip():
                for para in q.text.split("\n\n"):
                    # keep the stem on the same page as the drawing that follows it
                    add_mixed(new_paragraph(doc, ctx, keep_next=bool(q.figures)), para, ctx, f"שאלה {q.question_number}, גזע")
            add_figures(doc, q, source, ctx, labels, with_captions=False)
            for sec in q.sections:
                p = new_paragraph(doc, ctx, space_after=6)
                ind = p.paragraph_format
                ind.left_indent = Inches(0.0)
                ind.right_indent = Inches(0.0)
                ind.first_line_indent = None
                add_text(p, f"{section_label(sec.section_id, ctx)} ({fmt_points(sec.points)} {labels.points})  ", ctx, bold=True)
                add_mixed(p, sec.text, ctx, f"שאלה {q.question_number}, סעיף {sec.section_id}")

        elif doc_type == "solution":
            if q.text.strip():
                add_mixed(new_paragraph(doc, ctx, space_after=4), q.text, ctx, f"שאלה {q.question_number}, גזע", size=10)
            add_figures(doc, q, source, ctx, labels, with_captions=True)
            sec_ids = [s.section_id for s in q.sections]
            groups: list[tuple[str, list[SolutionStep]]] = []
            general = [s for s in q.solution_steps if s.section_id not in sec_ids]
            if general:
                groups.append(("", general))
            for sid in sec_ids:
                steps = [s for s in q.solution_steps if s.section_id == sid]
                if steps:
                    groups.append((sid, steps))
            for sid, steps in groups:
                if sid:
                    sec = next(s for s in q.sections if s.section_id == sid)
                    ph = new_paragraph(doc, ctx, space_after=2, keep_next=True)
                    add_text(ph, f"{labels.section} {sid} ({fmt_points(sec.points)} {labels.points})", ctx, bold=True, size=11.5)
                for idx, step in enumerate(steps, 1):
                    p = new_paragraph(doc, ctx, space_after=3)
                    if step.step_title.strip():
                        add_text(p, step.step_title.strip() + ": ", ctx, bold=True)
                    add_mixed(p, step.content, ctx, f"שאלה {q.question_number}, פתרון {sid or ''} שלב {idx}")
                    if step.final_answer.strip():
                        pa = new_paragraph(doc, ctx, space_after=6)
                        p_pr = pa._p.get_or_add_pPr()
                        set_ordered(p_pr, "w:shd", PPR_ORDER, {"w:val": "clear", "w:color": "auto", "w:fill": "E8F5E9"})
                        add_text(pa, labels.final_answer + ": ", ctx, bold=True, color=RGBColor(27, 94, 32))
                        add_mixed(pa, step.final_answer, ctx, f"שאלה {q.question_number}, תשובה סופית", bold=True)

        elif doc_type == "rubric":
            allocated = allocate_rubric_points(q.points, [r.percentage for r in q.rubric_steps])
            widths = [1.35, 0.8, 2.35, 1.2, 1.17]
            table = doc.add_table(rows=1, cols=5)
            headers = [labels.section_stage, labels.points, f"{labels.full_credit} / {labels.partial_credit} / {labels.zero_credit}",
                       labels.carried_error, labels.common_errors]
            for cell, text in zip(table.rows[0].cells, headers):
                fill_cell(cell, text, ctx, "כותרת מחוון", bold=True, size=9.5, align="center")
                shade_cell(cell, "DCE8F5")
            tr_pr = table.rows[0]._tr.get_or_add_trPr()
            tr_pr.append(OxmlElement("w:tblHeader"))
            for idx, rub in enumerate(q.rubric_steps):
                cells = table.add_row().cells
                stage = (f"{labels.section} {rub.section_id}: " if rub.section_id else "") + rub.stage_desc
                pts = f"{fmt_points(allocated[idx])}\n({rub.percentage:g}%)"
                criteria = (f"{labels.full_credit}: {rub.full_credit}\n{labels.partial_credit}: {rub.partial_credit}\n"
                            f"{labels.zero_credit}: {rub.zero_credit}")
                common = "\n".join(f"• {e.error} ({e.severity}; −{e.deduction_percent:g}%)" for e in rub.common_errors) or "—"
                values = [stage, pts, criteria, rub.carried_over_error_policy or "—", common]
                for col, value in enumerate(values):
                    fill_cell(cells[col], value, ctx, f"מחוון שאלה {q.question_number}, שלב {idx + 1}", size=9,
                              align="center" if col == 1 else "start")
            total_cells = table.add_row().cells
            fill_cell(total_cells[0], labels.total, ctx, "סיכום", bold=True, size=9.5)
            fill_cell(total_cells[1], fmt_points(sum(allocated, Decimal("0"))), ctx, "סיכום", bold=True, size=9.5, align="center")
            for c in total_cells:
                shade_cell(c, "F2F2F2")
            set_table_layout(table, ctx, widths, borders=True)
            new_paragraph(doc, ctx, space_after=4)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue(), ctx.warnings


# ============================================================
# 8. PDF conversion through LibreOffice
# ============================================================
_PDF_LOCK = threading.Lock()


def soffice_executable() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def docx_to_pdf_bytes(docx_bytes: bytes, stem: str = "document", timeout: int = 180) -> tuple[bytes | None, str]:
    exe = soffice_executable()
    if not exe:
        return None, "LibreOffice אינו מותקן בסביבה."
    with _PDF_LOCK, tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / f"{sanitize_filename(stem)}.docx"
        src.write_bytes(docx_bytes)
        profile = tmp_path / "lo_profile"  # private profile: no lock clashes between users
        env = os.environ.copy()
        env.setdefault("SAL_USE_VCLPLUGIN", "svp")
        env["HOME"] = str(tmp_path)
        cmd = [exe, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore", "--nolockcheck",
               "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(tmp_path), str(src)]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env, check=False)
        except subprocess.TimeoutExpired:
            return None, "ההמרה ל-PDF חרגה מזמן ההמתנה."
        pdf = src.with_suffix(".pdf")
        if proc.returncode != 0 or not pdf.exists():
            return None, f"ההמרה ל-PDF נכשלה: {proc.stderr.decode(errors='ignore')[-400:]}"
        return pdf.read_bytes(), ""


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name or "").strip().strip(".")
    return cleaned[:80] or "document"
