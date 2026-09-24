"""Teacher review UI for the diagram engine (Streamlit). Kept separate from app.py: UI only, no drawing logic."""
from __future__ import annotations

import hashlib
from typing import Any, Callable

import pandas as pd
import streamlit as st

import diagram_engine as de
import exam_core as core
from diagram_engine.schemas import (
    Asymptote, ChartSpec, CurvePiece, DiagramRecord, GAngleMark, GArc, GCircle, GConstraint, GEqualMark, GLengthLabel,
    GParallelMark, GPoint, GraphCurve, GraphPoint, GSegment,
)

from diagram_engine.facts import SUBTYPE_HE, TYPE_HE, facts as extracted_facts

DECISION_HE = {"high_confidence_preview": "ביטחון גבוה (ממתין לאישורך)", "review": "דורש אישור מורה", "draft": "טיוטה — דורש בדיקה",
               "original": "שימוש במקור"}
STATUS_HE = {"pending": "⏳ ממתין לאישור (במסמך: מקור)", "approved": "✅ אושר", "rejected": "🗑 נדחה (במסמך: מקור)", "original": "↩️ מקור"}


def _txt(v: Any) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def _num(v: Any, default: float | None = None) -> float | None:
    try:
        f = float(v)
        return default if pd.isna(f) else f
    except (TypeError, ValueError):
        return default


def _df(rows: list[dict], cols: dict[str, str]) -> pd.DataFrame:
    data = {c: [r.get(c) for r in rows] for c in cols}
    df = pd.DataFrame(data, columns=list(cols))
    for c, t in cols.items():
        if t == "float":
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        elif t == "int":
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        elif t == "bool":
            df[c] = df[c].fillna(False).astype("bool")
        else:
            df[c] = df[c].fillna("").astype("str")
    return df


def _records(df: pd.DataFrame) -> list[dict]:
    return df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")


@st.cache_data(show_spinner=False, max_entries=128)
def _render_png(spec_json: str) -> bytes | None:
    try:
        from diagram_engine.schemas import DiagramSpec

        return de.render_spec(DiagramSpec.model_validate_json(spec_json))[1]
    except Exception:
        return None


def bundle(exam: core.ExamAnalysis, questions_data: list[dict]) -> bytes:
    by_num = {int(q["question_number"]): q for q in questions_data}
    records, sources = {}, {}
    for q in exam.questions:
        imgs = by_num.get(q.question_number, {}).get("images", [])
        for fig in q.figures:
            rec = q.diagrams.get(fig.figure_id)
            if rec is not None:
                records[fig.figure_id] = rec
                crop = core.figure_source_crop(fig, imgs)
                if crop:
                    sources[fig.figure_id] = crop
    return de.export_bundle(records, sources)


# ------------------------------------------------------------------ editors
def _geometry_editor(rec: DiagramRecord, key: str) -> tuple[Any, str] | None:
    g = rec.spec.geometry
    st.caption("עריכת נתונים מובנית. גרירת נקודות בעכבר אינה נתמכת — שנו קואורדינטות בטבלה (y כלפי מעלה).")
    with st.form(f"geo_form_{key}"):
        pts = st.data_editor(_df([p.model_dump() for p in g.points], {"id": "str", "x": "float", "y": "float", "label": "str"}),
                             num_rows="dynamic", key=f"gp_{key}", hide_index=True, width="stretch",
                             column_config={"label": st.column_config.TextColumn("תווית (ריק = כמו id)")})
        segs = st.data_editor(_df([s.model_dump() for s in g.segments], {"a": "str", "b": "str", "style": "str"}),
                              num_rows="dynamic", key=f"gs_{key}", hide_index=True, width="stretch",
                              column_config={"style": st.column_config.SelectboxColumn("סגנון", options=["solid", "dashed"])})
        angs = st.data_editor(_df([m.model_dump() for m in g.angle_marks], {"vertex": "str", "a": "str", "b": "str", "kind": "str", "value": "str"}),
                              num_rows="dynamic", key=f"ga_{key}", hide_index=True, width="stretch",
                              column_config={"kind": st.column_config.SelectboxColumn("סוג", options=["arc", "right"])})
        eq_rows = [{"group": i + 1, "a": s[0], "b": s[1], "ticks": m.ticks} for i, m in enumerate(g.equal_marks) for s in m.segments]
        eqs = st.data_editor(_df(eq_rows, {"group": "int", "a": "str", "b": "str", "ticks": "int"}), num_rows="dynamic",
                             key=f"ge_{key}", hide_index=True, width="stretch",
                             column_config={"group": st.column_config.NumberColumn("קבוצת שוויון", min_value=1, step=1)})
        par_rows = [{"group": i + 1, "a": s[0], "b": s[1], "arrows": m.arrows} for i, m in enumerate(g.parallel_marks) for s in m.segments]
        pars = st.data_editor(_df(par_rows, {"group": "int", "a": "str", "b": "str", "arrows": "int"}), num_rows="dynamic",
                              key=f"gm_{key}", hide_index=True, width="stretch",
                              column_config={"group": st.column_config.NumberColumn("קבוצת מקבילות", min_value=1, step=1)})
        lens = st.data_editor(_df([l.model_dump() for l in g.length_labels], {"a": "str", "b": "str", "text": "str"}),
                              num_rows="dynamic", key=f"gl_{key}", hide_index=True, width="stretch")
        circ = st.data_editor(_df([c.model_dump() for c in g.circles], {"center": "str", "through": "str", "radius": "float"}),
                              num_rows="dynamic", key=f"gc_{key}", hide_index=True, width="stretch")
        cons = st.data_editor(_df([{"type": c.type, "points": ",".join(c.points), "value": c.value} for c in g.constraints if c.source == "teacher"],
                                  {"type": "str", "points": "str", "value": "float"}), num_rows="dynamic", key=f"gk_{key}",
                              hide_index=True, width="stretch",
                              column_config={"type": st.column_config.SelectboxColumn("יחס (אילוץ)", options=list(de.schemas.CONSTRAINT_ARITY)),
                                             "points": st.column_config.TextColumn("נקודות (A,B,C,D)")})
        if not st.form_submit_button("💾 החל שינויים ובדוק מחדש", type="primary"):
            return None
    new = rec.spec.model_copy(deep=True)
    ng = new.geometry
    ng.points = [GPoint(id=_txt(r["id"]), x=_num(r["x"], 0.0), y=_num(r["y"], 0.0), label=_txt(r["label"]) or None)
                 for r in _records(pts) if _txt(r["id"])]
    ng.segments = [GSegment(a=_txt(r["a"]), b=_txt(r["b"]), style=_txt(r["style"]) or "solid") for r in _records(segs) if _txt(r["a"])]
    ng.angle_marks = [GAngleMark(vertex=_txt(r["vertex"]), a=_txt(r["a"]), b=_txt(r["b"]), kind=_txt(r["kind"]) or "arc", value=_txt(r["value"]))
                      for r in _records(angs) if _txt(r["vertex"])]
    groups: dict[int, GEqualMark] = {}
    for r in _records(eqs):
        if _txt(r["a"]):
            gid = int(_num(r["group"], 1))
            groups.setdefault(gid, GEqualMark(segments=[], ticks=int(_num(r["ticks"], 1) or 1))).segments.append([_txt(r["a"]), _txt(r["b"])])
    ng.equal_marks = list(groups.values())
    pgroups: dict[int, GParallelMark] = {}
    for r in _records(pars):
        if _txt(r["a"]):
            gid = int(_num(r["group"], 1))
            pgroups.setdefault(gid, GParallelMark(segments=[], arrows=int(_num(r["arrows"], 1) or 1))).segments.append([_txt(r["a"]), _txt(r["b"])])
    ng.parallel_marks = list(pgroups.values())
    ng.length_labels = [GLengthLabel(a=_txt(r["a"]), b=_txt(r["b"]), text=_txt(r["text"])) for r in _records(lens) if _txt(r["a"])]
    ng.circles = [GCircle(center=_txt(r["center"]), through=_txt(r["through"]) or None, radius=_num(r["radius"]))
                  for r in _records(circ) if _txt(r["center"])]
    kept = [c for c in ng.constraints if c.source != "teacher"]
    for r in _records(cons):
        if _txt(r["type"]) and _txt(r["points"]):
            kept.append(GConstraint(type=_txt(r["type"]), points=[p.strip() for p in _txt(r["points"]).split(",") if p.strip()],
                                    value=_num(r["value"]), source="teacher"))
    # relations derived from marks are re-derived by the engine from the edited marks
    ng.constraints = [c for c in kept if c.source != "mark"]
    # teacher-confirmed labels are no longer ambiguous
    used = {p.id if p.label is None else p.label for p in ng.points} | {l.text for l in ng.length_labels} | {m.value for m in ng.angle_marks}
    new.labels = [l for l in new.labels if l.text in used or not l.ambiguous]
    for l in new.labels:
        if l.text in used:
            l.confidence, l.alternatives = 1.0, []
    summary = (f"עריכת שרטוט: {len(g.points)}→{len(ng.points)} נקודות, {len(g.segments)}→{len(ng.segments)} קטעים, "
               f"{len(g.angle_marks)}→{len(ng.angle_marks)} סימוני זווית, {len(g.equal_marks)}→{len(ng.equal_marks)} סימוני שוויון, "
               f"{len(g.parallel_marks)}→{len(ng.parallel_marks)} סימוני מקבילות")
    return new, summary


def _graph_editor(rec: DiagramRecord, key: str) -> tuple[Any, str] | None:
    g = rec.spec.graph
    a = g.axes
    st.caption("ביטויים בתחביר מחשבון: + - * / ^ ( ) sqrt cbrt abs ln log exp sin cos tan pi e")
    with st.form(f"graph_form_{key}"):
        c1, c2, c3, c4, c5 = st.columns(5)
        x_min = c1.number_input("x מינימום", value=float(a.x_min), key=f"xmin_{key}")
        x_max = c2.number_input("x מקסימום", value=float(a.x_max), key=f"xmax_{key}")
        y_min = c3.number_input("y מינימום", value=float(a.y_min), key=f"ymin_{key}")
        y_max = c4.number_input("y מקסימום", value=float(a.y_max), key=f"ymax_{key}")
        grid = c5.checkbox("רשת", value=a.show_grid, key=f"grid_{key}")
        s1, s2 = st.columns(2)
        x_step = s1.number_input("מרווח שנתות x", value=float(a.x_step), min_value=0.001, key=f"xs_{key}")
        y_step = s2.number_input("מרווח שנתות y", value=float(a.y_step), min_value=0.001, key=f"ys_{key}")
        curves = st.data_editor(_df([{"id": c.id, "label": c.label, "expression": c.expression} for c in g.curves if not c.pieces],
                                    {"id": "str", "label": "str", "expression": "str"}), num_rows="dynamic", key=f"cv_{key}",
                                hide_index=True, width="stretch")
        pts = st.data_editor(_df([p.model_dump() for p in g.points if p.source != "computed"],
                                 {"name": "str", "x": "float", "y": "float", "style": "str", "on_curve": "str",
                                  "label_dx": "float", "label_dy": "float", "show_coordinates": "bool"}),
                             num_rows="dynamic", key=f"gpp_{key}", hide_index=True, width="stretch",
                             column_config={"style": st.column_config.SelectboxColumn("סוג", options=["closed", "open"])})
        asy = st.data_editor(_df([s.model_dump() for s in g.asymptotes], {"kind": "str", "value": "float", "label": "str"}),
                             num_rows="dynamic", key=f"gas_{key}", hide_index=True, width="stretch",
                             column_config={"kind": st.column_config.SelectboxColumn("סוג", options=["vertical", "horizontal"])})
        if not st.form_submit_button("💾 החל שינויים ובדוק מחדש", type="primary"):
            return None
    new = rec.spec.model_copy(deep=True)
    ng = new.graph
    ng.axes = ng.axes.model_copy(update=dict(x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max, x_step=x_step, y_step=y_step, show_grid=grid))
    pieces = [c for c in ng.curves if c.pieces]
    old = {c.id: c for c in g.curves}
    ng.curves = pieces + [GraphCurve(id=_txt(r["id"]) or f"c{i}", label=_txt(r["label"]), expression=_txt(r["expression"]),
                                     source="teacher" if _txt(r["expression"]) != getattr(old.get(_txt(r["id"])), "expression", None)
                                     else getattr(old.get(_txt(r["id"])), "source", "teacher"), confidence=1.0)
                          for i, r in enumerate(_records(curves)) if _txt(r["expression"])]
    ng.points = [GraphPoint(name=_txt(r["name"]), x=_num(r["x"], 0.0), y=_num(r["y"], 0.0), style=_txt(r["style"]) or "closed",
                            on_curve=_txt(r["on_curve"]), label_dx=_num(r["label_dx"], 0.0), label_dy=_num(r["label_dy"], 0.0),
                            show_coordinates=bool(r["show_coordinates"]), source="teacher") for r in _records(pts)]
    ng.asymptotes = [Asymptote(kind=_txt(r["kind"]) or "vertical", value=_num(r["value"], 0.0), label=_txt(r["label"]), source="teacher")
                     for r in _records(asy) if _num(r["value"]) is not None]
    used = {p.name for p in ng.points if p.name} | {c.label for c in ng.curves if c.label}
    for l in new.labels:
        if l.text in used:
            l.confidence, l.alternatives = 1.0, []
    return new, f"עריכת גרף: {len(ng.curves)} עקומות, {len(ng.points)} נקודות, {len(ng.asymptotes)} אסימפטוטות"


def _chart_editor(rec: DiagramRecord, key: str) -> tuple[Any, str] | None:
    c = rec.spec.chart
    with st.form(f"chart_form_{key}"):
        kinds = ["bar", "histogram", "pie", "line", "frequency_table", "two_way_table"]
        kind = st.selectbox("סוג", kinds, index=kinds.index(c.kind), key=f"ck_{key}")
        title = st.text_input("כותרת", value=c.title, key=f"ct_{key}")
        vals = st.data_editor(_df([{"category": k, "value": v} for k, v in zip(c.categories, c.values)], {"category": "str", "value": "float"}),
                              num_rows="dynamic", key=f"cvv_{key}", hide_index=True, width="stretch")
        pct = st.checkbox("הצג אחוזים (רק אם מופיעים במקור)", value=c.show_percentages, key=f"cp_{key}")
        table = st.text_area("טבלה (שורה לכל שורה, תאים מופרדים ב-|)", value="\n".join(" | ".join(r) for r in c.table), key=f"ctb_{key}")
        if not st.form_submit_button("💾 החל שינויים ובדוק מחדש", type="primary"):
            return None
    new = rec.spec.model_copy(deep=True)
    rows = [r for r in _records(vals) if _txt(r["category"])]
    new.chart = new.chart.model_copy(update=dict(kind=kind, title=title, categories=[_txt(r["category"]) for r in rows],
                                                 values=[_num(r["value"], 0.0) for r in rows], show_percentages=pct,
                                                 table=[[x.strip() for x in line.split("|")] for line in table.splitlines() if line.strip()],
                                                 values_source="teacher"))
    return new, f"עריכת תרשים: {len(rows)} ערכים"


def _drag_editor(rec: DiagramRecord, key: str) -> tuple[Any, str] | None:
    """Drag points on a canvas. After 'apply' the solver re-imposes every constraint: a point constrained to a circle
    snaps back to it; the teacher is told which points were snapped and may remove constraints explicitly."""
    try:
        from streamlit_drawable_canvas import st_canvas
    except Exception:
        st.info("עורך הגרירה אינו זמין בסביבה זו — השתמשו בעריכת הטבלאות.")
        return None
    g = rec.spec.geometry
    vis = [p for p in g.points if not p.hidden]
    xs, ys = [p.x for p in vis], [p.y for p in vis]
    W, H, pad = 640, 420, 40
    sc = min((W - 2 * pad) / max(1e-9, max(xs) - min(xs)), (H - 2 * pad) / max(1e-9, max(ys) - min(ys)))
    to_px = lambda x, y: (pad + (x - min(xs)) * sc, H - pad - (y - min(ys)) * sc)  # noqa: E731
    to_xy = lambda px, py: (min(xs) + (px - pad) / sc, min(ys) + (H - pad - py) / sc)  # noqa: E731
    objs = []
    P = {p.id: to_px(p.x, p.y) for p in vis}
    for s_ in g.segments:
        if s_.a in P and s_.b in P:
            (x1, y1), (x2, y2) = P[s_.a], P[s_.b]
            objs.append({"type": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "left": min(x1, x2), "top": min(y1, y2),
                         "stroke": "#999999", "strokeWidth": 2, "selectable": False, "evented": False})
    for p in vis:
        x, y = P[p.id]
        objs.append({"type": "text", "left": x + 8, "top": y - 24, "text": p.id, "fontSize": 16, "selectable": False, "evented": False})
    for p in vis:
        x, y = P[p.id]
        objs.append({"type": "circle", "left": x - 7, "top": y - 7, "radius": 7, "fill": "#1565c0", "stroke": "#0d47a1",
                     "strokeWidth": 1, "hasControls": False})
    st.caption("הפעילו את מצב העריכה בסרגל הכלים של הלוח, גררו נקודות (העיגולים הכחולים) ולחצו 'החל'. "
               "נקודה שיש עליה אילוץ (למשל על מעגל) תוחזר אל האילוץ. קווים שצוירו בטעות אינם נקראים.")
    res = st_canvas(drawing_mode="freedraw", stroke_width=1, initial_drawing={"version": "4.4.0", "objects": objs}, width=W, height=H,
                    background_color="#ffffff", update_streamlit=True, key=f"drag_{key}")
    removable = [f"{c.type}:{','.join(c.points)}" for c in g.constraints]
    drop = st.multiselect("הסרת אילוצים (רק אם אתם בטוחים שהם שגויים)", removable, key=f"drop_{key}")
    if not st.button("💾 החל גרירה ובדוק מחדש", key=f"apply_drag_{key}", type="primary"):
        return None
    circles = [o for o in ((res.json_data or {}).get("objects") or []) if o.get("type") == "circle"]
    if len(circles) != len(vis):
        st.error("לא ניתן לקרוא את מיקומי הנקודות מהעורך — נסו שוב.")
        return None
    new = rec.spec.model_copy(deep=True)
    moved, dropped = {}, set(drop)
    for p, o in zip([p for p in new.geometry.points if not p.hidden], circles):
        r = float(o.get("radius", 7)) * float(o.get("scaleX", 1))
        nx, ny = to_xy(float(o["left"]) + r, float(o["top"]) + r)
        if abs(nx - p.x) > 1e-6 or abs(ny - p.y) > 1e-6:
            moved[p.id] = (nx, ny)
            p.x, p.y, p.source = nx, ny, "teacher"
    new.geometry.constraints = [c for c in new.geometry.constraints if f"{c.type}:{','.join(c.points)}" not in dropped]
    state = st.session_state
    state[f"drag_moved_{rec.figure_id}"] = moved
    summary = f"גרירת נקודות: {', '.join(moved) or 'ללא'}" + (f"; הוסרו אילוצים: {', '.join(dropped)}" if dropped else "")
    return new, summary


# ------------------------------------------------------------------ main section
def figures_section(q: core.QuestionAnalysis, source: dict[str, Any], v: str, editor: Callable, state: Any) -> None:
    st.markdown("**שרטוטים וגרפים**")
    st.caption("המערכת אינה מציירת לפי ניחוש: השחזור נבנה ממנוע דטרמיניסטי, נבדק מול נוסח השאלה והמקור, "
               "ונכנס למסמך רק אחרי אישור. בכל מקרה של ספק — ישולב השרטוט המקורי.")
    images = source.get("images", [])
    rows = editor(f"fig_{v}", [{"figure_id": f.figure_id, "description": f.description, "source_image_index": f.source_image_index,
                                "bbox": ",".join(map(str, f.bbox))} for f in q.figures],
                  {"figure_id": "str", "description": "str", "source_image_index": "int", "bbox": "str"},
                  {"figure_id": None, "description": st.column_config.TextColumn("תיאור"),
                   "source_image_index": st.column_config.NumberColumn("תמונת מקור", min_value=1, step=1),
                   "bbox": st.column_config.TextColumn("אזור בתמונה (ymin,xmin,ymax,xmax · 0–1000)")})
    old = {f.figure_id: f for f in q.figures}
    figs = []
    used_ids: set[str] = set()
    for r in rows:
        if not _txt(r["description"]):
            continue
        try:
            bbox = [int(float(x)) for x in _txt(r["bbox"]).split(",") if x.strip()]
        except ValueError:
            bbox = []
        fid = _txt(r["figure_id"])
        base = old.get(fid)
        if not fid or fid in used_ids:
            n = 1
            while f"q{q.question_number}t{n}" in used_ids or f"q{q.question_number}t{n}" in old:
                n += 1
            fid = f"q{q.question_number}t{n}"
        used_ids.add(fid)
        figs.append(core.FigureRef(
            description=_txt(r["description"]), source_image_index=max(1, int(_num(r["source_image_index"], 1))),
            bbox=bbox if len(bbox) == 4 and core.bbox_is_valid(bbox) else [], figure_id=fid,
            figure_type=base.figure_type if base else "source_crop", spec_json=base.spec_json if base else "",
            rebuild_confidence=base.rebuild_confidence if base else 0.0,
            geogebra_commands=base.geogebra_commands if base else []))
    q.figures = figs
    core.attach_diagrams(q, images, ai_model=getattr(state.get("processed_exam"), "model_used", ""))
    text = core.question_text_for_diagrams(q)
    for idx, fig in enumerate(q.figures, 1):
        rec = q.diagrams.get(fig.figure_id)
        crop = core.figure_source_crop(fig, images)
        with st.container(border=True):
            st.markdown(f"**תרשים {idx}: {fig.description}**")
            if rec is None:
                st.info("אין נתוני שחזור — ישולב המקור.")
                continue
            ctype = rec.spec.diagram_type if rec.spec else "unknown"
            c1, c2, c3 = st.columns([1, 1, 1])
            if crop:
                c1.image(crop, caption="מקור", width="stretch")
            png = _render_png(rec.spec.model_dump_json()) if (rec.spec and rec.validation.ok and not rec.render_error) else None
            if png:
                c2.image(png, caption="שחזור דטרמיניסטי", width="stretch")
            else:
                c2.info("אין שחזור שמיש — ישולב המקור.")
            with c3:
                st.markdown("**בדיקת המערכת**")
                sub = SUBTYPE_HE.get(rec.classifier_subtype or "", rec.classifier_subtype or "")
                st.write(f"סוג: {TYPE_HE.get(ctype, ctype)}" + (f" · {sub}" if sub else ""))
                st.write(f"ביטחון סיווג: {rec.classifier_confidence:.0%} · ביטחון ניתוח: {rec.parser_confidence:.0%}")
                st.write(f"אימות: {'עבר' if rec.validation.ok else 'נכשל'} · השוואה למקור: {rec.comparison.overall_score:.0%}")
                st.write(f"החלטה: {DECISION_HE.get(rec.decision.action, rec.decision.action)}")
                st.caption(STATUS_HE.get(rec.review.status, ""))
                for ok, text in extracted_facts(rec)[:14]:
                    st.write(("✓ " if ok else "⚠ ") + text)
            if rec.teacher_message:
                st.write(rec.teacher_message)
            if state.get(f"snap_msg_{fig.figure_id}"):
                st.warning(state.pop(f"snap_msg_{fig.figure_id}"))
            for e in rec.validation.errors:
                st.error(e)
            for w in rec.validation.warnings:
                st.warning(w)
            for n in rec.comparison.notes:
                st.caption("• " + n)
            ek = f"edit_{fig.figure_id}_{v}"
            b1, b2, b3, b4 = st.columns(4)
            if b1.button("✅ אשר שחזור", key=f"ok_{fig.figure_id}_{v}", disabled=not de.can_approve(rec) or rec.review.status == "approved",
                         width="stretch"):
                de.approve(rec)
                st.rerun()
            if b2.button("✏️ ערוך שחזור", key=f"ed_{fig.figure_id}_{v}", disabled=rec.spec is None or ctype in ("unknown", "generic", "spatial", "table"),
                         width="stretch"):
                state[ek] = not state.get(ek, False)
                st.rerun()
            if b3.button("↩️ השתמש במקור", key=f"or_{fig.figure_id}_{v}", width="stretch"):
                de.use_original(rec)
                st.rerun()
            if b4.button("🗑 דחה שחזור", key=f"rj_{fig.figure_id}_{v}", width="stretch"):
                de.reject(rec)
                st.rerun()
            b5, b6, b7 = st.columns(3)
            if b5.button("🔄 נתח מחדש", key=f"re_{fig.figure_id}_{v}", width="stretch",
                         help="מריץ מחדש את הבדיקות הדטרמיניסטיות על נתוני ה-AI המקוריים (ללא קריאה נוספת ל-AI; עריכות יבוטלו)."):
                q.diagrams[fig.figure_id] = de.process(fig.figure_id, fig.spec_json, text, crop, figure_type_hint=fig.figure_type,
                                                       fallback_confidence=fig.rebuild_confidence, ai_model=rec.audit.get("ai_model", ""))
                q.diagrams[fig.figure_id].audit["input_key"] = rec.audit.get("input_key")
                q.diagrams[fig.figure_id].audit["question_text"] = rec.audit.get("question_text")
                st.rerun()
            if b6.button("✅ אשר טקסט בלבד", key=f"tx_{fig.figure_id}_{v}", width="stretch",
                         help="נוסח השאלה מאושר; התרשים יישאר המקורי."):
                q.teacher_verified = True
                de.use_original(rec)
                st.rerun()
            if b7.button("✅ אשר תרשים בלבד", key=f"fo_{fig.figure_id}_{v}", disabled=not de.can_approve(rec), width="stretch",
                         help="מאשר את השחזור בלבד; אימות נוסח השאלה נשאר בנפרד."):
                de.approve(rec)
                st.rerun()
            if state.get(ek) and rec.spec is not None:
                key = f"{fig.figure_id}_{de.review_state.spec_hash(rec.spec)}_{v}"
                editors = {"geometry": _geometry_editor, "graph": _graph_editor, "chart": _chart_editor}
                result = None
                if ctype == "geometry":
                    tab_drag, tab_table = st.tabs(["🖱️ גרירת נקודות", "📋 טבלאות"])
                    with tab_drag:
                        result = _drag_editor(rec, key)
                    with tab_table:
                        result = result or _geometry_editor(rec, key)
                elif ctype == "graph" and rec.spec.graph is not None and (rec.classifier_subtype or "formula_graph") == "formula_graph":
                    result = _graph_editor(rec, key)
                elif ctype == "chart" and rec.spec.chart is not None:
                    result = _chart_editor(rec, key)
                else:
                    st.info("לסוג תרשים זה אין עורך מובנה — ניתן לאשר, לנתח מחדש או להשתמש במקור.")
                if result is not None:
                    new_spec, summary = result
                    try:
                        new_rec = de.apply_teacher_edit(rec, new_spec, summary, text, crop, ai_model=rec.audit.get("ai_model", ""))
                        moved = state.pop(f"drag_moved_{fig.figure_id}", {})
                        snapped = []
                        if moved and new_rec.spec is not None and new_rec.spec.geometry is not None:
                            from diagram_engine.geometry.constraints import size_of
                            size = size_of(new_rec.spec.geometry)
                            final = {p.id: (p.x, p.y) for p in new_rec.spec.geometry.points}
                            snapped = [k for k, (x, y) in moved.items() if k in final and
                                       ((final[k][0] - x) ** 2 + (final[k][1] - y) ** 2) ** 0.5 > 0.03 * size]
                        if snapped:
                            state[f"snap_msg_{fig.figure_id}"] = ("הנקודות " + ", ".join(snapped) +
                                                                   " הוחזרו למיקום המקיים את האילוצים (למשל על המעגל). כדי להזיז אותן בחופשיות יש להסיר את האילוץ.")
                        q.diagrams[fig.figure_id] = new_rec
                        state[ek] = False
                    except Exception as exc:
                        st.error(f"לא ניתן להחיל את השינויים: {exc}")
                        return
                    st.rerun()
