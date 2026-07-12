"""Windows에서 WebDAV를 네트워크 드라이브로 연결/해제.

`net use`는 비밀번호가 명령줄에 노출(프로세스 목록·감사 로그)되므로, 자격증명을
프로세스 안에서만 전달하는 Win32 API WNetAddConnection2W(mpr.dll)를 ctypes로 호출한다.

WebDAV URL을 UNC(\\\\server@SSL@port\\dav)로 바꿔 매핑한다. 평문 HTTP일 때는
Windows WebClient가 기본적으로 Basic 인증을 막으므로 HTTPS 사용을 권장한다.
"""
import ctypes
import subprocess
from ctypes import wintypes
from urllib.parse import urlsplit

RESOURCETYPE_DISK = 0x00000001
CONNECT_UPDATE_PROFILE = 0x00000001  # 재부팅 후에도 유지(persistent)
_ERROR_NOT_CONNECTED = 2250


class _NETRESOURCE(ctypes.Structure):
    _fields_ = [
        ("dwScope", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwDisplayType", wintypes.DWORD),
        ("dwUsage", wintypes.DWORD),
        ("lpLocalName", wintypes.LPWSTR),
        ("lpRemoteName", wintypes.LPWSTR),
        ("lpComment", wintypes.LPWSTR),
        ("lpProvider", wintypes.LPWSTR),
    ]


def _mpr():
    mpr = ctypes.WinDLL("mpr.dll")
    mpr.WNetAddConnection2W.argtypes = [
        ctypes.POINTER(_NETRESOURCE), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    mpr.WNetAddConnection2W.restype = wintypes.DWORD
    mpr.WNetCancelConnection2W.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    mpr.WNetCancelConnection2W.restype = wintypes.DWORD
    return mpr


def _unc(server_url: str) -> str:
    u = urlsplit(server_url)
    host = u.hostname or ""
    port = u.port
    secure = u.scheme == "https"
    at = "@SSL" if secure else ""
    if secure and port == 443:
        at = "@SSL@443"
    elif port and not (secure and port == 443) and not (not secure and port == 80):
        at += f"@{port}"
    return rf"\\{host}{at}\dav"


_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def webclient_running() -> bool:
    """WebDAV 마운트에 필요한 WebClient 서비스가 실행 중인지 (관리자 권한 불필요)."""
    try:
        out = subprocess.run(["sc", "query", "webclient"], capture_output=True,
                             text=True, timeout=10, creationflags=_NO_WINDOW)
        return "RUNNING" in out.stdout.upper()
    except Exception:
        return False


def start_webclient_elevated():
    """관리자 권한으로 WebClient를 자동 시작 설정하고 켠다 (UAC 프롬프트 표시)."""
    inner = ("Set-Service WebClient -StartupType Automatic -ErrorAction SilentlyContinue; "
             "Start-Service WebClient -ErrorAction SilentlyContinue")
    launch = (f'Start-Process powershell -Verb RunAs '
              f'-ArgumentList \'-NoProfile\',\'-WindowStyle\',\'Hidden\',\'-Command\',"{inner}"')
    subprocess.run(["powershell", "-NoProfile", "-Command", launch],
                   creationflags=_NO_WINDOW, timeout=120)


def _ensure_webclient():
    """마운트 전에 WebClient를 시작한다 (best-effort, 관리자 아니면 실패할 수 있음)."""
    if webclient_running():
        return
    try:
        subprocess.run(["net", "start", "webclient"], capture_output=True,
                       timeout=20, creationflags=_NO_WINDOW)
    except Exception:
        pass


def _error_message(err: int, unc: str) -> str:
    msg = ctypes.FormatError(err).strip()
    hint = ""
    if err in (1326, 86, 1327):        # 로그온 실패 / 잘못된 비밀번호
        hint = "\n· 아이디 또는 비밀번호를 확인하세요."
    elif err in (67, 53, 1222, 66, 1231):  # 네트워크 경로/장치 문제
        hint = (
            "\n· Windows 'WebClient' 서비스가 실행 중이어야 합니다 "
            "(서비스에서 자동/수동 시작으로 설정하거나, 관리자 명령창에서 "
            "'net start webclient')."
            "\n· 서버가 WebDAV(/dav)를 제공하는 최신 버전인지 확인하세요."
            "\n· HTTPS 서버여야 합니다. Cloudflare 등 앞단이 있으면 /dav 경로의 "
            "WebDAV 클라이언트(Microsoft-WebDAV-MiniRedir)를 차단하지 않도록 예외를 두세요."
        )
    return f"드라이브 연결 실패 (코드 {err}: {msg}){hint}\n대상: {unc}"


def connect_drive(drive: str, server_url: str, username: str, password: str) -> str:
    unc = _unc(server_url)
    drive = drive.rstrip("\\")
    _ensure_webclient()
    mpr = _mpr()
    # 기존 매핑이 있으면 먼저 해제 (오류 무시)
    mpr.WNetCancelConnection2W(drive, 0, True)
    nr = _NETRESOURCE()
    nr.dwType = RESOURCETYPE_DISK
    nr.lpLocalName = drive
    nr.lpRemoteName = unc
    err = mpr.WNetAddConnection2W(ctypes.byref(nr), password, username, CONNECT_UPDATE_PROFILE)
    if err != 0:
        raise RuntimeError(_error_message(err, unc))
    return unc


def disconnect_drive(drive: str):
    drive = drive.rstrip("\\")
    err = _mpr().WNetCancelConnection2W(drive, CONNECT_UPDATE_PROFILE, True)
    if err not in (0, _ERROR_NOT_CONNECTED):
        raise RuntimeError(f"연결 해제 실패 (코드 {err}: {ctypes.FormatError(err).strip()})")
