import json

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import services
from .auth import authenticate, login_required, role_required
from .db import get_db
from .services import ConflictError, ReplacementConflictError, ServiceError

bp = Blueprint("routes", __name__)


def _get_study_or_404(db, study_id):
    study = db.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
    if study is None:
        abort(404)
    return study


# ---------------------------------------------------------------- 页面

@bp.route("/")
@login_required
def dashboard():
    studies = get_db().execute(
        """
        SELECT s.*,
               (SELECT COUNT(*) FROM codes c WHERE c.study_id = s.id)   AS code_count,
               (SELECT COUNT(*) FROM batches b WHERE b.study_id = s.id) AS batch_count
        FROM studies s ORDER BY s.id DESC
        """
    ).fetchall()
    return render_template("dashboard.html", studies=studies)


@bp.route("/studies/new", methods=("GET", "POST"))
@role_required("admin")
def study_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        seed_raw = request.form.get("seed", "").strip()
        groups = services.parse_groups(request.form.get("groups", ""))
        strata, parse_errors = services.parse_strata(request.form.get("strata", ""))
        try:
            if parse_errors:
                raise ServiceError(parse_errors)
            seed = int(seed_raw) if seed_raw.lstrip("-").isdigit() else current_app.config["DEFAULT_SEED"]
            study_id = services.create_study(
                get_db(), name, seed, groups, strata, g.user["username"]
            )
            flash("研究创建成功，可前往详情页生成样品码", "ok")
            return redirect(url_for("routes.study_detail", study_id=study_id))
        except ServiceError as e:
            for err in e.errors:
                flash(err, "error")
    return render_template(
        "study_new.html", default_seed=current_app.config["DEFAULT_SEED"]
    )


@bp.route("/studies/<int:study_id>")
@login_required
def study_detail(study_id):
    db = get_db()
    study = _get_study_or_404(db, study_id)
    strata = db.execute(
        "SELECT * FROM strata WHERE study_id = ? ORDER BY id", (study_id,)
    ).fetchall()
    groups = db.execute(
        "SELECT * FROM treatment_groups WHERE study_id = ? ORDER BY id", (study_id,)
    ).fetchall()
    codes = services.get_codes_with_chain(db, study_id)
    replacements = services.get_replacements(db, study_id)
    events = db.execute(
        "SELECT * FROM unblinding_events WHERE study_id = ? ORDER BY id DESC",
        (study_id,),
    ).fetchall()
    return render_template(
        "study_detail.html",
        study=study,
        strata=strata,
        groups=groups,
        codes=codes,
        replacements=replacements,
        events=events,
        can_view_mapping=services.can_view_mapping(study, g.user),
    )


@bp.route("/studies/<int:study_id>/generate", methods=("POST",))
@role_required("admin")
def generate(study_id):
    db = get_db()
    study = _get_study_or_404(db, study_id)
    try:
        batch_id, count = services.generate_batch(db, study, g.user["username"])
        flash(f"批次 #{batch_id} 生成成功，共发放 {count} 个样品码", "ok")
    except ConflictError as e:
        for err in e.errors:
            flash(err, "error")
        for code in e.conflicts[:20]:
            flash(f"冲突样品码：{code}", "error")
        if len(e.conflicts) > 20:
            flash(f"……冲突共 {len(e.conflicts)} 个，仅列出前 20 个", "error")
    except ServiceError as e:
        for err in e.errors:
            flash(err, "error")
    return redirect(url_for("routes.study_detail", study_id=study_id))


@bp.route("/studies/<int:study_id>/replace", methods=("POST",))
@role_required("admin")
def replace_code(study_id):
    db = get_db()
    study = _get_study_or_404(db, study_id)
    try:
        record, replayed = services.replace_code(
            db,
            study,
            request.form.get("old_code", ""),
            request.form.get("reason", ""),
            request.form.get("request_id", ""),
            g.user["username"],
        )
        if replayed:
            flash(
                f"该请求标识已办理过补发：{record['old_code']} → {record['new_code']}（返回原结果）",
                "ok",
            )
        else:
            flash(
                f"补发成功：原码 {record['old_code']} 已作废，新码 {record['new_code']}"
                " 已继承原分层与处理组",
                "ok",
            )
    except ServiceError as e:
        for err in e.errors:
            flash(err, "error")
    return redirect(url_for("routes.study_detail", study_id=study_id))


@bp.route("/studies/<int:study_id>/mapping")
@login_required
def mapping(study_id):
    db = get_db()
    study = _get_study_or_404(db, study_id)
    if not services.can_view_mapping(study, g.user):
        flash("映射仅授权人员可读", "error")
        return redirect(url_for("routes.study_detail", study_id=study_id))
    return render_template(
        "mapping.html", study=study, rows=services.get_mapping(db, study_id)
    )


@bp.route("/studies/<int:study_id>/unblind", methods=("GET", "POST"))
@login_required
def unblind(study_id):
    db = get_db()
    study = _get_study_or_404(db, study_id)
    if request.method == "POST":
        try:
            event_id = services.unblind_study(
                db,
                study,
                request.form.get("operator1", ""),
                request.form.get("password1", ""),
                request.form.get("operator2", ""),
                request.form.get("password2", ""),
                request.form.get("reason", ""),
                current_app.config["MIN_REASON_LENGTH"],
            )
            flash("揭盲成功：已记录时间、操作者与映射快照", "ok")
            return redirect(url_for("routes.unblinding_event", event_id=event_id))
        except ServiceError as e:
            for err in e.errors:
                flash(err, "error")
    return render_template(
        "unblind.html",
        study=study,
        min_reason_length=current_app.config["MIN_REASON_LENGTH"],
    )


@bp.route("/unblinding/<int:event_id>")
@login_required
def unblinding_event(event_id):
    db = get_db()
    event = db.execute(
        "SELECT * FROM unblinding_events WHERE id = ?", (event_id,)
    ).fetchone()
    if event is None:
        abort(404)
    study = _get_study_or_404(db, event["study_id"])
    snapshot = json.loads(event["snapshot"])
    return render_template(
        "unblinding_event.html", event=event, study=study, snapshot=snapshot
    )


# ---------------------------------------------------------------- JSON API

@bp.get("/api/health")
def api_health():
    return jsonify(ok=True)


@bp.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or request.form
    user = authenticate(data.get("username", ""), data.get("password", ""))
    if user is None:
        return jsonify(ok=False, errors=["用户名或密码错误"]), 401
    session.clear()
    session["user_id"] = user["id"]
    return jsonify(ok=True, user={"username": user["username"], "role": user["role"]})


@bp.get("/api/studies")
@login_required
def api_studies():
    rows = get_db().execute(
        "SELECT id, name, seed, status, created_by, created_at FROM studies ORDER BY id"
    ).fetchall()
    return jsonify(ok=True, studies=[dict(r) for r in rows])


@bp.get("/api/studies/<int:study_id>")
@login_required
def api_study(study_id):
    db = get_db()
    study = db.execute(
        "SELECT id, name, seed, status, created_by, created_at FROM studies WHERE id = ?",
        (study_id,),
    ).fetchone()
    if study is None:
        return jsonify(ok=False, errors=["研究不存在"]), 404
    return jsonify(ok=True, study=dict(study))


@bp.post("/api/studies")
@role_required("admin")
def api_create_study():
    data = request.get_json(silent=True) or {}
    groups = data.get("groups", [])
    if isinstance(groups, str):
        groups = services.parse_groups(groups)
    try:
        seed = int(data.get("seed") or current_app.config["DEFAULT_SEED"])
    except (TypeError, ValueError):
        return jsonify(ok=False, errors=["随机种子必须是整数"]), 400
    try:
        study_id = services.create_study(
            get_db(),
            str(data.get("name", "")).strip(),
            seed,
            groups,
            services.normalize_strata(data.get("strata")),
            g.user["username"],
        )
    except ServiceError as e:
        return jsonify(ok=False, errors=e.errors), 400
    return jsonify(ok=True, study_id=study_id), 201


@bp.post("/api/studies/<int:study_id>/generate")
@role_required("admin")
def api_generate(study_id):
    db = get_db()
    study = db.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
    if study is None:
        return jsonify(ok=False, errors=["研究不存在"]), 404
    try:
        batch_id, count = services.generate_batch(db, study, g.user["username"])
    except ConflictError as e:
        return jsonify(ok=False, errors=e.errors, conflicts=e.conflicts), 409
    except ServiceError as e:
        return jsonify(ok=False, errors=e.errors), 400
    return jsonify(ok=True, batch_id=batch_id, count=count), 201


@bp.get("/api/studies/<int:study_id>/codes")
@login_required
def api_codes(study_id):
    db = get_db()
    study = db.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
    if study is None:
        return jsonify(ok=False, errors=["研究不存在"]), 404
    show_group = services.can_view_mapping(study, g.user)
    codes = services.get_codes_with_chain(db, study_id)
    for item in codes:
        item.pop("id", None)
        if not show_group:
            item["group_name"] = None  # 未授权不返回组别
    return jsonify(ok=True, codes=codes, blinded=not show_group)


@bp.post("/api/studies/<int:study_id>/replacements")
@role_required("admin")
def api_replace_code(study_id):
    db = get_db()
    study = db.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
    if study is None:
        return jsonify(ok=False, errors=["研究不存在"]), 404
    data = request.get_json(silent=True) or request.form
    try:
        record, replayed = services.replace_code(
            db,
            study,
            data.get("old_code", ""),
            data.get("reason", ""),
            data.get("request_id", ""),
            g.user["username"],
        )
    except ReplacementConflictError as e:
        return jsonify(ok=False, errors=e.errors), 409
    except ServiceError as e:
        return jsonify(ok=False, errors=e.errors), 400
    body = dict(ok=True, replayed=replayed, replacement=record)
    return jsonify(body), (200 if replayed else 201)


@bp.get("/api/studies/<int:study_id>/replacements")
@login_required
def api_replacements(study_id):
    db = get_db()
    study = db.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
    if study is None:
        return jsonify(ok=False, errors=["研究不存在"]), 404
    show_group = services.can_view_mapping(study, g.user)
    replacements = services.get_replacements(db, study_id)
    for item in replacements:
        item.pop("study_id", None)
        if not show_group:
            item["group_name"] = None  # 揭盲前普通用户看不到处理组
    return jsonify(ok=True, replacements=replacements, blinded=not show_group)


@bp.get("/api/studies/<int:study_id>/mapping")
@login_required
def api_mapping(study_id):
    db = get_db()
    study = db.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
    if study is None:
        return jsonify(ok=False, errors=["研究不存在"]), 404
    if not services.can_view_mapping(study, g.user):
        return jsonify(ok=False, errors=["映射仅授权人员可读"]), 403
    return jsonify(ok=True, mapping=services.get_mapping(db, study_id))


@bp.post("/api/studies/<int:study_id>/unblind")
@login_required
def api_unblind(study_id):
    db = get_db()
    study = db.execute("SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
    if study is None:
        return jsonify(ok=False, errors=["研究不存在"]), 404
    data = request.get_json(silent=True) or request.form
    try:
        event_id = services.unblind_study(
            db,
            study,
            data.get("operator1", ""),
            data.get("password1", ""),
            data.get("operator2", ""),
            data.get("password2", ""),
            data.get("reason", ""),
            current_app.config["MIN_REASON_LENGTH"],
        )
    except ServiceError as e:
        return jsonify(ok=False, errors=e.errors), 400
    return jsonify(ok=True, event_id=event_id)


@bp.get("/api/studies/<int:study_id>/unblinding-events")
@login_required
def api_unblinding_events(study_id):
    db = get_db()
    rows = db.execute(
        "SELECT id, study_id, operator1, operator2, reason, snapshot, created_at"
        " FROM unblinding_events WHERE study_id = ? ORDER BY id DESC",
        (study_id,),
    ).fetchall()
    events = []
    for r in rows:
        item = dict(r)
        item["snapshot"] = json.loads(item["snapshot"])
        events.append(item)
    return jsonify(ok=True, events=events)
