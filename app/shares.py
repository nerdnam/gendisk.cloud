"""외부 공유 링크 (읽기 전용).

소유자는 로그인 상태에서 파일/폴더에 대한 공유 링크(/s/<token>)를 만들고, 링크만
아는 외부 사용자는 로그인 없이 그 안을 열람·다운로드할 수 있다. 링크에는 선택적으로
비밀번호와 만료일을 걸 수 있다.

보안 요점:
  * token 은 secrets.token_urlsafe(32) — 추측 불가.
  * 공개 열람 시에도 소유자의 신원으로 space 를 다시 확인하므로, 마운트 접근이
    회수되거나 소유자가 삭제되면 링크도 죽는다(404).
  * 경로는 항상 공유 루트 하위로 가둔다(디렉토리 트래버설 차단).
  * 절대 상위(공유 루트 밖)를 노출하지 않는다 — entry 의 path 는 공유 루트 기준 상대경로.
"""
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import files as files_mod
from .auth import current_user, hash_password
from .database import get_db

router = APIRouter(prefix="/api/shares", tags=["shares"])
public_router = APIRouter(prefix="/api/public/share", tags=["public-share"])

SHARE_COOKIE = "gd_share"
UNLOCK_HOURS = 12


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cookie_path(token: str) -> str:
    return f"/api/public/share/{token}"


def _base_name(rel_path: str, space: str) -> str:
    name = rel_path.rstrip("/").split("/")[-1]
    return name or space


# ---------- 공유 조회/검증 헬퍼 ----------

def _get_share(conn, token: str):
    return conn.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()


def _expired(share) -> bool:
    return bool(share["expires_at"]) and datetime.fromisoformat(share["expires_at"]) < _utcnow()


def _load_owner(conn, owner_id: int):
    row = conn.execute(
        "SELECT id, username, is_admin FROM users WHERE id = ?", (owner_id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}


def _share_root_path(share) -> Path:
    """공유 대상(파일 또는 폴더)의 실제 경로. 소유자 신원으로 space 를 다시 확인한다."""
    conn = get_db()
    try:
        owner = _load_owner(conn, share["owner_id"])
    finally:
        conn.close()
    if owner is None:
        raise HTTPException(404, "공유를 찾을 수 없습니다")
    # space_root 가 마운트 존재/소유자 접근권을 강제한다 (없거나 회수됐으면 404)
    base = files_mod.space_root(owner, share["space"])
    root = (base / share["path"].strip("/").replace("\\", "/")).resolve()
    # 재확인: 공유 루트는 반드시 space 루트 하위여야 한다
    if root != base and base not in root.parents:
        raise HTTPException(404, "공유를 찾을 수 없습니다")
    if not root.exists():
        raise HTTPException(404, "공유 대상이 더 이상 존재하지 않습니다")
    return root


def _resolve_within(root: Path, sub: str) -> Path:
    """공유 루트 하위로 가둔 경로 해석 (트래버설 차단)."""
    target = (root / sub.strip("/").replace("\\", "/")).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(400, "잘못된 경로입니다")
    return target


def _is_unlocked(conn, request: Request, token: str, share) -> bool:
    if not share["password_hash"]:
        return True
    access = request.cookies.get(SHARE_COOKIE)
    if not access:
        return False
    row = conn.execute(
        "SELECT expires_at FROM share_unlocks WHERE access_token = ? AND share_token = ?",
        (access, token),
    ).fetchone()
    if row is None:
        return False
    return datetime.fromisoformat(row["expires_at"]) >= _utcnow()


def _require_access(conn, request: Request, token: str):
    """유효한 공유인지(존재·미만료·필요시 언락) 확인하고 (share, root) 반환."""
    share = _get_share(conn, token)
    if share is None or _expired(share):
        raise HTTPException(404, "공유를 찾을 수 없습니다")
    if share["password_hash"] and not _is_unlocked(conn, request, token, share):
        raise HTTPException(401, "비밀번호가 필요합니다")
    root = _share_root_path(share)
    return share, root


def _touch(token: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE shares SET last_access_at = ? WHERE token = ?",
            (_utcnow().isoformat(), token),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- 소유자용 (로그인 필요) ----------

class ShareCreate(BaseModel):
    space: str = files_mod.HOME_SPACE
    path: str = Field(min_length=1)
    password: str | None = Field(default=None, max_length=256)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


@router.post("/create")
def create_share(body: ShareCreate, user: dict = Depends(current_user)):
    # safe_path/space_root 가 트래버설 + 소유자의 space 접근권을 강제한다
    root = files_mod.space_root(user, body.space)
    target = files_mod.safe_path(user, body.path, body.space)
    if target == root:
        raise HTTPException(400, "저장소 루트는 공유할 수 없습니다")
    if not target.exists():
        raise HTTPException(404, "대상을 찾을 수 없습니다")
    rel = target.relative_to(root).as_posix()
    is_dir = target.is_dir()

    token = secrets.token_urlsafe(32)
    password_hash = salt = None
    if body.password:
        salt = secrets.token_hex(16)
        password_hash = hash_password(body.password, salt)
    expires_at = None
    if body.expires_days:
        expires_at = (_utcnow() + timedelta(days=body.expires_days)).isoformat()

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO shares (token, owner_id, space, path, is_dir, password_hash, salt, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (token, user["id"], body.space, rel, 1 if is_dir else 0, password_hash, salt, expires_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "token": token,
        "path": f"/s/{token}",
        "name": target.name,
        "is_dir": is_dir,
        "protected": bool(password_hash),
        "expires_at": expires_at,
    }


@router.get("/list")
def list_shares(user: dict = Depends(current_user)):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT token, space, path, is_dir, (password_hash IS NOT NULL) AS protected, "
            "expires_at, created_at, last_access_at FROM shares "
            "WHERE owner_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    finally:
        conn.close()
    now = _utcnow()
    out = []
    for r in rows:
        expired = bool(r["expires_at"]) and datetime.fromisoformat(r["expires_at"]) < now
        out.append({
            "token": r["token"],
            "space": r["space"],
            "path": r["path"],
            "name": _base_name(r["path"], r["space"]),
            "is_dir": bool(r["is_dir"]),
            "protected": bool(r["protected"]),
            "expires_at": r["expires_at"],
            "expired": expired,
            "created_at": r["created_at"],
            "last_access_at": r["last_access_at"],
        })
    return {"shares": out}


class ShareToken(BaseModel):
    token: str = Field(min_length=1, max_length=200)


@router.post("/revoke")
def revoke_share(body: ShareToken, user: dict = Depends(current_user)):
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM shares WHERE token = ? AND owner_id = ?", (body.token, user["id"])
        )
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "공유를 찾을 수 없습니다")
    return {"ok": True}


# ---------- 공개 (로그인 불필요) ----------

class UnlockBody(BaseModel):
    password: str = Field(max_length=256)


@public_router.get("/{token}")
def share_meta(token: str, request: Request):
    conn = get_db()
    try:
        share = _get_share(conn, token)
        if share is None:
            raise HTTPException(404, "공유를 찾을 수 없습니다")
        if _expired(share):
            raise HTTPException(410, "만료된 공유입니다")
        protected = bool(share["password_hash"])
        unlocked = _is_unlocked(conn, request, token, share)
    finally:
        conn.close()
    resp = {"protected": protected, "unlocked": unlocked, "expires_at": share["expires_at"]}
    # 비밀번호가 걸려 있고 아직 안 풀렸으면 이름/종류조차 노출하지 않는다
    if unlocked or not protected:
        resp["name"] = _base_name(share["path"], share["space"])
        resp["is_dir"] = bool(share["is_dir"])
    return resp


@public_router.post("/{token}/unlock")
def share_unlock(token: str, body: UnlockBody, request: Request, response: Response):
    conn = get_db()
    try:
        share = _get_share(conn, token)
        if share is None or _expired(share):
            raise HTTPException(404, "공유를 찾을 수 없습니다")
        if not share["password_hash"]:
            return {"ok": True}  # 비밀번호 없는 공유 — 언락 불필요
        attempt = hash_password(body.password, share["salt"])
        if not secrets.compare_digest(share["password_hash"], attempt):
            raise HTTPException(401, "비밀번호가 올바르지 않습니다")
        access = secrets.token_urlsafe(32)
        exp = _utcnow() + timedelta(hours=UNLOCK_HOURS)
        if share["expires_at"]:
            exp = min(exp, datetime.fromisoformat(share["expires_at"]))
        conn.execute(
            "INSERT INTO share_unlocks (access_token, share_token, expires_at) VALUES (?, ?, ?)",
            (access, token, exp.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    response.set_cookie(
        SHARE_COOKIE,
        access,
        max_age=UNLOCK_HOURS * 3600,
        path=_cookie_path(token),
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@public_router.get("/{token}/list")
def share_list(token: str, request: Request, path: str = ""):
    conn = get_db()
    try:
        share, root = _require_access(conn, request, token)
    finally:
        conn.close()
    if not bool(share["is_dir"]):
        raise HTTPException(400, "폴더 공유가 아닙니다")
    target = _resolve_within(root, path)
    if not target.is_dir():
        raise HTTPException(404, "폴더를 찾을 수 없습니다")
    entries = []
    try:
        with os.scandir(target) as it:
            dirents = list(it)
    except OSError as exc:
        raise files_mod._fs_error(exc)
    for de in dirents:
        try:
            entries.append(files_mod.entry_info(Path(de.path), root))
        except OSError:
            continue  # stat 불가 항목은 조용히 건너뜀 (공개 페이지라 최소 노출)
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    _touch(token)
    return {
        "name": root.name,
        "path": target.relative_to(root).as_posix() if target != root else "",
        "entries": entries,
    }


def _served_file(share, root: Path, path: str, download: bool):
    # 파일 공유면 path 무시(대상은 root 자체), 폴더 공유면 하위 파일을 가리켜야 한다
    target = _resolve_within(root, path) if bool(share["is_dir"]) else root
    if not target.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    return files_mod._serve_file(target, download=download)


@public_router.get("/{token}/download")
def share_download(token: str, request: Request, path: str = ""):
    conn = get_db()
    try:
        share, root = _require_access(conn, request, token)
    finally:
        conn.close()
    resp = _served_file(share, root, path, download=True)
    _touch(token)
    return resp


@public_router.get("/{token}/raw")
def share_raw(token: str, request: Request, path: str = ""):
    conn = get_db()
    try:
        share, root = _require_access(conn, request, token)
    finally:
        conn.close()
    resp = _served_file(share, root, path, download=False)
    _touch(token)
    return resp
