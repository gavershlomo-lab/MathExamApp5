# TEST REPORT — 5.5.0

Environment: Linux sandbox, Python 3.12, Streamlit 1.64.0, streamlit-drawable-canvas 0.13.0, LibreOffice (with math) for PDF.
Gemini: **mocked** (no network access to Google from the sandbox).

| Suite | File(s) | Passed | Failed |
|---|---|---|---|
| Acceptance (Bagrut questionnaires 14–19) | tests/diagram_engine/acceptance/test_acceptance.py | 42 | 0 |
| Crop/Erase/Rotate/Straighten/Undo regression + edited-bytes proof + native table export | tests/diagram_engine/test_crop_erase_regression.py | 9 | 0 |
| Decision policy, export gate, no-hallucination, render race, bidi, process_diagram API | tests/diagram_engine/test_decision_export_hallucination.py | 28 | 0 |
| Evidence + classifier | tests/diagram_engine/test_evidence_classifier.py | 5 | 0 |
| Geometry v2 (constraints, solver, circle, tangent, point order, renderer) | tests/diagram_engine/test_geometry_v2.py | 15 | 0 |
| Graph engine (rational powers, AST, features, qualitative, multiple choice) | tests/diagram_engine/test_graph_engine.py | 24 | 0 |
| Mixed, normal distribution, tables, generic, voxel, solids, vector box | tests/diagram_engine/test_mixed_charts_spatial.py | 9 | 0 |
| Engine regression from 5.4 (updated to 5.5 policy) | tests/test_diagram_engine.py | 77 | 0 |
| Core (Gemini flow, scoring, Word/PDF, OMML, RTL, quotas) | tests/test_core.py | 86 | 0 |
| Streamlit AppTest (review buttons, editors) | tests/test_app.py | 6 | 0 |
| **Total (`python -m pytest -q tests`)** | | **301** | **0** |

Other checks
- `python -m compileall` — OK. Import smoke test — OK. AppTest full flow — OK.
- Browser E2E (Playwright, mock Gemini): normal mode — PASSED; free-tier simulation (2 requests/min) — PASSED.
  Covers upload, crop, crop→erase, erase (pixel check), straighten, crop, undo, analysis, edited images sent, diagram approval,
  7 downloads.
- Word XSD validation of the 3 downloaded .docx — PASSED. PDF rendered and inspected visually (Hebrew RTL, formulas, figure).
- Visual inspection of all acceptance reconstructions side by side with the sources — done (contact sheets).

NOT TESTED
- Real Gemini API (no network in the sandbox). How to test: run the app with a key on the six questionnaires and review step 4.
- Microsoft Word rendering (only XSD validation + LibreOffice). How to test: open the three .docx files in Word.
- Dragging points in the drag editor (canvas interaction) — only rendering is covered. How to test: step 4 → ✏️ → 🖱️.
- Streamlit Community Cloud deployment.
- KaTeX isolation CSS — verified on a screenshot only.
