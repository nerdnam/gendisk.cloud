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


def log_path() -> str:
    """rclone 로그 파일 경로 (%LOCALAPPDATA%\\genDISK\\rclone.log).
    마운트가 예고 없이 끊겼을 때 원인을 볼 수 있는 유일한 기록이다."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "genDISK")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "rclone.log")
    try:                       # 무한히 커지지 않게 8MB 넘으면 한 번 밀어낸다
        if os.path.getsize(p) > 8 * 1024 * 1024:
            old = p + ".1"
            if os.path.exists(old):
                os.remove(old)
            os.replace(p, old)
    except OSError:
        pass
    return p


def winfsp_installed() -> bool:
    for p in (r"C:\Program Files (x86)\WinFsp\bin\winfsp-x64.dll",
              r"C:\Program Files\WinFsp\bin\winfsp-x64.dll"):
        if os.path.isfile(p):
            return True
    return False


#: 자동 설치 대상 — (표시 이름, winget 패키지 ID, 설치 확인 함수)
REQUIREMENTS = (
    ("WinFsp", "WinFsp.WinFsp", lambda: winfsp_installed()),
    ("rclone", "Rclone.Rclone", lambda: bool(rclone_path())),
)


def missing_requirements() -> list[tuple[str, str]]:
    """설치되지 않은 구성 요소 [(이름, winget ID)]."""
    return [(name, pkg) for name, pkg, ok in REQUIREMENTS if not ok()]


def requirements_message() -> str | None:
    """빠진 구성 요소가 있으면 안내 문구, 다 있으면 None."""
    missing = missing_requirements()
    if not missing:
        return None
    return ("genDISK Drive 연결에 다음이 필요합니다:\n · " +
            "\n · ".join(f"{n} ({pkg})" for n, pkg in missing))


def winget_available() -> bool:
    from shutil import which
    return bool(which("winget"))


def install_requirements(log=None) -> str | None:
    """빠진 구성 요소를 winget 으로 설치한다(관리자 권한 승격 1회).
    성공하면 None, 실패하면 사람이 읽을 오류 문구를 돌려준다."""
    import ctypes

    missing = missing_requirements()
    if not missing:
        return None
    if not winget_available():
        return ("winget(앱 설치 관리자)을 찾을 수 없습니다.\n"
                "Microsoft Store 에서 '앱 설치 관리자'를 설치하거나,\n"
                "다음 주소에서 직접 받아 설치하세요:\n"
                " · WinFsp: https://winfsp.dev\n · rclone: https://rclone.org/downloads/")

    # 한 번의 UAC 승인으로 필요한 것을 모두 설치한다.
    cmds = "; ".join(
        f"winget install --id {pkg} -e --accept-source-agreements "
        f"--accept-package-agreements" for _n, pkg in missing)
    if log:
        log(f"구성 요소 설치 시작: {', '.join(n for n, _ in missing)}")
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe",
        f'-NoProfile -ExecutionPolicy Bypass -Command "{cmds}"', None, 0)
    if rc <= 32:                       # 32 이하 = 실행 실패(취소 포함)
        return ("설치를 시작하지 못했습니다(관리자 권한 승인이 취소되었을 수 있습니다).\n"
                "다음 명령을 직접 실행해도 됩니다:\n" +
                "\n".join(f" winget install --id {pkg} -e" for _n, pkg in missing))

    # 설치 완료를 기다린다(최대 5분) — 설치 후 PATH 갱신 전이라 경로 탐색으로 확인.
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if not missing_requirements():
            if log:
                log("구성 요소 설치 완료")
            return None
        time.sleep(2.0)
    still = ", ".join(n for n, _ in missing_requirements())
    return f"설치가 확인되지 않았습니다 ({still}). 설치 창을 확인한 뒤 다시 시도하세요."


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


#: WinFsp 가 보고하는 파일시스템 이름(마운트 판별용). 일반 폴더는 NTFS/FAT 등이 나온다.
_WINFSP_FS_NAMES = ("WinFsp", "FUSE", "rclone")


def filesystem_name(point: str) -> str | None:
    """마운트 지점의 파일시스템 이름. 실패하면 None."""
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(point.rstrip("\\") + "\\"),
        None, 0, None, None, None, buf, ctypes.sizeof(buf) // 2)
    return buf.value if ok else None


def is_mounted(point: str) -> bool:
    """마운트 지점이 **실제로 rclone/WinFsp 마운트인지** 확인한다.

    주의: 예전에는 'os.listdir 가 되면 마운트됨'으로 판정했는데, 구버전(온디맨드)이
    남긴 로컬 폴더도 목록이 읽히는 바람에 mount() 가 그냥 반환해 버렸다. 그러면 앱은
    '연결됨'이라고 알리는데 사용자는 서버가 아닌 죽은 로컬 폴더를 보게 된다.
    그래서 파일시스템 종류로 진짜 마운트인지 가린다."""
    point = point.rstrip("\\")
    if not os.path.isdir(point):
        return False
    fs = filesystem_name(point)
    if fs is None:
        return False        # 일반 폴더는 볼륨 정보가 없다(ERROR_DIR_NOT_ROOT)
    if len(point) <= 2:      # 드라이브 문자 지점: 진짜 디스크와 구분해야 한다
        return any(k.lower() in fs.lower() for k in _WINFSP_FS_NAMES)
    return True              # 폴더 지점에 볼륨 정보가 있다 = 무언가 마운트되어 있음


def _looks_like_stale_placeholder_tree(point: str) -> bool:
    """구버전 온디맨드가 남긴 잔재인지 판별한다 — 실데이터 삭제를 막는 안전장치.
    잔재는 (a) 클라우드 플레이스홀더(재파스 포인트)이거나 (b) desktop.ini 뿐이거나
    (c) 내용이 0바이트인 항목들이다. 하나라도 실제 내용이 있는 파일이 보이면 False."""
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    real_bytes = 0
    try:
        for root, _dirs, files in os.walk(point):
            for n in files:
                if n.lower() == "desktop.ini":
                    continue
                p = os.path.join(root, n)
                try:
                    st = os.lstat(p)
                except OSError:
                    return False               # 확인 불가 → 건드리지 않는다
                if st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    continue                   # 온디맨드 플레이스홀더
                real_bytes += st.st_size
                if real_bytes > 0:
                    return False               # 실제 데이터가 있다 → 잔재 아님
    except OSError:
        return False
    return True


def mount(point: str, volname: str = "genDISK Drive", timeout: float = 25.0):
    """마운트 지점에 서버를 연결한다.

    지점은 **폴더 경로**를 쓴다(기본 %USERPROFILE%\\genDISK). 드라이브 문자를
    만들지 않고, 탐색기 사이드바의 genDISK Drive 노드가 이 폴더를 가리키게 해
    브랜디드 항목 하나로 서버를 열게 하기 위함이다. ('G:' 형식도 동작한다.)
    rclone 은 마운트 지점이 존재하지 않아야 하므로, 비어 있으면 지우고 건다."""
    msg = requirements_message()
    if msg:
        raise RuntimeError(msg)
    point = point.rstrip("\\")
    if is_mounted(point):
        return
    if len(point) > 2 and os.path.isdir(point):   # 폴더 지점(드라이브 문자 아님)
        try:
            os.rmdir(point)                       # 비어 있을 때만 성공 — 내용 보호
        except OSError:
            # 비어 있지 않다 = 구버전 온디맨드가 남긴 플레이스홀더 잔재일 가능성이 크다.
            # 그대로 두면 rclone 이 마운트하지 못하고, 사용자는 죽은 로컬 폴더를 보게 된다.
            # 실제 데이터가 아닌 잔재일 때만(플레이스홀더/빈 파일) 치운다.
            if _looks_like_stale_placeholder_tree(point):
                import shutil
                shutil.rmtree(point, ignore_errors=True)
            if os.path.isdir(point):
                raise RuntimeError(
                    f"마운트 지점에 폴더가 남아 있어 연결할 수 없습니다:\n{point}\n\n"
                    "그 폴더의 내용을 확인해 옮기거나 지운 뒤 다시 시도하세요.")
    rc = rclone_path()
    args = [rc, "mount", f"{REMOTE}:", point,
            "--volname", volname,
            "--vfs-cache-mode", "writes",  # 쓰기만 임시 버퍼 — 목록·읽기는 서버 직결
            "--dir-cache-time", "10s",   # 폴더 목록 캐시 짧게 → 항상 최신에 가깝게
            # 연결이 끊겨도 탐색기에 'I/O 장치 오류'로 튀지 않게 충분히 재시도한다.
            # (VPN·무선 전환처럼 경로가 흔들리면 TCP 가 통째로 끊기는데, 기본
            #  재시도 횟수로는 그 순간 열람이 그대로 실패로 보였다.)
            "--low-level-retries", "20",
            "--retries", "10",
            "--timeout", "60s",
            "--contimeout", "20s",
            "--ftp-close-timeout", "10s",
            # 마운트가 조용히 죽으면 원인을 알 길이 없었다 — 로그를 파일로 남긴다.
            "--log-file", log_path(), "--log-level", "INFO",
            "--no-console"]
    subprocess.Popen(args, creationflags=_NOWINDOW)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_mounted(point):
            return
        time.sleep(0.5)
    raise RuntimeError(f"{point} 마운트가 시간 안에 준비되지 않았습니다.")


def _our_rclone_pids(point: str) -> list[int]:
    """이 마운트 지점을 서비스 중인 rclone 프로세스 PID 목록.
    사용자의 다른 rclone 작업(백업·다른 마운트)을 건드리지 않기 위해 명령줄로 가린다."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='rclone.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")
    r = _run(["powershell.exe", "-NoProfile", "-Command", ps], timeout=30)
    pids = []
    needle = os.path.normcase(point.rstrip("\\"))
    for line in (r.stdout or "").splitlines():
        pid, _, cmd = line.partition("|")
        if not pid.strip().isdigit() or not cmd:
            continue
        if needle in os.path.normcase(cmd) and " mount " in cmd:
            pids.append(int(pid.strip()))
    return pids


def unmount(point: str, timeout: float = 15.0):
    """이 마운트만 해제한다. 먼저 정상 종료를 시도해 쓰기 버퍼를 비울 기회를 준다
    (--vfs-cache-mode writes 라 강제 종료하면 아직 안 올라간 파일이 사라진다)."""
    point = point.rstrip("\\")
    if not is_mounted(point):
        return True
    pids = _our_rclone_pids(point)
    if not pids:
        return not is_mounted(point)
    for pid in pids:                       # 1차: 정상 종료 요청(버퍼 플러시)
        _run(["taskkill", "/PID", str(pid)], timeout=20)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_mounted(point):
            return True
        time.sleep(0.25)
    for pid in pids:                       # 2차: 그래도 안 내려가면 강제
        _run(["taskkill", "/F", "/PID", str(pid)], timeout=20)
    for _ in range(20):
        if not is_mounted(point):
            return True
        time.sleep(0.25)
    return not is_mounted(point)
