# AUDIT — גרסה 5.5.0

**A. Existing architecture reviewed** — 5.4: `app.py` (5 steps, image editor with crop/erase/rotate/straighten/undo, fragments),
`exam_core.py` (Gemini with retries, model fallback and adaptive rate limiter; scoring/validation; image processing; Word/PDF),
`diagram_engine/` v1 (graph, geometry, chart, generic), `diagram_ui.py`, tests (169) and browser E2E. The six questionnaires
(14–19, 66 pages) were scanned; 90 vector regions were extracted automatically and ~40 real figures mapped to engines.

**B. Major architectural changes** — family sub-packages (graph / geometry / mixed / charts / generic / spatial); Evidence layer;
schema v2 with subtypes; Comparison v2 (7 scores); decision v2 without any automatic export; single API `process_diagram`
→ `DiagramResult`; per-figure audit; thread-safe caches; render lock.

**C. Bugs found**
1. Hebrew text with brackets inside figures was drawn with reversed brackets — python-bidi 0.6 reorders but does not mirror (all versions ≤5.4).
2. Inline math inside Hebrew UI lines was reversed (e.g. heading showed "4x² − y =") — `.katex` had `direction:ltr` without isolation.
3. Race: parallel question workers render with matplotlib, whose rcParams are global (`rc_context`).
4. Gemini calls had no HTTP timeout — one stuck request could hang the analysis.
5. Solver flagged a false "point changed side" when a constraint moved the point exactly ONTO the line.
6. `(x²−4)/(x−2)` reported an oblique asymptote (degree computed before cancelling).
7. `ln(x−1)` vertical asymptote missed (sampling-based detection).
8. Label score 0.5 for diagrams with no text at all / for fully verified tables → needlessly low confidence.
9. Scatter detection: light-gray grid not detected; dots lying on grid intersections were cut apart (found on the real Bagrut crop).
10. streamlit-drawable-canvas 0.13 removed `drawing_mode="transform"` (drag editor crashed — caught by AppTest).
11. Test folder `tests/diagram_engine/__init__.py` shadowed the real package (test infrastructure).
12. Axis names (x, y) were drawn but not recorded in the manifest, so they counted as "missing labels" and lowered confidence.

**D. Bugs fixed** — all 12 above, each with a regression test (bidi, render race, feature cases, flip, scatter on the real fixture,
AppTest for the editor). The KaTeX fix is CSS and was verified on a browser screenshot (no automated assertion).

**E. DiagramSpec / Evidence** — Pydantic schema 2.0 (`schema_version`, `parser_version`, `diagram_type`, `subtype`, confidences,
`evidence`, family blocks, `observed` source signature). EvidenceItem with source, fact_type, value, confidence, bbox, raw_text,
ambiguous, alternatives. Hierarchy teacher > question text > diagram symbol > deterministic detection/derived math > OCR > vision.

**F. Graph engine** — formula graphs from the question text (text beats pixels); features: domain intervals, roots, y-intercept,
extrema, monotonicity, vertical/horizontal/oblique asymptotes, holes, symmetry, branches; real rational powers (`real_rational_power`,
x^(±1/3), x^(±2/3), x^(4/3)); AST whitelist after parsing. Qualitative graphs = topology only (landmarks, branches, end behaviour,
asymptotes), drawn with a monotone cubic so no extremum can be invented; validation rejects unmarked turns. Multiple choice I–IV:
separate options, separate manifests, deterministic option matching kept separate from rendering.

**G. Geometry engine** — 37 constraint types (incl. point_order, concyclic, circle through points with hidden centre, diameter,
radius, chord, secant, tangent, intersection, equilateral/isosceles/rectangle/square, axis relations, inside/outside circle/polygon,
fixed coordinates, ratio on segment); Gauss–Newton solver with fixed points; side-flip check; incidence checks; renderer with
equal-angle marks, shaded polygons, dimension lines, coordinate axes. Not-to-scale rule enforced in the parser.

**H. Mixed graph-geometry** — one coordinate system; `on_curve`, `on_x_axis`, `on_y_axis`; curve drawn first, construction second.

**I. Charts and tables** — scatter with deterministic dot detection and pixel→value mapping (unstable mapping ⇒ original);
normal-distribution template (regions, percentages verbatim, callouts, dividing lines, answer boxes, symmetry); tables with exact cell
comparison and native DOCX tables on export; bar/line/histogram/pie as before.

**J. Spatial / 3D** — voxel columns with painter-buffer visibility (a column whose top is not visible ⇒ critical, never guessed);
cuboid/cylinder/polyhedron with fixed oblique projection, hidden edges, dimensions with explicit meaning (radius ≠ diameter),
relations, vectors, points on edges by ratio.

**K. Crop/Erase regression** — editor code untouched. Unit tests: crop, erase, crop→erase, erase→crop, rotate→erase, erase→rotate,
straighten→crop, undo history, edited bytes = bytes sent to the AI and to the diagram engine. Browser E2E (normal + free-tier):
passed.

**L. Streamlit performance** — pipeline re-runs only when the input key changes (image digest + bbox + spec + parser version);
approval / rejection never re-runs anything; renders cached (LRU) and serialised; teacher editors in forms; "re-analyse" re-runs the
deterministic pipeline without calling the AI.

**M. Security** — no eval/exec; whitelist of identifiers; exponent guard before simplification; AST validation of the parsed tree.

**N. Export gating** — `usable_in_document` = approved by a teacher AND bound to the exact spec hash AND validation passed AND no
critical mismatch. Otherwise the source crop. Tables approved → native Word table.

**O–R. Tests** — see TEST_REPORT_V5_5.md (301 automated tests passed, 0 failed; 2 browser E2E runs passed; NOT TESTED list there).

**S. Acceptance examples** — see ACCEPTANCE_TEST_REPORT.md (16 cases: 15 reconstruct-with-teacher-review, 1 fallback-to-original by
design; 14 corrupted candidates all blocked).

**T. Remaining limitations**
| Limitation | Safe fallback | Future |
|---|---|---|
| Structure is still proposed by the vision model; only scatter dots are measured from pixels | comparison + teacher; weak evidence ⇒ draft/original | OpenCV-based line/circle/label detection as independent evidence |
| No local OCR engine: labels and table cells come from Gemini | ambiguous label ⇒ validation error; tables need approval | optional Tesseract layer |
| Real Gemini output for the new families NOT TESTED | missing fields ⇒ lower confidence ⇒ draft/original | run the 6 questionnaires through the real API and tune the prompt |
| Word embeds 300-dpi PNG, not SVG (SVG kept in the audit bundle) | sharp in print | svgBlip embedding |
| Drag editor: drag-and-apply NOT exercised by automated tests (canvas component) | table editors (tested) | Playwright drag test |
| Cube structures: no pixel-level cube detection; hidden columns ⇒ original | original image | isometric grid detection |
| Scatter detection validated on PDF-quality crops only; perspective photos may be unstable | unstable ⇒ original | perspective correction |
| Figures of one question are processed sequentially (parallelism is per question, bounded) | — | per-figure pool (MAX_DIAGRAM_WORKERS) |
| Piecewise functions are not parsed from question text | spec from vision/teacher | text parser |

**U. Files added / V. Files modified / W. Dependencies** — see CHANGELOG_V5_5.md (no new runtime dependencies).

**X. How to run** — `pip install -r requirements.txt` (and `packages.txt` on Linux/Streamlit Cloud), `streamlit run app.py`;
tests: `pip install -r requirements-dev.txt && python -m pytest -q tests`.
