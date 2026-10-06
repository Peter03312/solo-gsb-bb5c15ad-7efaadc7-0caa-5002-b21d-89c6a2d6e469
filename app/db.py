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
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT UNIQUE NOT NULL,  -- 全局唯一：样品码一经发放不可复用
    study_id       INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    batch_id       INTEGER REFERENCES batches (id) ON DELETE CASCADE,  -- 补发码无所属批次
    stratum        TEXT NOT NULL,
    group_name     TEXT NOT NULL,         -- 盲底：样品码 -> 处理组 的映射
    issued_at      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'voided')),
    replaced_by_id INTEGER REFERENCES codes (id)  -- 作废后指向补发新码
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

-- 补发记录：同一研究内请求标识唯一（幂等重试返回原结果）
CREATE TABLE IF NOT EXISTS replacements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id     INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
    request_id   TEXT NOT NULL,       -- 客户端请求标识，用于幂等重试
    old_code_id  INTEGER NOT NULL REFERENCES codes (id),
    old_code     TEXT NOT NULL,
    new_code_id  INTEGER NOT NULL REFERENCES codes (id),
    new_code     TEXT NOT NULL,
    stratum      TEXT NOT NULL,       -- 新码继承的分层
    group_name   TEXT NOT NULL,       -- 新码继承的处理组（盲底）
    reason       TEXT NOT NULL,       -- 补发原因
    requested_by TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (study_id, request_id)
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # 写锁竞争时等待而非立即报 locked（支撑并发补发的串行化）
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate(db):
    """对既有库平滑升级：新增补发相关列；batch_id 需放宽为可空时重建表。"""
    cols = db.execute("PRAGMA table_info(codes)").fetchall()
    if not cols:
        return
    names = {c["name"] for c in cols}
    if "status" not in names:
        db.execute(
            "ALTER TABLE codes ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            " CHECK (status IN ('active', 'voided'))"
        )
    if "replaced_by_id" not in names:
        db.execute(
            "ALTER TABLE codes ADD COLUMN replaced_by_id INTEGER REFERENCES codes (id)"
        )

    cols = db.execute("PRAGMA table_info(codes)").fetchall()
    batch_col = next(c for c in cols if c["name"] == "batch_id")
    if batch_col["notnull"] == 1:
        # 补发码没有批次，batch_id 必须可空：SQLite 需重建表才能放宽 NOT NULL。
        # 注意：外键指向 codes 的表（如 replacements）在 RENAME 后仍指向 codes_old，
        # 必须一并重建，否则之后写入会报 “no such table: main.codes_old”。
        db.commit()  # PRAGMA foreign_keys 不可在事务内切换
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("ALTER TABLE codes RENAME TO codes_old")
        db.execute(
            """
            CREATE TABLE codes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                code           TEXT UNIQUE NOT NULL,
                study_id       INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
                batch_id       INTEGER REFERENCES batches (id) ON DELETE CASCADE,
                stratum        TEXT NOT NULL,
                group_name     TEXT NOT NULL,
                issued_at      TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'voided')),
                replaced_by_id INTEGER REFERENCES codes (id)
            )
            """
        )
        db.execute(
            """
            INSERT INTO codes (id, code, study_id, batch_id, stratum, group_name,
                               issued_at, status, replaced_by_id)
            SELECT id, code, study_id, batch_id, stratum, group_name,
                   issued_at, COALESCE(status, 'active'), replaced_by_id
            FROM codes_old
            """
        )
        # 重建 replacements（若已存在），使其外键重新指向新的 codes 表
        repl_tables = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='replacements'"
        ).fetchall()
        if repl_tables:
            db.execute("ALTER TABLE replacements RENAME TO replacements_old")
            db.execute(
                """
                CREATE TABLE replacements (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    study_id     INTEGER NOT NULL REFERENCES studies (id) ON DELETE CASCADE,
                    request_id   TEXT NOT NULL,
                    old_code_id  INTEGER NOT NULL REFERENCES codes (id),
                    old_code     TEXT NOT NULL,
                    new_code_id  INTEGER NOT NULL REFERENCES codes (id),
                    new_code     TEXT NOT NULL,
                    stratum      TEXT NOT NULL,
                    group_name   TEXT NOT NULL,
                    reason       TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    UNIQUE (study_id, request_id)
                )
                """
            )
            db.execute(
                """
                INSERT INTO replacements (id, study_id, request_id, old_code_id, old_code,
                                          new_code_id, new_code, stratum, group_name, reason,
                                          requested_by, created_at)
                SELECT id, study_id, request_id, old_code_id, old_code, new_code_id, new_code,
                       stratum, group_name, reason, requested_by, created_at
                FROM replacements_old
                """
            )
            db.execute("DROP TABLE replacements_old")
        db.execute("DROP TABLE codes_old")
        db.execute("PRAGMA foreign_keys = ON")
    db.commit()


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
        _migrate(db)
        _seed_users(db)
