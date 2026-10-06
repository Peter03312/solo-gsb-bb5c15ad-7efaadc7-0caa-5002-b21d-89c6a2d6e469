import functools

from flask import (
    Blueprint,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from .db import get_db

bp = Blueprint("auth", __name__)


def authenticate(username, password):
    """校验用户名密码，成功返回用户行，失败返回 None。"""
    if not username or not password:
        return None
    user = get_db().execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if user is not None and check_password_hash(user["password_hash"], password):
        return user
    return None


@bp.before_app_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


@bp.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        user = authenticate(
            request.form.get("username", "").strip(),
            request.form.get("password", ""),
        )
        if user is None:
            flash("用户名或密码错误", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("routes.dashboard"))
    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


def _wants_json():
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def login_required(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            if _wants_json():
                return jsonify(ok=False, errors=["未登录"]), 401
            return redirect(url_for("auth.login"))
        return view(**kwargs)

    return wrapped


def role_required(*roles):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(**kwargs):
            if g.user is None:
                if _wants_json():
                    return jsonify(ok=False, errors=["未登录"]), 401
                return redirect(url_for("auth.login"))
            if g.user["role"] not in roles:
                if _wants_json():
                    return jsonify(ok=False, errors=["权限不足"]), 403
                flash("权限不足", "error")
                return redirect(url_for("routes.dashboard"))
            return view(**kwargs)

        return wrapped

    return decorator
