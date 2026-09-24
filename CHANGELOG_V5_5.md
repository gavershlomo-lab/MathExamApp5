# CHANGELOG — 5.5.0

## Architecture
The diagram engine became a package of families with a strict separation of PARSING / VALIDATION / SOLVING / RENDERING /
COMPARISON / DECISION / UI / EXPORT. Pipeline: source → preprocess → structured evidence (vision JSON + question text) →
DiagramSpec (schema) → text/marks merge → constraint solving → deterministic validation → deterministic renderer →
source comparison → decision → teacher review → approved export. One public entry point: `diagram_engine.process_diagram(...)`
returning `DiagramResult` (`process(...)` kept for 5.4 callers).

## Files added
- `diagram_engine/constants.py` — versions (schema 2.0, parser diagram-engine/2.0), policy thresholds, MAX_DIAGRAM_WORKERS, source ranking.
- `diagram_engine/evidence.py` — EvidenceItem, reliability hierarchy, `may_create_relation` (not-to-scale rule).
- `diagram_engine/cache.py` — thread-safe LRU caches keyed by SHA-256(+parser version).
- `diagram_engine/audit.py`, `diagram_engine/facts.py` (Hebrew fact list for the teacher panel).
- `diagram_engine/graph/` — `formula_parser`, `feature_detector` (domain intervals, roots, extrema, monotonicity, vertical/horizontal/
  oblique asymptotes, holes, symmetry, branch count), `evaluator`, `qualitative_parser` (topology + Fritsch–Carlson monotone curve,
  hyperbolic tails), `renderer` (formula / qualitative / multiple choice I–IV), `comparator` (+ deterministic option matching).
- `diagram_engine/geometry/` — `parser` (Hebrew/LaTeX relations, exact coordinates, diameter, tangent, intersection, point order),
  `constraints` (37 constraint types), `solver` (hidden circle centres, fixed points, curve incidence), `incidence`, `renderer`.
- `diagram_engine/mixed/` — graph + construction in one coordinate system.
- `diagram_engine/charts/` — `scatter` (numpy-only grid/axis detection, pixel→value mapping, dot centres), `normal_distribution`,
  `table`, `parser`, `renderer`.
- `diagram_engine/generic/` — plans/schematics with dimension lines attached to endpoints, adjacency.
- `diagram_engine/spatial/` — `voxel` (counts, top view, painter-buffer visibility → hidden columns), `solids`, `projection`, `renderer`, `comparator`.
- `tests/diagram_engine/` — 9 unit/integration files + `acceptance/` (cases from the six supplied questionnaires, 37 fixture crops).
- `CHANGELOG_V5_5.md`, `AUDIT_V5_5.md`, `TEST_REPORT_V5_5.md`, `ACCEPTANCE_TEST_REPORT.md`.

## Files modified
- `diagram_engine/schemas.py` (v2: 8 families, subtypes, evidence, source signature fields), `validator.py`, `comparison.py` (v2),
  `decision.py` (v2), `review_state.py`, `pipeline.py`, `classifier.py`, `safe_math.py`, `text_utils.py`, `__init__.py`.
- Old module paths (`graph_parser.py`, `graph_renderer.py`, `geometry_parser.py`, `geometry_renderer.py`, `constraints.py`,
  `chart_parser.py`, `chart_renderer.py`) are now thin compatibility shims.
- `exam_core.py` — version 5.5.0, Gemini HTTP timeout, prompt for all families, native DOCX tables for approved tables,
  cache key includes parser version, bracket mirroring in `visual_text`, auto-approval removed (parameter kept and ignored).
- `app.py` — no auto-approval option; `.katex { unicode-bidi: isolate }`.
- `diagram_ui.py` — review panel v2, 7 buttons, drag editor with constraint snapping.
- `tests/test_diagram_engine.py` — updated to the 5.5 policy.

## Behaviour changes
- Nothing is ever approved automatically. `high_confidence_preview` (≥0.94) still needs the teacher.
- Thresholds: 0.94 / 0.85 / 0.70. Critical mismatch or validation error → original, always.
- Metric relations proposed only by vision are dropped with a warning.
- Qualitative graphs never get an equation.

## Bugs fixed (details in AUDIT_V5_5.md)
Hebrew brackets reversed in rendered figures (all earlier versions); formulas reversed in RTL UI headings; matplotlib rcParams race
between parallel question workers; no HTTP timeout on Gemini calls; false "side flip" when a point is solved onto a line; oblique
asymptote reported for a removable rational function; log asymptotes missed by sampling; label score penalising text-less diagrams;
scatter dots on grid intersections lost; canvas 0.13 removed `transform` mode.

## Dependencies
None added. OpenCV was NOT added: all detection is numpy/PIL (works everywhere; see limitations). PyMuPDF was used only offline
to extract the acceptance fixtures from the PDFs and is not a runtime dependency.

## Backward compatibility
5.4 imports and `process(...)` keep working; 5.4 `spec_json` and 5.x legacy specs are still converted; `allow_auto` parameters
are accepted and ignored.
