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
import threading
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


def _unregister_stale_sync_root(point: str) -> bool:
    """구버전 온디맨드(Cloud Files) 드라이브가 남긴 sync root 등록을 푼다.

    등록만 남고 provider 가 없으면 폴더가 비어 있어도 열람·삭제가 전부
    'cloud file provider is not running' 으로 실패해 rmdir 가 통하지 않는다.
    등록을 풀면 일반 폴더로 돌아온다. sync root 가 아니면 그냥 실패(무해)."""
    import ctypes
    try:
        hr = ctypes.windll.cldapi.CfUnregisterSyncRoot(ctypes.c_wchar_p(point))
        return hr == 0
    except Exception:  # noqa: BLE001
        return False


def probe(point: str, timeout: float = 8.0) -> bool:
    """마운트가 **실제로 응답하는지** 확인한다.

    is_mounted() 는 볼륨이 존재하는지만 본다. 그런데 실제 장애는 rclone 프로세스가
    멀쩡한 채 FTP 연결만 죽는 형태였다(끊긴 소켓을 계속 재사용해 모든 요청이 실패).
    그때도 볼륨은 존재하므로 is_mounted() 는 True 였고 워치독은 몇 분간 아무것도
    하지 않았다 — 실측 7분 34초. 그래서 목록을 실제로 한 번 읽어 본다.

    os.listdir 이 걸려 돌아오지 않을 수 있으므로 별도 스레드에서 돌리고 기다린다
    (감시 스레드가 통째로 멈추면 안 된다)."""
    point = point.rstrip("\\")
    result = {}

    def work():
        try:
            os.listdir(point + "\\")
            result["ok"] = True
        except Exception:  # noqa: BLE001
            result["ok"] = False

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return result.get("ok", False)          # 시간 안에 응답 없으면 비정상으로 본다


def recent_errors(seconds: float = 120.0, limit: int = 4000) -> int:
    """최근 N초 안에 rclone 로그에 찍힌 연결 오류 수.

    프로브가 실패했을 때 '연결이 죽었나'와 '단지 바쁜가'를 가르는 데 쓴다.
    썸네일 생성처럼 전송이 몰리면 목록 응답이 늦어 프로브가 실패할 수 있는데,
    그때 재마운트하면 받던 것을 끊어 오히려 악화된다. 진짜 사망이면 로그에
    연결 오류(aborted/reset/timeout)가 쏟아진다."""
    import re
    path = os.path.join(os.environ.get("LOCALAPPDATA") or "", "genDISK", "rclone.log")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            try:
                f.seek(max(0, os.path.getsize(path) - 400_000))
                f.readline()
            except OSError:
                pass
            lines = f.readlines()[-limit:]
    except OSError:
        return 0
    cutoff = time.time() - seconds
    pat = re.compile(r"aborted|reset by peer|i/o timeout|connection refused|Dir\.Stat error")
    n = 0
    for ln in lines:
        if "ERROR" not in ln or not pat.search(ln):
            continue
        try:
            ts = time.mktime(time.strptime(ln[:19], "%Y/%m/%d %H:%M:%S"))
        except ValueError:
            continue
        if ts >= cutoff:
            n += 1
    return n


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
            if _unregister_stale_sync_root(point):
                try:
                    os.rmdir(point)               # 등록이 풀리면 빈 폴더는 이제 지워진다
                except OSError:
                    pass
            stale = os.path.isdir(point) and _looks_like_stale_placeholder_tree(point)
            if stale:
                import shutil
                shutil.rmtree(point, ignore_errors=True)
            if stale and os.path.isdir(point):
                # 메타데이터가 손상된 플레이스홀더는 삭제 자체가 거부되기도 한다.
                # 잔재로 판정된 경우에 한해 옆으로 치워 지점을 비운다(실데이터면 여기 안 옴).
                for i in range(100):
                    cand = f"{point}.old-leftover" + (f"-{i}" if i else "")
                    if not os.path.exists(cand):
                        try:
                            os.rename(point, cand)
                        except OSError:
                            pass
                        break
            if os.path.isdir(point):
                raise RuntimeError(
                    f"마운트 지점에 폴더가 남아 있어 연결할 수 없습니다:\n{point}\n\n"
                    "그 폴더의 내용을 확인해 옮기거나 지운 뒤 다시 시도하세요.")
    rc = rclone_path()
    args = [rc, "mount", f"{REMOTE}:", point,
            "--volname", volname,
            # 썸네일·미리 보기가 실용적으로 동작하려면 읽기 캐시가 필요하다.
            # writes 모드에서는 탐색기가 같은 파일을 볼 때마다 매번 다시 내려받아
            # 폴더 하나 여는 데 FTP 연결 8개가 모두 묶이고 화면이 멈췄다.
            # full 모드 + 용량·기간 상한으로 '한 번 받은 것만' 잠시 재사용한다.
            "--vfs-cache-mode", "full",
            "--vfs-cache-max-size", "20G",
            "--vfs-cache-max-age", "168h",       # 7일 — 어제 본 폴더를 다시 안 받게
            # 상한과 별개로 디스크 여유가 이 아래로 떨어지면 캐시를 비운다(디스크 보호)
            "--vfs-cache-min-free-space", "20G",
            # 썸네일은 파일 앞부분만 읽는다. 기본 청크가 128M 이라 큰 동영상까지
            # 통째로 받아왔다 — 작게 시작해 필요할 때만 키운다.
            "--vfs-read-chunk-size", "1M",
            "--vfs-read-chunk-size-limit", "128M",
            "--vfs-fast-fingerprint",     # 변경 감지에 드는 메타데이터 왕복 감소
            # 순단을 흡수하려면 목록 캐시가 어느 정도 길어야 한다. 10초로 짧게 잡았더니
            # 잠깐의 끊김이 곧바로 탐색기 오류로 번역됐다.
            "--dir-cache-time", "3m",
            # 재시도는 '짧고 확실하게'. --timeout(60s) × --low-level-retries 가 그대로
            # 곱해지므로 20회는 최악의 경우 탐색기를 20분 붙잡는다. 오래 매달리기보다
            # 빨리 실패하고 워치독이 재마운트하는 편이 회복이 빠르다.
            "--low-level-retries", "4",
            "--timeout", "30s",          # IO 유휴 타임아웃
            "--contimeout", "15s",
            "--ftp-close-timeout", "10s",
            # 죽은 연결을 오래 물고 있지 않게 유휴 연결을 정리한다(핵심: rclone 이
            # abort 된 소켓을 계속 재사용하는 바람에 수 분간 전부 실패했다).
            "--ftp-idle-timeout", "30s",
            # 썸네일은 여러 파일을 동시에 읽는다. 8개로는 폴더 하나에 금방 포화돼
            # 목록 조회까지 밀렸다(서버는 IP당 64까지 허용).
            "--ftp-concurrency", "16",
            "--transfers", "8",
            # 마운트가 조용히 죽으면 원인을 알 길이 없었다 — 로그를 파일로 남긴다.
            "--log-file", log_path(), "--log-level", "INFO",
            "--no-console"]
    proc = subprocess.Popen(args, creationflags=_NOWINDOW)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_mounted(point):
            return
        if proc.poll() is not None:      # rclone 이 스스로 죽었다 — 더 기다릴 이유가 없다
            raise RuntimeError(
                f"rclone 이 마운트에 실패했습니다 (종료코드 {proc.returncode}). "
                f"자세한 내용은 {log_path()} 를 확인하세요.")
        time.sleep(0.5)
    # 타임아웃: 우리가 띄운 프로세스를 반드시 회수한다. 안 그러면 워치독이 재시도할
    # 때마다 좀비 rclone 이 하나씩 쌓여 서버 연결만 갉아먹는다.
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass
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
        if not _cmd_targets_point(cmd, needle):
            continue
        pids.append(int(pid.strip()))
    return pids


def _cmd_targets_point(cmd: str, needle: str) -> bool:
    """rclone 명령줄이 '정확히 이 마운트 지점'을 대상으로 하는지.
    단순 부분문자열이면 형제 경로(...\\genDISK2)나 하위 경로의 남의 마운트까지
    잡아 강제 종료해 버린다 — 경계를 확인해 그런 오탐을 막는다."""
    low = os.path.normcase(cmd)
    if " mount " not in low or needle not in low:
        return False
    i = low.find(needle)
    tail = low[i + len(needle):i + len(needle) + 1]
    return tail in ("", " ", '"', "\\", "\t")     # 뒤에 경로가 더 붙으면 다른 지점


def unmount(point: str, timeout: float = 15.0):
    """이 마운트만 해제한다. 먼저 정상 종료를 시도해 쓰기 버퍼를 비울 기회를 준다
    (--vfs-cache-mode writes 라 강제 종료하면 아직 안 올라간 파일이 사라진다).

    주의: 마운트가 이미 보이지 않아도 프로세스는 남아 있을 수 있다(마운트 타임아웃·
    비정상 종료). 그때도 반드시 회수해야 좀비 rclone 이 쌓이지 않는다."""
    point = point.rstrip("\\")
    pids = _our_rclone_pids(point)
    if not pids:
        return not is_mounted(point)
    for pid in pids:                       # 1차: 정상 종료 요청(버퍼 플러시)
        _run(["taskkill", "/PID", str(pid)], timeout=20)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_mounted(point) and not _our_rclone_pids(point):
            return True
        time.sleep(0.5)
    for pid in _our_rclone_pids(point) or pids:   # 2차: 남은 것만 강제 종료
        _run(["taskkill", "/F", "/PID", str(pid)], timeout=20)
    for _ in range(20):
        if not is_mounted(point):
            return True
        time.sleep(0.25)
    return not is_mounted(point)
