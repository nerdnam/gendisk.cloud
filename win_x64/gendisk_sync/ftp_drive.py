"""genDISK FTP 를 실제 드라이브(예: G:)로 마운트한다 — rclone + WinFsp.

온디맨드(Cloud Files) 방식과 달리 **로컬에 목록·파일을 저장하지 않는다**.
탐색기는 마운트된 드라이브를 통해 서버를 직접 읽고 쓰며, 사이드바('내 PC')에
네트워크 드라이브로 나타난다. 서버 상태가 곧 화면이라 플레이스홀더 손상·
목록 불일치 같은 문제가 원천적으로 없다.

필요 구성 요소(각 1회 설치):
  · WinFsp   — 사용자 모드 파일시스템 드라이버
  · rclone   — FTP 백엔드
둘 다 winget 으로 설치할 수 있고, 없으면 안내 메시지를 낸다.
"""
import os
import subprocess
import time

REMOTE = "gendisk"                      # rclone 원격 이름 (앱 전용)
_NOWINDOW = 0x08000000                  # CREATE_NO_WINDOW


def _run(args, timeout=30):
    # errors="replace": taskkill 등 콘솔 도구가 한글(CP949) 을 뱉어도 죽지 않게
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          creationflags=_NOWINDOW, timeout=timeout)


def rclone_path() -> str | None:
    """rclone.exe 경로 (PATH → winget 설치 경로 순)."""
    from shutil import which
    p = which("rclone")
    if p:
        return p
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            if "rclone.exe" in files:
                return os.path.join(root, "rclone.exe")
    return None


def winfsp_installed() -> bool:
    for p in (r"C:\Program Files (x86)\WinFsp\bin\winfsp-x64.dll",
              r"C:\Program Files\WinFsp\bin\winfsp-x64.dll"):
        if os.path.isfile(p):
            return True
    return False


def requirements_message() -> str | None:
    """빠진 구성 요소가 있으면 안내 문구, 다 있으면 None."""
    missing = []
    if not winfsp_installed():
        missing.append("WinFsp (winget install WinFsp.WinFsp)")
    if not rclone_path():
        missing.append("rclone (winget install Rclone.Rclone)")
    if not missing:
        return None
    return ("FTP 드라이브 연결에 다음이 필요합니다:\n · " + "\n · ".join(missing) +
            "\n\n설치 후 다시 시도하세요.")


def configure(host: str, port: int, username: str, password: str, tls: bool = False):
    """rclone 원격을 현재 접속 정보로 만들거나 갱신한다 (비밀번호는 rclone 이 난독화 저장)."""
    rc = rclone_path()
    if not rc:
        raise RuntimeError("rclone 을 찾을 수 없습니다")
    args = [rc, "config", "create", REMOTE, "ftp",
            f"host={host}", f"port={int(port)}", f"user={username}",
            f"pass={password}", "--obscure", "--non-interactive"]
    if tls:
        args.append("explicit_tls=true")
    r = _run(args, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"rclone 설정 실패: {(r.stderr or r.stdout)[:300]}")


def is_mounted(drive: str) -> bool:
    return os.path.isdir(drive.rstrip("\\") + "\\")


def mount(drive: str, volname: str = "genDISK", timeout: float = 25.0):
    """drive(예: 'G:')에 마운트한다. 이미 있으면 그대로 둔다."""
    msg = requirements_message()
    if msg:
        raise RuntimeError(msg)
    drive = drive.rstrip("\\")
    if is_mounted(drive):
        return
    rc = rclone_path()
    args = [rc, "mount", f"{REMOTE}:", drive,
            "--volname", volname,
            "--network-mode",            # 네트워크 드라이브로 표시(탐색기 사이드바)
            "--vfs-cache-mode", "writes",  # 쓰기만 임시 버퍼 — 목록·읽기는 서버 직결
            "--dir-cache-time", "10s",   # 폴더 목록 캐시 짧게 → 항상 최신에 가깝게
            "--no-console"]
    subprocess.Popen(args, creationflags=_NOWINDOW)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_mounted(drive):
            return
        time.sleep(0.5)
    raise RuntimeError(f"{drive} 마운트가 시간 안에 준비되지 않았습니다.")


def unmount(drive: str):
    """마운트 해제 (rclone 프로세스 종료)."""
    drive = drive.rstrip("\\")
    _run(["taskkill", "/F", "/IM", "rclone.exe"], timeout=20)
    for _ in range(20):
        if not is_mounted(drive):
            return True
        time.sleep(0.25)
    return not is_mounted(drive)
