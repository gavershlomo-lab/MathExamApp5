# ACCEPTANCE TEST REPORT — Bagrut questionnaires 14–19

What is tested: the deterministic engine on source crops extracted from the supplied PDFs, fed with the structured extraction an
ideal vision model would return (hand-authored from the PDF). Real Gemini extraction is NOT TESTED (see TEST_REPORT).
Pass = validation PASS, 0 critical, required facts/labels/topology 100%, teacher approval still required — or the designed safe
fallback to the original.

Note: some cases in the task prompt (e.g. aquarium 60×30×40 with a 6/18 cylinder, semicircle + rectangle, circle with CB diameter
and F on the x-axis) do not appear in the six questionnaires. The acceptance suite uses the real questions below; the prompt's
scenarios are covered by synthetic unit tests (test_mixed_charts_spatial.py, test_geometry_v2.py).

| Case | Source | Engine | Result | Facts verified |
|---|---|---|---|---|
| A01 | q14 p2 qualitative f(x) | graph/qualitative | reconstruct (review) | max (6,a), intercept (12,0), min (18,−2a), one branch, y=0 both ends, no equation |
| A02 | q16 p7 f(x)=2x−4√(5x−16) + I–IV | graph/multiple choice | reconstruct | domain x≥3.2, f′(8.2)=0, 4 separate options, option II matched deterministically |
| A03 | q14 p3 rational g(x) + I–IV | graph/multiple choice | reconstruct | branches and asymptotes per option preserved |
| A04 | q14 p7 f(x)=2√(x²−1) | graph/formula | reconstruct | roots ±1, 2 branches, even; points A,B of the text NOT drawn (not in the source figure) |
| A05 | q16 p3 scatter | chart/scatter | reconstruct | 6 dots detected from pixels, exact coordinates |
| A06 | q16 p8 entrance/path/garden | generic | reconstruct | 3 regions, 9 labels, adjacency, "8 מטרים" attached to A–G |
| A07 | q18 p12 table | table | reconstruct | 3×5 cells exact |
| A07b | q19 p4 table | table | reconstruct | 3×4 cells exact, Hebrew brackets correct |
| A08 | q16 p5 circle, B(0,18), A(−16,6) | geometry/analytic | reconstruct (review) | M=(−24,0), C=(−32,−6), D=(0,−46/3), tangent at A, AC diameter, M-A-B order |
| A09 | q15 p2 box | spatial/cuboid | reconstruct | 8 vertices, 12 edges, 3 hidden edges at A |
| A09b | q17 p3 pyramid SABC | spatial/vector | reconstruct | vectors u,v,w; E midpoint BS; F at 2/3 of BC |
| A10 | q15 p5 log graphs I–IV | graph/multiple choice | reconstruct | branch/asymptote counts per option |
| A11 | q19 p8 normal curve | chart/normal | reconstruct | 12 regions, 11 lines, 11 boxes, percentages verbatim, symmetric |
| A12 | q19 p12 cube structure | spatial/voxel | **original by design** | hidden columns cannot be read — no cube invented |
| A13 | q19 p14 carton + cup | spatial/solids | reconstruct | 24 / 6.5 / 6.5 cm on the right edges; cup "3 cm" is a RADIUS |
| A14 | q14 p5 circle, diameter AB, kite EMKO | geometry/circle | reconstruct | OA=OB=OC, AB diameter, K=AM∩CO, MK=ME, OK=OE, M outside |

Corrupted candidates (must be blocked) — all 14 blocked → original, approval impossible:
extra maximum (A01), extra / missing / moved scatter point (A05), missing region (A06), one wrong table number (A07), lost tangent
(A08), missing edge (A09), changed percentage and missing region (A11), wrong dimension (A13), extra point and wrong point order
(A14), merged options (A02).

Totals: 16 cases — 15 reconstruct (teacher approval required), 1 fallback-to-original by design; 14/14 corruptions blocked;
42 acceptance tests passed, 0 failed.
