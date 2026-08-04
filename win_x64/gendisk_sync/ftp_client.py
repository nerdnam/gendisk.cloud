"""genDISK Drive 용 FTP 전송 클라이언트.

서버의 내장 FTP(GENDISK_FTP_PORT)와 통신하며, drive.py 가 쓰는 GenDiskClient 의
파일 입출력 메서드(spaces/list_dir/download_range/put_smart/delete/move/mkdir)와
같은 시그니처를 구현한다 → DriveController 가 전송만 바꿔 끼울 수 있다.

FTP 가상 루트의 폴더 이름이 곧 저장소 id 다("home" + 마운트 이름) — 서버
app/ftp.py 와 약속된 구조. 경로는 "/<space>/<path>".

주의: Cloudflare 는 FTP 를 프록시하지 않으므로 호스트는 원본 서버 주소
(IP/DNS-only 호스트)여야 한다. ftplib 는 인스턴스 단위로 스레드 안전하지
않아서, 작은 연결 풀에서 하나씩 빌려 쓰고 반납한다.
"""
import ftplib
import os
import socket
import threading
from datetime import datetime, timezone

from .client import AuthError

_BLOCK = 64 * 1024


class FtpDriveClient:
    def __init__(self, host: str, port: int, username: str, password: str,
                 tls: bool = False, timeout: int = 12, pool_size: int = 6):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.tls = tls
        self.timeout = timeout
        self._pool: list[ftplib.FTP] = []
        self._lock = threading.Lock()
        self._pool_size = pool_size

    # ---------- 연결 풀 ----------
    def _new_conn(self) -> ftplib.FTP:
        ftp = ftplib.FTP_TLS() if self.tls else ftplib.FTP()
        ftp.encoding = "utf-8"
        try:
            ftp.connect(self.host, self.port, timeout=self.timeout)
            ftp.login(self.username, self.password)
            if self.tls:
                ftp.prot_p()                      # 데이터 채널도 암호화
        except ftplib.error_perm as e:
            self._close_quiet(ftp)
            if str(e).startswith("530"):
                raise AuthError("FTP 로그인 실패 — 아이디/비밀번호를 확인하세요")
            raise
        except (OSError, EOFError) as e:
            self._close_quiet(ftp)
            raise OSError(f"FTP 서버({self.host}:{self.port})에 연결할 수 없습니다: {e}")
        return ftp

    @staticmethod
    def _close_quiet(ftp):
        try:
            ftp.close()
        except Exception:
            pass

    def _acquire(self) -> ftplib.FTP:
        with self._lock:
            while self._pool:
                ftp = self._pool.pop()
                try:
                    ftp.voidcmd("NOOP")           # 유휴 타임아웃으로 죽은 연결 걸러내기
                    return ftp
                except Exception:
                    self._close_quiet(ftp)
        return self._new_conn()

    def _release(self, ftp: ftplib.FTP, dirty: bool = False):
        if dirty:
            self._close_quiet(ftp)
            return
        with self._lock:
            if len(self._pool) < self._pool_size:
                self._pool.append(ftp)
                return
        self._close_quiet(ftp)

    def close(self):
        with self._lock:
            for ftp in self._pool:
                self._close_quiet(ftp)
            self._pool.clear()

    def _run(self, fn):
        """풀에서 연결을 빌려 fn(ftp) 실행. 끊긴 연결이면 새 연결로 1회 재시도."""
        for attempt in (1, 2):
            ftp = self._acquire()
            try:
                out = fn(ftp)
            except _Truncated:
                self._release(ftp, dirty=True)    # 전송 중단 — 제어 연결 폐기, 데이터는 유효
                raise
            except ftplib.error_perm:
                self._release(ftp)                # 프로토콜은 정상 — 연결 재사용
                raise
            except (OSError, EOFError, ftplib.Error):
                self._release(ftp, dirty=True)
                if attempt == 2:
                    raise
                continue
            self._release(ftp)
            return out

    # ---------- 경로 ----------
    @staticmethod
    def _p(space: str, path: str = "") -> str:
        base = "/" + (space or "home")
        path = (path or "").strip("/")
        return f"{base}/{path}" if path else base

    # ---------- GenDiskClient 호환 메서드 ----------
    def spaces(self) -> list[dict]:
        def go(ftp):
            names = [n for n, f in ftp.mlsd("/") if f.get("type") == "dir"]
            out = []
            for n in sorted(names, key=lambda x: (x != "home", x.lower())):
                out.append({"id": n, "name": "내 파일" if n == "home" else n})
            return out
        return self._run(go)

    def list_dir(self, space: str, path: str = "") -> list[dict]:
        target = self._p(space, path)
        rel = (path or "").strip("/")

        def go(ftp):
            entries = []
            for name, facts in ftp.mlsd(target):
                t = facts.get("type", "")
                if t not in ("file", "dir"):
                    continue                      # cdir/pdir 등 메타 항목 제외
                is_dir = t == "dir"
                mtime = 0
                m = facts.get("modify", "")
                try:                              # YYYYMMDDHHMMSS(.sss) — UTC
                    mtime = int(datetime.strptime(m[:14], "%Y%m%d%H%M%S")
                                .replace(tzinfo=timezone.utc).timestamp())
                except ValueError:
                    pass
                entries.append({
                    "name": name,
                    "path": f"{rel}/{name}" if rel else name,
                    "is_dir": is_dir,
                    "size": 0 if is_dir else int(facts.get("size") or 0),
                    "mtime": mtime,
                    "kind": "folder" if is_dir else "file",
                    "etag": None,
                })
            return entries
        try:
            return self._run(go)
        except ftplib.error_perm as e:
            raise OSError(f"FTP 목록 실패({target}): {e}")

    def download_range(self, space: str, path: str, offset: int, length: int) -> bytes:
        target = self._p(space, path)

        def go(ftp):
            ftp.voidcmd("TYPE I")
            conn = ftp.transfercmd(f"RETR {target}", rest=offset or None)
            chunks = []
            got = 0
            complete = False                       # 서버가 전송을 스스로 끝냈는가
            try:
                while got < length:
                    b = conn.recv(min(_BLOCK, length - got))
                    if not b:
                        complete = True
                        break
                    chunks.append(b)
                    got += len(b)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            if complete:
                ftp.voidresp()                     # 226 — 제어 연결 재사용 가능
                return b"".join(chunks)
            # 중간에 끊었음 → 제어 연결 상태를 신뢰할 수 없으니 버린다
            raise _Truncated(b"".join(chunks))

        try:
            return self._run(go)
        except _Truncated as t:
            return t.data
        except ftplib.error_perm as e:
            raise OSError(f"FTP 다운로드 실패({target}): {e}")

    def put_smart(self, space: str, path: str, local_path, progress=None) -> None:
        target = self._p(space, path)
        total = os.path.getsize(local_path)
        done = 0

        def go(ftp):
            nonlocal done
            done = 0
            ftp.voidcmd("TYPE I")
            with open(local_path, "rb") as f:
                def cb(block):
                    nonlocal done
                    done += len(block)
                    if progress:
                        progress(done, total)
                ftp.storbinary(f"STOR {target}", f, blocksize=_BLOCK, callback=cb)
        try:
            self._run(go)
        except ftplib.error_perm as e:
            raise OSError(f"FTP 업로드 실패({target}): {e}")

    def delete(self, space: str, path: str) -> None:
        target = self._p(space, path)

        def go(ftp):
            self._delete_recursive(ftp, target)
        try:
            self._run(go)
        except ftplib.error_perm as e:
            raise OSError(f"FTP 삭제 실패({target}): {e}")

    def _delete_recursive(self, ftp, target: str):
        """파일이면 DELE, 폴더면 (비어있지 않아도) 내용까지 지운다 — API delete 와 동일 의미.
        대상이 이미 없으면 조용히 통과한다(API delete 의 404 무시와 동일)."""
        try:
            ftp.delete(target)
            return
        except ftplib.error_perm:
            pass                                   # 파일이 아님(폴더거나 없음) → 아래로
        try:
            entries = list(ftp.mlsd(target))
        except ftplib.error_perm:
            return                                 # 폴더도 아님 = 이미 없음
        for name, facts in entries:
            t = facts.get("type", "")
            if t == "dir":
                self._delete_recursive(ftp, f"{target}/{name}")
            elif t == "file":
                ftp.delete(f"{target}/{name}")
        ftp.rmd(target)

    def move(self, space: str, src: str, dst: str,
             src_space: str | None = None, dst_space: str | None = None) -> dict:
        f = self._p(src_space or space, src)
        t = self._p(dst_space or space, dst)

        def go(ftp):
            ftp.rename(f, t)
            return {}
        try:
            return self._run(go)
        except ftplib.error_perm as e:
            raise OSError(f"FTP 이동 실패({f} → {t}): {e}")

    def mkdir(self, space: str, path: str) -> None:
        target = self._p(space, path)

        def go(ftp):
            try:
                ftp.mkd(target)
            except ftplib.error_perm as e:
                if not str(e).startswith("550"):   # 이미 존재 등 — API mkdir(409 무시)와 동일
                    raise
        self._run(go)


class _Truncated(Exception):
    """범위 다운로드에서 필요한 만큼 읽고 전송을 끊었음을 나타내는 내부 신호."""
    def __init__(self, data: bytes):
        super().__init__("truncated")
        self.data = data
