"""설정 저장/불러오기 (%APPDATA%\\gendisk-sync\\config.json)."""
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field

from . import secret

_save_lock = threading.Lock()


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "gendisk-sync")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


@dataclass
class Config:
    server_url: str = ""
    username: str = ""
    token: str = ""                 # 세션 토큰
    password_enc: str = ""          # DPAPI로 암호화된 비밀번호 (평문 저장 안 함)
    space: str = "home"
    local_folder: str = ""
    interval_sec: int = 30
    enabled: bool = False           # 자동 동기화 활성 여부
    # 시작 동작
    save_credentials: bool = False  # 로그인 정보(비밀번호) 저장
    auto_start: bool = False        # Windows 시작 시 자동 실행
    auto_login: bool = False        # 프로그램 시작 시 자동 로그인
    auto_connect_drive: bool = False  # 자동 로그인 후 드라이브 자동 연결
    drive_letter: str = "N:"
    appearance: str = "system"        # 화면 테마: light / dark / system(자동)
    # genDISK Drive (Windows Cloud Files 온디맨드 가상 드라이브)
    vfs_enabled: bool = False         # 온디맨드 드라이브 사용
    vfs_root: str = ""                # 싱크루트 경로 (빈 값이면 %USERPROFILE%\genDISK)
    # 드라이브 자체는 SMB처럼 동작한다(폴더를 열 때마다 서버 최신 목록).
    # 이 옵션은 '백그라운드 자동 반영'(SSE 실시간 + 주기 폴링)을 추가로 켤지 여부다.
    vfs_sync: bool = True
    # 무저장(SMB식): 파일 데이터를 컴퓨터에 남기지 않는다 — 업로드한 드롭 파일은 즉시,
    # 열람한 파일은 잠시 후 온라인 전용으로 되돌린다('항상 이 장치에 유지' 고정은 예외).
    vfs_free_space: bool = True
    # 드라이브 파일 전송 방식: "api"(HTTPS, 기본) / "ftp"(서버 내장 FTP — GENDISK_FTP_PORT).
    # 실시간 반영(SSE)·delta 는 FTP 모드에서도 HTTPS API 를 계속 쓴다(하이브리드).
    vfs_transport: str = "api"
    # FTP 접속 정보. Cloudflare 는 FTP 를 프록시하지 않으므로 호스트는 원본 서버
    # 주소(IP/DNS-only)여야 한다. 비우면 서버 주소의 호스트를 그대로 쓴다.
    ftp_host: str = ""
    ftp_port: int = 2121
    ftp_tls: bool = False
    # FTP 드라이브(rclone+WinFsp): 서버를 실제 드라이브 문자로 마운트한다.
    # 로컬에 목록·파일을 두지 않고 탐색기가 서버를 직접 본다(온디맨드 대체).
    ftp_drive_enabled: bool = True
    # 탐색기 미리 보기·썸네일 끄기: 탐색기가 썸네일을 만들려고 폴더 안 파일을
    # 통째로 내려받아 서버가 느려지는 것을 막는다(끄면 트래픽 자체가 없음).
    ftp_no_preview: bool = True
    # 마운트 지점: 비우면 %USERPROFILE%\genDISK. 드라이브 문자를 만들지 않고
    # 탐색기 사이드바의 'genDISK Drive' 항목이 이 폴더를 가리킨다.
    ftp_mount_point: str = ""

    def ftp_host_resolved(self) -> str:
        """접속할 FTP 호스트. ftp_host 가 비어 있으면 server_url(ftp://host:port)에서 뽑는다.
        설정 화면을 한 번도 안 연 상태에서 _collect() 가 빈 입력칸으로 ftp_host 를 덮어써
        재연결이 통째로 실패하던 문제가 있었다."""
        if self.ftp_host:
            return self.ftp_host
        import urllib.parse
        return urllib.parse.urlsplit(self.server_url).hostname or ""

    def ftp_mount_path(self) -> str:
        import os
        return self.ftp_mount_point or os.path.expandvars(r"%USERPROFILE%\genDISK")
    # 일반(범용) WebDAV 서버 연결 목록. 각 항목은 dict:
    #   {name, url, username, password_enc(DPAPI), drive, auto(bool)}
    # genDISK 서버 마운트와 별개로, 임의 WebDAV 서버(NAS/Nextcloud 등)를 드라이브로 연결.
    webdav_mounts: list = field(default_factory=list)

    def vfs_root_path(self) -> str:
        import os
        return self.vfs_root or os.path.expandvars(r"%USERPROFILE%\genDISK")

    @classmethod
    def load(cls) -> "Config":
        try:
            with open(config_path(), encoding="utf-8") as f:
                data = json.load(f)
            known = {k: data[k] for k in asdict(cls()) if k in data}
            return cls(**known)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self):
        # 원자적 쓰기(임시파일 + os.replace) + 잠금 — 동시 저장·중단 시 설정 손상 방지
        with _save_lock:
            d = config_dir()
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".cfg-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(asdict(self), f, ensure_ascii=False, indent=2)
                os.replace(tmp, config_path())
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def is_ready(self) -> bool:
        return bool(self.server_url and self.token and self.local_folder)

    def is_ftp_session(self) -> bool:
        """로그인 화면에서 FTP 주소로 접속한 세션 — HTTPS API 없이 FTP 만으로 동작.
        (드라이브는 FTP 전송, 동기화·WebDAV 등 API 전용 기능은 비활성)"""
        return self.server_url.startswith("ftp://")

    # ---------- 비밀번호 (DPAPI) ----------
    def set_password(self, password: str):
        enc = secret.encrypt(password) if password else None
        self.password_enc = enc or ""

    def get_password(self) -> str:
        return secret.decrypt(self.password_enc) or "" if self.password_enc else ""

    def clear_password(self):
        self.password_enc = ""
