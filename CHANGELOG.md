# Changelog

## 5.4 — deterministic diagram engine
### Added
- `diagram_engine/` package: `schemas` (DiagramSpec/GraphSpec/GeometrySpec/ChartSpec/GenericSpec, OCR labels, observed facts,
  validation/comparison/decision/review records), `safe_math`, `graph_parser`, `geometry_parser`, `chart_parser`, `ocr_labels`,
  `classifier`, `preprocess`, `constraints` (numpy Gauss-Newton solver), `validator`, `graph_renderer`, `geometry_renderer`,
  `chart_renderer`, `comparison`, `decision`, `review_state`, `pipeline`.
- Text-over-pixels merging: explicit functions, geometric relations and chart values from the question text override the image.
- Constraint solving for equal lengths, parallel, perpendicular/right angle, midpoint, collinear, on-segment, on-circle,
  tangent and angle values; inconsistent constraints are rejected; side-of-line flips are critical.
- Graph features: roots, y-intercept, extrema, vertical/horizontal asymptotes, removable holes (drawn as open points),
  piecewise functions with open/closed endpoints.
- Anti-hallucination manifest: every renderer reports what it drew; it must equal the spec exactly.
- Structural comparison against the source's observed facts + informative visual similarity score.
- Conservative confidence policy; auto-approval is opt-in (off by default); image-only curves are never auto-approved.
- Teacher review UI (`diagram_ui.py`): source vs reconstruction, approve / edit / use original / reject, table-based editors.
- Approval bound to the spec hash; any edit voids approval; audit trail of edits.
- Diagram audit bundle (ZIP) in step 5.
- 77 diagram-engine tests + 3 integration tests + 3 review-UI tests; extended browser E2E.
### Changed
- Gemini FIGURES prompt now requests structured extraction (DiagramSpec JSON, observed counts, label confidence/alternatives).
- Documents embed a reconstruction only if it is approved for the exact current spec; otherwise the source crop.
- Rebuilt figures are 300-dpi PNG in Word/PDF (SVG is kept in the audit bundle).
- `exam_core.safe_function` / `latex_to_plain` now delegate to `diagram_engine.safe_math`.
### Removed
- Old direct renderers (`render_rebuilt_figure`, `render_*_png`, `parse_spec`) and the `MIN_REBUILD_CONFIDENCE` gate that trusted the
  model's self-reported confidence. Legacy 5.x `spec_json` is converted automatically.
### Fixed during this work
- Missing `images` variable in the question editor after the figure section was replaced (caught by AppTest).
- Graph tick labels crowded on wide ranges (labels thinned, tick marks kept).

## 5.3
- Merge of 5.2-audited improvements with fixes (see README).
