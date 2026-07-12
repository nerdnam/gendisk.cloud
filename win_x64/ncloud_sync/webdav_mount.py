"""Windows에서 WebDAV를 네트워크 드라이브로 연결/해제 (net use).

WebDAV URL을 UNC 형식(\\\\server@SSL@port\\dav)으로 바꿔 `net use`로 매핑한다.
평문 HTTP(https 아님)일 때는 Windows WebClient가 기본적으로 Basic 인증을 막으므로
HTTPS 사용을 권장한다.
"""
import subprocess
from urllib.parse import urlsplit


def _unc(server_url: str) -> str:
    u = urlsplit(server_url)
    host = u.hostname or ""
    port = u.port
    secure = u.scheme == "https"
    # \\host@SSL@443\dav  (https) / \\host@80\dav (http)
    at = "@SSL" if secure else ""
    if port and not (secure and port == 443) and not (not secure and port == 80):
        at += f"@{port}"
    elif secure and port == 443:
        at = "@SSL@443"
    return rf"\\{host}{at}\dav"


def connect_drive(drive: str, server_url: str, username: str, password: str):
    unc = _unc(server_url)
    drive = drive.rstrip("\\")
    # 기존 매핑이 있으면 먼저 해제 (오류 무시)
    subprocess.run(["net", "use", drive, "/delete", "/y"],
                   capture_output=True, text=True)
    result = subprocess.run(
        ["net", "use", drive, unc, password, f"/user:{username}", "/persistent:yes"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"연결 실패 (코드 {result.returncode}).\n{msg}\n\n"
            "HTTP(비-HTTPS) 서버라면 Windows가 Basic 인증을 막을 수 있습니다. "
            "HTTPS를 사용하거나 WebClient의 BasicAuthLevel 설정이 필요합니다."
        )
    return unc


def disconnect_drive(drive: str):
    drive = drive.rstrip("\\")
    result = subprocess.run(["net", "use", drive, "/delete", "/y"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"해제 실패: {msg}")
