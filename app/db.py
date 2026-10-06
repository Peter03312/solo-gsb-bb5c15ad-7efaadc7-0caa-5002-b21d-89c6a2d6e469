import os
import sqlite3

from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'viewer'))
);

CREATE TABLE IF NOT EXISTS studies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE NOT NULL,
    seed       INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'unblinded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS treatment_groups (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    UNIQUE (study_id, name)
);

CREATE TABLE IF NOT EXISTS strata (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    expected INTEGER,               -- 允许为空：生成时按“缺少样品”拒绝
    UNIQUE (study_id, name)
);

CREATE TABLE IF NOT EXISTS batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id   INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    code_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT UNIQUE NOT NULL,  -- 全局唯一：样品码一经发放不可复用
    study_id   INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    batch_id   INTEGER NOT NULL REFERENCES batches (id) ON DELETE CASCADE,
    stratum    TEXT NOT NULL,
    group_name TEXT NOT NULL,         -- 盲底：样品码 -> 处理组 的映射
    issued_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unblinding_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id   INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    operator1  TEXT NOT NULL,
    operator2  TEXT NOT NULL,
    reason     TEXT NOT NULL,
    snapshot   TEXT NOT NULL,         -- 揭盲时刻的完整映射快照（JSON）
    created_at TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _seed_users(db):
    """内置账号（开发默认值，可用环境变量覆盖密码）。"""
    defaults = [
        ("admin", os.environ.get("ADMIN_PASSWORD", "admin123"), "admin"),
        ("operator1", os.environ.get("OPERATOR1_PASSWORD", "op123456"), "operator"),
        ("operator2", os.environ.get("OPERATOR2_PASSWORD", "op234567"), "operator"),
        ("viewer", os.environ.get("VIEWER_PASSWORD", "view1234"), "viewer"),
    ]
    for username, password, role in defaults:
        db.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), role),
        )
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        _seed_users(db)
