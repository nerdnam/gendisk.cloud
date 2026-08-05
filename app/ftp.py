"""FTP(S) 서버 — genDISK 저장소를 표준 FTP 클라이언트로 노출 (pyftpdlib).

GENDISK_FTP_PORT 환경변수를 설정하면 켜진다(기본 꺼짐). 웹/WebDAV와 동일한
계정·권한 모델을 쓴다: 로그인하면 가상 루트에 `home`(내 파일)과 접근 권한이
있는 외부 마운트들이 폴더로 나타난다. 읽기 전용 마운트에는 쓰기가 거부된다.

환경변수:
  GENDISK_FTP_PORT           예: 2121 — 설정해야 켜짐
  GENDISK_FTP_PASSIVE_PORTS  패시브 데이터 포트 범위 (기본 "60000-60019")
  GENDISK_FTP_MASQUERADE     NAT 뒤일 때 패시브 응답에 실을 외부 IP/호스트
  GENDISK_FTP_TLS_CERT/KEY   둘 다 설정하면 FTPS(explicit TLS) — pyopenssl 필요

주의: Cloudflare는 FTP를 프록시하지 않으므로 이 포트들은 서버 IP로 직접
연결해야 한다(포트포워딩/VPN). 평문 FTP는 자격증명이 그대로 노출되므로
인터넷에 여는 경우 TLS(FTPS) 사용을 권장한다.
"""
import logging
import os
import threading

from . import auth
from .database import DATA_DIR
from .events import notify_change, parent_of
from .files import (
    HOME_SPACE,
    _writable,
    accessible_mounts,
    dir_size,
    user_quota,
    user_root,
)

log = logging.getLogger("gendisk.ftp")

# 가상 루트로 쓸 실제(빈) 디렉토리 — pyftpdlib 는 홈이 실존 경로여야 한다.
_VROOT = str(DATA_DIR / "ftproot")

_WRITE_PERMS = "adfmwMT"          # 쓰기 계열 권한 문자 (d=삭제 f=이름변경 m=폴더 w=업로드 …)


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _build_spaces(user: dict) -> dict[str, str]:
    """가상 루트에 보일 {이름: 실제경로}. WebDAV(/dav)와 같은 구성."""
    spaces = {HOME_SPACE: str(user_root(user).resolve())}
    for m in accessible_mounts(user):
        spaces[m.name] = str(m.resolve())
    return spaces


class GenDiskAuthorizer:
    """DB 계정으로 인증하고, 세션별 공간 목록·쓰기 권한을 판정한다.
    (pyftpdlib DummyAuthorizer 와 같은 인터페이스를 구현한 커스텀 authorizer)"""

    def __init__(self):
        self._sessions: dict[str, tuple[dict, dict[str, str]]] = {}  # username -> (user, spaces)

    # -- pyftpdlib 프로토콜 --
    def validate_authentication(self, username, password, handler):
        from pyftpdlib.authorizers import AuthenticationFailed

        user = auth.verify_credentials(username, password)
        if user is None:
            raise AuthenticationFailed("아이디 또는 비밀번호가 올바르지 않습니다")
        # 공간 목록은 로그인 시점에 확정(세션 동안 캐시) — 권한 변경은 재로그인 후 반영
        self._sessions[username] = (user, _build_spaces(user))

    def get_home_dir(self, username):
        return _VROOT

    def has_user(self, username):
        return username in self._sessions

    def has_perm(self, username, perm, path=None):
        if perm not in _WRITE_PERMS:
            return True
        entry = self._sessions.get(username)
        if entry is None or path is None:
            return False
        user, spaces = entry
        p = _norm(path)
        if p == _norm(_VROOT):
            return False                     # 가상 루트 직접 조작 금지
        for name, root in spaces.items():
            nroot = _norm(root)
            if p == nroot:
                return False                 # 공간 폴더 자체 삭제/개명 금지
            if p.startswith(nroot + os.sep):
                if name != HOME_SPACE and not _writable(root):
                    return False             # 읽기 전용 마운트
                if name == HOME_SPACE and perm in "wa":
                    quota = user_quota(user["id"])
                    if quota > 0 and dir_size(root) >= quota:
                        return False         # 용량 초과(개략 검사 — 업로드 시작 시점 기준)
                return True
        return False                         # 어떤 공간에도 속하지 않는 경로

    def get_perms(self, username):
        return "elr" + _WRITE_PERMS

    def get_msg_login(self, username):
        return "genDISK FTP — 로그인 성공"

    def get_msg_quit(self, username):
        return "안녕히 가세요"

    def impersonate_user(self, username, password):
        pass

    def terminate_impersonation(self, username):
        pass

    # -- 내부용 --
    def session(self, username):
        return self._sessions.get(username)


def _make_fs_class():
    """AbstractedFS 서브클래스 — 가상 루트('/') 아래에 공간들을 폴더로 매핑."""
    from pyftpdlib.filesystems import AbstractedFS

    class SpaceFS(AbstractedFS):
        def __init__(self, root, cmd_channel):
            super().__init__(root, cmd_channel)
            user, spaces = cmd_channel.authorizer.session(cmd_channel.username)
            self._user = user
            self._spaces = spaces

        # ---- 경로 매핑 ----
        def ftp2fs(self, ftppath):
            vpath = self.ftpnorm(ftppath)                 # 절대 가상 경로('..' 해소됨)
            parts = [p for p in vpath.split("/") if p]
            if not parts:
                return self._root                          # 가상 루트
            root = self._spaces.get(parts[0])
            if root is None:
                # 없는 공간 → 실존하지 않는 경로로 귀결시켜 자연스럽게 오류
                return os.path.normpath(os.path.join(self._root, *parts))
            return os.path.normpath(os.path.join(root, *parts[1:]))

        def fs2ftp(self, fspath):
            p = os.path.normpath(fspath)
            if _norm(p) == _norm(self._root):
                return "/"
            for name, root in self._spaces.items():
                if _norm(p) == _norm(root):
                    return "/" + name
                if _norm(p).startswith(_norm(root) + os.sep):
                    rel = p[len(root) + 1:].replace(os.sep, "/")
                    return f"/{name}/{rel}"
            return "/"

        def validpath(self, path):
            p = _norm(path)
            if p == _norm(self._root):
                return True
            for root in self._spaces.values():
                nroot = _norm(root)
                if p == nroot or p.startswith(nroot + os.sep):
                    return True
            return False

        # ---- 가상 루트 목록/속성 ----
        # 루트 목록을 만들 때 pyftpdlib 는 `<루트>/<이름>` 을 stat 하므로,
        # 그 합성 경로를 실제 공간 경로로 돌려준다.
        def _remap(self, path):
            parent, base = os.path.split(os.path.normpath(path))
            if _norm(parent) == _norm(self._root) and base in self._spaces:
                return self._spaces[base]
            return path

        def listdir(self, path):
            if _norm(os.path.normpath(path)) == _norm(self._root):
                return list(self._spaces.keys())
            return super().listdir(path)

        def stat(self, path):
            return super().stat(self._remap(path))

        def lstat(self, path):
            return super().lstat(self._remap(path))

        def islink(self, path):
            return super().islink(self._remap(path))

        # ---- 변경 이벤트 (웹/동기화 클라이언트 실시간 반영) ----
        def _notify(self, real, gone: bool = False):
            try:
                v = self.fs2ftp(real)                      # "/<space>/<rel>"
                parts = [p for p in v.split("/") if p]
                if not parts:
                    return
                space, rel = parts[0], "/".join(parts[1:])
                notify_change(space, parent_of(rel), self._user["id"],
                              private=(space == HOME_SPACE),
                              gone=rel if gone else None)
            except Exception:                              # 알림 실패가 FTP 동작을 막지 않게
                log.debug("notify failed", exc_info=True)

        def mkdir(self, path):
            super().mkdir(path)
            self._notify(path)

        def rmdir(self, path):
            super().rmdir(path)
            self._notify(path, gone=True)

        def remove(self, path):
            super().remove(path)
            self._notify(path, gone=True)

        def rename(self, src, dst):
            super().rename(src, dst)
            self._notify(src, gone=True)
            self._notify(dst)

    return SpaceFS


def _make_handler_class(tls_cert: str | None, tls_key: str | None):
    if tls_cert and tls_key:
        from pyftpdlib.handlers import TLS_FTPHandler as Base
    else:
        from pyftpdlib.handlers import FTPHandler as Base

    class Handler(Base):
        banner = "genDISK FTP ready."

        def on_file_received(self, file):                  # 업로드 완료(STOR/APPE)
            fs = getattr(self, "fs", None)
            if fs is not None:
                fs._notify(file)

    if tls_cert and tls_key:
        Handler.certfile = tls_cert
        Handler.keyfile = tls_key
        Handler.tls_control_required = True
        Handler.tls_data_required = True
    return Handler


def _parse_ports(spec: str) -> range | None:
    try:
        a, _, b = spec.partition("-")
        lo, hi = int(a), int(b or a)
        if 0 < lo <= hi < 65536:
            return range(lo, hi + 1)
    except ValueError:
        pass
    return None


def maybe_start() -> threading.Thread | None:
    """GENDISK_FTP_PORT 가 설정돼 있으면 FTP 서버를 데몬 스레드로 띄운다."""
    port = int(os.environ.get("GENDISK_FTP_PORT", "0") or 0)
    if not port:
        return None
    try:
        from pyftpdlib.servers import ThreadedFTPServer
    except ImportError:
        log.error("GENDISK_FTP_PORT 가 설정됐지만 pyftpdlib 가 없습니다 "
                  "(pip install pyftpdlib) — FTP 비활성")
        return None

    os.makedirs(_VROOT, exist_ok=True)
    tls_cert = os.environ.get("GENDISK_FTP_TLS_CERT") or None
    tls_key = os.environ.get("GENDISK_FTP_TLS_KEY") or None

    handler = _make_handler_class(tls_cert, tls_key)
    handler.authorizer = GenDiskAuthorizer()
    handler.abstracted_fs = _make_fs_class()
    passive = _parse_ports(os.environ.get("GENDISK_FTP_PASSIVE_PORTS", "60000-60019"))
    if passive:
        handler.passive_ports = passive
    masq = os.environ.get("GENDISK_FTP_MASQUERADE")
    if masq:
        handler.masquerade_address = masq

    server = ThreadedFTPServer(("0.0.0.0", port), handler)
    # 온디맨드 드라이브(연결 풀)·탐색기 열람 폭주 + NAT 뒤 여러 기기가 한 IP 로
    # 몰리는 상황을 고려해 넉넉히 잡는다 (기본 16 은 간헐 421 거부를 유발했음)
    server.max_cons = 256
    server.max_cons_per_ip = 64

    t = threading.Thread(target=server.serve_forever, name="gendisk-ftp", daemon=True)
    t.start()
    log.info("FTP%s 서버 시작: 포트 %d (패시브 %s)",
             "S" if tls_cert else "", port,
             f"{passive.start}-{passive.stop - 1}" if passive else "임의")
    return t
