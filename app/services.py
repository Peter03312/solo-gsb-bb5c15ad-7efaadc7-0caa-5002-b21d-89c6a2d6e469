"""核心业务逻辑：盲法编码生成与揭盲。

设计要点：
- 样品码由 (研究种子, 研究名称, 分层, 处理组, 数量) 确定性生成，本身不含组别含义；
- 样品码全局唯一（数据库唯一约束兜底），一经发放不可复用；
- 生成前先做平衡性/缺失校验与冲突预检，任一失败整批拒绝、不落库；
- 揭盲需两位不同操作者（账号+口令，且具备揭盲权限）及完整理由，
  全部条件满足才在单个事务内写入揭盲事件（含映射快照）并更新研究状态。
"""

import json
import random
import secrets
import sqlite3
from datetime import datetime, timezone

from .auth import authenticate

CODE_PREFIX = "SMP-"
CODE_LENGTH = 8
# 去掉易混淆字符（0/O、1/I/L）
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

UNBLIND_ROLES = ("admin", "operator")


class ServiceError(Exception):
    """业务校验失败：errors 为可展示的原因列表。"""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class ConflictError(ServiceError):
    """本次生成会复用已发放样品码：整批拒绝并列出冲突。"""

    def __init__(self, conflicts):
        self.conflicts = list(conflicts)
        super().__init__(
            [f"本次生成将复用 {len(self.conflicts)} 个已发放的样品码，整批已拒绝"]
        )


class NotFoundError(ServiceError):
    """请求目标不存在。"""

    def __init__(self, message):
        super().__init__([message])


class IdempotencyConflict(ServiceError):
    """同一请求标识被用于不同参数，不能按重试处理。"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 输入解析

def parse_groups(text):
    """逗号（中英文均可）分隔的处理组列表，去重保序。"""
    groups, seen = [], set()
    for part in (text or "").replace("，", ",").split(","):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            groups.append(name)
    return groups


def parse_strata(text):
    """解析“每行一条：分层名=数量”。数量留空记为 None（生成时按缺少样品拒绝）。"""
    strata, errors = [], []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if "=" not in line:
            errors.append(f"第 {lineno} 行格式错误（应为「名称=数量」）：{line}")
            continue
        name, _, count = line.partition("=")
        name, count = name.strip(), count.strip()
        if not name:
            errors.append(f"第 {lineno} 行缺少分层名称")
            continue
        if count == "":
            strata.append({"name": name, "count": None})
        elif count.isdigit():
            strata.append({"name": name, "count": int(count)})
        else:
            errors.append(f"分层「{name}」的数量不是非负整数：{count}")
    names = [s["name"] for s in strata]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        errors.append("分层名称重复：" + "、".join(dups))
    return strata, errors


def normalize_strata(items):
    """API(JSON) 形式的分层：[{"name": ..., "count": ...}]，count 可为 null。"""
    strata = []
    for item in items or []:
        name = str((item or {}).get("name", "")).strip()
        count = (item or {}).get("count", None)
        if count is not None:
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = None
        strata.append({"name": name, "count": count})
    return strata


# ---------------------------------------------------------------- 校验

def validate_plan(strata, groups):
    """平衡性与缺失校验：返回错误列表（空表示通过）。"""
    errors = []
    if len(groups) < 2:
        errors.append("至少需要两个处理组")
    if not strata:
        errors.append("至少需要一个分层")
    n = len(groups)
    for s in strata:
        count = s["count"]
        if count is None:
            errors.append(f"分层「{s['name']}」缺少样品数量")
        elif count <= 0:
            errors.append(f"分层「{s['name']}」样品数量为 0，缺少样品")
        elif n >= 2 and count % n != 0:
            errors.append(
                f"分层「{s['name']}」数量 {count} 无法在 {n} 个处理组间平衡分配"
            )
    return errors


# ---------------------------------------------------------------- 编码生成

def build_plan(seed, study_name, strata, groups):
    """确定性生成 [(code, (stratum, group)), ...]。

    同一种子+研究配置必然得到相同结果；因此重复生成会命中已发放编码，
    由冲突预检整批拒绝（编码一经发放不可复用）。
    """
    cells = []
    per_group = None
    for s in strata:
        per_group = s["count"] // len(groups)
        for g in groups:
            cells.extend([(s["name"], g)] * per_group)

    rng = random.Random(f"{seed}|{study_name}")
    rng.shuffle(cells)  # 打乱分层/组别顺序，使编码顺序不泄露分组

    codes, seen = [], set()
    while len(codes) < len(cells):
        code = CODE_PREFIX + "".join(rng.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return list(zip(codes, cells))


def _find_conflicts(db, codes):
    conflicts = []
    for i in range(0, len(codes), 500):  # 分批避免超出 SQLite 变量上限
        chunk = codes[i : i + 500]
        marks = ",".join("?" * len(chunk))
        rows = db.execute(
            f"SELECT code FROM codes WHERE code IN ({marks})", chunk
        ).fetchall()
        conflicts.extend(r["code"] for r in rows)
    return conflicts


def _generate_unique_code(db):
    """生成不与任何已发放（含已作废）样品码重复的新码。"""
    while True:
        code = CODE_PREFIX + "".join(
            secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH)
        )
        exists = db.execute("SELECT 1 FROM codes WHERE code = ?", (code,)).fetchone()
        if exists is None:
            return code


def create_study(db, name, seed, groups, strata, username):
    errors = []
    if not name:
        errors.append("研究名称不能为空")
    if len(groups) < 2:
        errors.append("至少需要两个处理组")
    if not strata:
        errors.append("至少需要一个分层")
    if any(not s["name"] for s in strata):
        errors.append("分层名称不能为空")
    if errors:
        raise ServiceError(errors)
    try:
        cur = db.execute(
            "INSERT INTO studies (name, seed, created_by, created_at) VALUES (?, ?, ?, ?)",
            (name, seed, username, utcnow()),
        )
        study_id = cur.lastrowid
        db.executemany(
            "INSERT INTO treatment_groups (study_id, name) VALUES (?, ?)",
            [(study_id, g) for g in groups],
        )
        db.executemany(
            "INSERT INTO strata (study_id, name, expected) VALUES (?, ?, ?)",
            [(study_id, s["name"], s["count"]) for s in strata],
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        raise ServiceError([f"研究名称「{name}」已存在，或分层/处理组名称重复"])
    return study_id


def generate_batch(db, study, username):
    """生成一批样品码。校验失败或冲突时整批拒绝，不写入任何数据。"""
    strata = [
        {"name": r["name"], "count": r["expected"]}
        for r in db.execute(
            "SELECT name, expected FROM strata WHERE study_id = ? ORDER BY id",
            (study["id"],),
        ).fetchall()
    ]
    groups = [
        r["name"]
        for r in db.execute(
            "SELECT name FROM treatment_groups WHERE study_id = ? ORDER BY id",
            (study["id"],),
        ).fetchall()
    ]

    errors = validate_plan(strata, groups)
    if errors:
        raise ServiceError(errors)

    plan = build_plan(study["seed"], study["name"], strata, groups)
    conflicts = _find_conflicts(db, [code for code, _ in plan])
    if conflicts:
        raise ConflictError(conflicts)

    now = utcnow()
    cur = db.execute(
        "INSERT INTO batches (study_id, created_by, created_at, code_count) VALUES (?, ?, ?, ?)",
        (study["id"], username, now, len(plan)),
    )
    batch_id = cur.lastrowid
    db.executemany(
        "INSERT INTO codes (code, study_id, batch_id, stratum, group_name, issued_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (code, study["id"], batch_id, cell[0], cell[1], now)
            for code, cell in plan
        ],
    )
    db.commit()
    return batch_id, len(plan)


# ---------------------------------------------------------------- 补发

def _replacement_payload(row):
    return {
        "id": row["id"],
        "request_id": row["request_id"],
        "original_code": row["original_code"],
        "replacement_code": row["replacement_code"],
        "reason": row["reason"],
        "requested_by": row["requested_by"],
        "created_at": row["created_at"],
    }


def replace_code(db, study, original_code, reason, request_id, username):
    """凭有效样品码补发；返回 (请求记录, created)。

    - (study_id, request_id) 为幂等键：相同参数重试返回原结果，参数变化拒绝；
    - (study_id, original_code) 保证同一有效码的并发补发只能成功一次；
    - BEGIN IMMEDIATE 将幂等检查、原码作废和新码写入放在同一写事务中。
    """
    original_code = str(original_code).strip() if original_code is not None else ""
    reason = str(reason).strip() if reason is not None else ""
    request_id = str(request_id).strip() if request_id is not None else ""

    errors = []
    if not original_code:
        errors.append("原样品码不能为空")
    if not reason:
        errors.append("补发原因不能为空")
    if not request_id:
        errors.append("请求标识不能为空")
    if errors:
        raise ServiceError(errors)

    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        study = db.execute(
            "SELECT * FROM studies WHERE id = ?", (study["id"],)
        ).fetchone()
        existing = db.execute(
            "SELECT * FROM replacement_requests"
            " WHERE study_id = ? AND request_id = ?",
            (study["id"], request_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["original_code"] != original_code
                or existing["reason"] != reason
            ):
                raise IdempotencyConflict(
                    ["同一请求标识已用于其他样品码或补发原因，请求已拒绝"]
                )
            db.commit()
            return _replacement_payload(existing), False

        if study["status"] == "unblinded":
            raise ServiceError(["研究已揭盲，不能办理新的补发"])

        original = db.execute(
            "SELECT * FROM codes WHERE study_id = ? AND code = ?",
            (study["id"], original_code),
        ).fetchone()
        if original is None:
            raise NotFoundError("原样品码不存在")
        if original["status"] != "active":
            raise ServiceError(["原样品码已作废，不能再次补发"])

        now = utcnow()
        new_code = _generate_unique_code(db)
        cur = db.execute(
            "INSERT INTO codes (code, study_id, batch_id, stratum, group_name,"
            " issued_at, status, replaces_id, replaced_by_id)"
            " VALUES (?, ?, NULL, ?, ?, ?, 'active', ?, NULL)",
            (
                new_code,
                study["id"],
                original["stratum"],
                original["group_name"],
                now,
                original["id"],
            ),
        )
        new_id = cur.lastrowid
        db.execute(
            "UPDATE codes SET status = 'voided', replaced_by_id = ? WHERE id = ?",
            (new_id, original["id"]),
        )
        try:
            req_cur = db.execute(
                "INSERT INTO replacement_requests"
                " (study_id, request_id, original_code, replacement_code, reason,"
                "  requested_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    study["id"],
                    request_id,
                    original_code,
                    new_code,
                    reason,
                    username,
                    now,
                ),
            )
            db.commit()
        except sqlite3.IntegrityError:
            # 并发下另一个请求可能已取得同一 request_id 或原码；
            # 重新读取，让幂等规则或“已作废”规则给出稳定响应。
            db.rollback()
            existing = db.execute(
                "SELECT * FROM replacement_requests"
                " WHERE study_id = ? AND request_id = ?",
                (study["id"], request_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["original_code"] != original_code
                    or existing["reason"] != reason
                ):
                    raise IdempotencyConflict(
                        ["同一请求标识已用于其他样品码或补发原因，请求已拒绝"]
                    )
                return _replacement_payload(existing), False
            raise ServiceError(["原样品码已作废，不能再次补发"])
    except sqlite3.IntegrityError:
        # 并发下另一个请求可能已取得同一 request_id、原码，或占用同一新码。
        # 重新读取，让幂等规则或“已作废”规则给出稳定响应。
        db.rollback()
        existing = db.execute(
            "SELECT * FROM replacement_requests"
            " WHERE study_id = ? AND request_id = ?",
            (study["id"], request_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["original_code"] != original_code
                or existing["reason"] != reason
            ):
                raise IdempotencyConflict(
                    ["同一请求标识已用于其他样品码或补发原因，请求已拒绝"]
                )
            return _replacement_payload(existing), False
        raise ServiceError(["原样品码已作废，不能再次补发"])
    except Exception:
        db.rollback()
        raise

    row = db.execute(
        "SELECT * FROM replacement_requests WHERE id = ?", (req_cur.lastrowid,)
    ).fetchone()
    return _replacement_payload(row), True


# ---------------------------------------------------------------- 映射与揭盲

def get_mapping(db, study_id):
    return [
        dict(r)
        for r in db.execute(
            "SELECT c.code, c.stratum, c.group_name, c.batch_id, c.issued_at,"
            "       c.status, oc.code AS replaces_code, nc.code AS replaced_by_code"
            " FROM codes c"
            " LEFT JOIN codes oc ON oc.id = c.replaces_id"
            " LEFT JOIN codes nc ON nc.id = c.replaced_by_id"
            " WHERE c.study_id = ? ORDER BY c.code",
            (study_id,),
        ).fetchall()
    ]


def get_replacement_chain(db, study_id):
    """按时间返回补发记录；旧码/新码均可凭其中记录还原完整链。"""
    return [
        dict(r)
        for r in db.execute(
            "SELECT request_id, original_code, replacement_code, reason,"
            "       requested_by, created_at"
            " FROM replacement_requests WHERE study_id = ? ORDER BY id",
            (study_id,),
        ).fetchall()
    ]


def can_view_mapping(study, user):
    """映射仅授权可读：管理员随时可读；揭盲后对所有登录用户公开。"""
    return user["role"] == "admin" or study["status"] == "unblinded"


def unblind_study(db, study, op1, pw1, op2, pw2, reason, min_reason_len):
    """双人揭盲。任一条件不满足：抛出 ServiceError（只含原因），不改变任何数据。"""
    errors = []
    if study["status"] == "unblinded":
        errors.append("该研究已揭盲，不能重复揭盲")

    reason = (reason or "").strip()
    if len(reason) < min_reason_len:
        errors.append(f"揭盲理由不完整：至少需要 {min_reason_len} 个字符")

    op1, op2 = (op1 or "").strip(), (op2 or "").strip()
    if not op1 or not op2:
        errors.append("必须提供两位操作者")
    elif op1 == op2:
        errors.append("两位操作者必须是不同账号")
    if errors:
        raise ServiceError(errors)

    u1 = authenticate(op1, pw1)
    if u1 is None:
        errors.append(f"操作者一（{op1}）身份验证失败")
    u2 = authenticate(op2, pw2)
    if u2 is None:
        errors.append(f"操作者二（{op2}）身份验证失败")
    for u in (u1, u2):
        if u is not None and u["role"] not in UNBLIND_ROLES:
            errors.append(f"用户「{u['username']}」没有揭盲权限")

    rows = db.execute(
        "SELECT code, stratum, group_name, status, replaces_id, replaced_by_id"
        " FROM codes WHERE study_id = ? ORDER BY code",
        (study["id"],),
    ).fetchall()
    if not rows:
        errors.append("该研究尚未发放样品码，无法揭盲")
    if errors:
        raise ServiceError(errors)

    now = utcnow()
    snapshot = {
        "study_id": study["id"],
        "study_name": study["name"],
        "seed": study["seed"],
        "unblinded_at": now,
        "operators": [u1["username"], u2["username"]],
        "reason": reason,
        "mapping": [dict(r) for r in rows],
        "replacements": get_replacement_chain(db, study["id"]),
    }
    cur = db.execute(
        "INSERT INTO unblinding_events (study_id, operator1, operator2, reason, snapshot, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            study["id"],
            u1["username"],
            u2["username"],
            reason,
            json.dumps(snapshot, ensure_ascii=False),
            now,
        ),
    )
    db.execute("UPDATE studies SET status = 'unblinded' WHERE id = ?", (study["id"],))
    db.commit()
    return cur.lastrowid
