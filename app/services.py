"""核心业务逻辑：盲法编码生成、补发与揭盲。

设计要点：
- 样品码由 (研究种子, 研究名称, 分层, 处理组, 数量) 确定性生成，本身不含组别含义；
- 样品码全局唯一（数据库唯一约束兜底），一经发放不可复用；
- 生成前先做平衡性/缺失校验与冲突预检，任一失败整批拒绝、不落库；
- 样品损毁可由管理员补发：原码立即作废（保留历史），新码继承原分层与处理组；
  补发以“研究内请求标识”幂等，相同标识重试返回原结果，改参数则拒绝；
  并发补发同一有效码仅能成功一次（BEGIN IMMEDIATE 串行化）；
- 揭盲需两位不同操作者（账号+口令，且具备揭盲权限）及完整理由，
  全部条件满足才在单个事务内写入揭盲事件（含完整映射与补发链快照）并更新研究状态。
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


class ReplacementConflictError(ServiceError):
    """补发请求与既有请求冲突（请求标识已被同研究的其它补发参数占用）。"""


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


# ---------------------------------------------------------------- 样品补发

def _new_replacement_code(db):
    """生成一个与任何已发放码（含已作废码）都不重复的补发码。

    补发是一次性事件，不沿用确定性种子（否则会与原批次码冲突），
    改用加密级随机源生成；codes.code 的全局唯一约束为最终防线。
    """
    for _ in range(20):
        code = CODE_PREFIX + "".join(
            secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH)
        )
        exists = db.execute("SELECT 1 FROM codes WHERE code = ?", (code,)).fetchone()
        if exists is None:
            return code
    raise ServiceError(["未能生成不冲突的补发码，请重试"])


def _replacement_record(db, repl):
    row = db.execute(
        "SELECT r.*, c.status AS new_code_status FROM replacements r"
        " JOIN codes c ON c.id = r.new_code_id WHERE r.id = ?",
        (repl["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "request_id": row["request_id"],
        "old_code": row["old_code"],
        "new_code": row["new_code"],
        "stratum": row["stratum"],
        "group_name": row["group_name"],
        "reason": row["reason"],
        "requested_by": row["requested_by"],
        "created_at": row["created_at"],
        "new_code_status": row["new_code_status"],
    }


def replace_code(db, study, old_code, reason, request_id, username):
    """样品损毁补发。

    返回 (record, replayed)：replayed=True 表示命中同请求标识的原结果。
    规则：
    - 原码须存在且当前有效；原因/请求标识不能为空；研究须仍处盲态；
    - 原码立即作废（保留历史与替换链），新码继承原分层与处理组；
    - 同研究相同 request_id 重试返回原记录；改换 old_code/reason 则拒绝；
    - 不同请求并发补发同一有效码只成功一次（BEGIN IMMEDIATE 串行化）。
    """
    old_code = (old_code or "").strip()
    reason = (reason or "").strip()
    request_id = (request_id or "").strip()

    # 幂等命中优先：即便原码后来作废、研究后来揭盲，重试仍返回原记录
    if request_id:
        existing = db.execute(
            "SELECT * FROM replacements WHERE study_id = ? AND request_id = ?",
            (study["id"], request_id),
        ).fetchone()
        if existing is not None:
            if existing["old_code"] != old_code or existing["reason"] != reason:
                raise ReplacementConflictError(
                    [
                        f"请求标识「{request_id}」已用于样品码 {existing['old_code']} 的补发"
                        "（原因亦须一致），不能改换样品码或原因"
                    ]
                )
            return _replacement_record(db, existing), True

    errors = []
    if not old_code:
        errors.append("原样品码不能为空")
    if not reason:
        errors.append("补发原因不能为空")
    if not request_id:
        errors.append("请求标识不能为空")
    if errors:
        raise ServiceError(errors)

    if study["status"] == "unblinded":
        raise ServiceError(["研究已揭盲，不能再办理补发"])

    old = db.execute(
        "SELECT * FROM codes WHERE study_id = ? AND code = ?",
        (study["id"], old_code),
    ).fetchone()
    if old is None:
        raise ServiceError([f"样品码 {old_code} 不存在"])
    if old["status"] != "active":
        replaced = db.execute(
            "SELECT code FROM codes WHERE id = ?", (old["replaced_by_id"],)
        ).fetchone()
        hint = f"，已由 {replaced['code']} 替换" if replaced else ""
        raise ServiceError([f"样品码 {old_code} 已作废{hint}，不能重复补发"])

    now = utcnow()

    # 立即取写锁：并发补发在此串行，失败者在锁内重新校验状态/幂等
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        recheck = db.execute(
            "SELECT * FROM replacements WHERE study_id = ? AND request_id = ?",
            (study["id"], request_id),
        ).fetchone()
        if recheck is not None:
            db.commit()
            if recheck["old_code"] != old_code or recheck["reason"] != reason:
                raise ReplacementConflictError(
                    [f"请求标识「{request_id}」已用于其它补发，参数不一致，已拒绝"]
                )
            return _replacement_record(db, recheck), True

        old = db.execute(
            "SELECT * FROM codes WHERE id = ?", (old["id"],)
        ).fetchone()
        if old is None:
            db.rollback()
            raise ServiceError([f"样品码 {old_code} 不存在"])
        if old["status"] != "active":
            db.rollback()
            raise ServiceError([f"样品码 {old_code} 已作废，不能重复补发"])

        new_code = _new_replacement_code(db)
        cur = db.execute(
            "INSERT INTO codes (code, study_id, batch_id, stratum, group_name,"
            " issued_at, status, replaced_by_id)"
            " VALUES (?, ?, NULL, ?, ?, ?, 'active', NULL)",
            (new_code, study["id"], old["stratum"], old["group_name"], now),
        )
        new_id = cur.lastrowid
        # 条件更新：只有仍有效时才作废，保证并发补发只成功一次
        cur = db.execute(
            "UPDATE codes SET status = 'voided', replaced_by_id = ?"
            " WHERE id = ? AND status = 'active'",
            (new_id, old["id"]),
        )
        if cur.rowcount != 1:
            db.rollback()
            raise ServiceError([f"样品码 {old_code} 已作废，不能重复补发"])

        repl_cur = db.execute(
            "INSERT INTO replacements (study_id, request_id, old_code_id, old_code,"
            " new_code_id, new_code, stratum, group_name, reason, requested_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                study["id"],
                request_id,
                old["id"],
                old["code"],
                new_id,
                new_code,
                old["stratum"],
                old["group_name"],
                reason,
                username,
                now,
            ),
        )
        db.commit()
    except ReplacementConflictError:
        raise
    except sqlite3.Error:
        db.rollback()
        raise
    repl = db.execute(
        "SELECT * FROM replacements WHERE id = ?", (repl_cur.lastrowid,)
    ).fetchone()
    return _replacement_record(db, repl), False


def get_replacements(db, study_id):
    """研究的完整补发链（按时间顺序）。"""
    return [
        dict(r)
        for r in db.execute(
            "SELECT id, study_id, request_id, old_code, new_code, stratum,"
            " group_name, reason, requested_by, created_at"
            " FROM replacements WHERE study_id = ? ORDER BY id",
            (study_id,),
        ).fetchall()
    ]


# ---------------------------------------------------------------- 映射与揭盲

def get_codes_with_chain(db, study_id):
    """样品码列表（含有效状态与替换关系），按发放顺序返回。"""
    return [
        dict(r)
        for r in db.execute(
            """
            SELECT c.id, c.code, c.stratum, c.group_name, c.batch_id, c.issued_at,
                   c.status,
                   nc.code AS replaced_by_code,
                   oc.code AS replacement_of_code
            FROM codes c
            LEFT JOIN codes nc ON nc.id = c.replaced_by_id
            LEFT JOIN replacements r ON r.new_code_id = c.id
            LEFT JOIN codes oc ON oc.id = r.old_code_id
            WHERE c.study_id = ?
            ORDER BY c.id
            """,
            (study_id,),
        ).fetchall()
    ]


def get_mapping(db, study_id):
    return get_codes_with_chain(db, study_id)


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
        """
        SELECT c.code, c.stratum, c.group_name, c.status,
               nc.code AS replaced_by_code,
               oc.code AS replacement_of_code
        FROM codes c
        LEFT JOIN codes nc ON nc.id = c.replaced_by_id
        LEFT JOIN replacements r ON r.new_code_id = c.id
        LEFT JOIN codes oc ON oc.id = r.old_code_id
        WHERE c.study_id = ?
        ORDER BY c.id
        """,
        (study["id"],),
    ).fetchall()
    if not rows:
        errors.append("该研究尚未发放样品码，无法揭盲")
    if errors:
        raise ServiceError(errors)

    replacements = get_replacements(db, study["id"])
    now = utcnow()
    snapshot = {
        "study_id": study["id"],
        "study_name": study["name"],
        "seed": study["seed"],
        "unblinded_at": now,
        "operators": [u1["username"], u2["username"]],
        "reason": reason,
        "mapping": [dict(r) for r in rows],
        # 完整补发链：原码/新码、分层、处理组、原因与请求标识全部留档
        "replacements": replacements,
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
