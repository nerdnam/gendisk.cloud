"""GitHub Releases 자동 업데이트 — 최신 릴리스 확인·다운로드·자체 교체.

릴리스는 CI(docker.yml)가 main 푸시마다 만든다: 태그 vX.Y.Z + 자산
gendisk-sync-X.Y.Z.exe. 이 모듈은 시작 시 최신 태그를 현재 버전과 비교해
새 버전이면 exe 를 받아 **실행 중인 exe 자리**를 바꾼다(바탕화면 사본 등
사용자가 실제로 실행하는 파일이 갱신되도록). 안정 사본(%LOCALAPPDATA%)은
기존 업그레이드 인계 로직(main.py/autostart.py)이 다음 시작에서 맞춘다.

교체 순서(윈도우는 실행 중인 exe 삭제는 안 되지만 **이름 변경은 된다**):
  1) 새 exe 를 update 폴더로 다운로드(크기 검증)
  2) 실행 중 exe → <exe>.old 로 이름 변경
  3) 새 exe 를 원래 경로로 이동
  4) 2초 뒤 원래 경로를 다시 실행하는 분리 프로세스를 띄우고 앱은 종료
  5) 다음 시작에서 cleanup_old() 가 <exe>.old 를 지운다
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request

REPO = "nerdnam/gendisk.cloud"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
_UA = {"User-Agent": "gendisk-sync-updater"}
_NOWINDOW = 0x08000000                  # CREATE_NO_WINDOW


def current_version() -> tuple[int, ...] | None:
    """현재 exe 버전. 개발 실행(스크립트/버전 미주입 빌드)은 None — 업데이트 안 함."""
    if not getattr(sys, "frozen", False):
        return None
    from . import __version__
    ver = _parse(__version__)
    if not ver or ver == (0, 0, 0):     # 0.0.0 = CI 주입 전 개발 기본값
        return None
    return ver


def _parse(tag: str) -> tuple[int, ...] | None:
    s = tag.lstrip("vV").strip()
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        return None


def check(timeout: float = 10.0):
    """새 버전이 있으면 (버전문자열, 다운로드 URL, 크기) — 없거나 개발 실행이면 None."""
    cur = current_version()
    if cur is None:
        return None
    req = urllib.request.Request(API_LATEST, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rel = json.load(r)
    latest = _parse(rel.get("tag_name", ""))
    if not latest or latest <= cur:
        return None
    for a in rel.get("assets", []):
        name = a.get("name", "")
        if name.startswith("gendisk-sync") and name.endswith(".exe"):
            return rel["tag_name"].lstrip("vV"), a["browser_download_url"], int(a["size"])
    return None


def _update_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "genDISK", "update")
    os.makedirs(d, exist_ok=True)
    return d


def apply(url: str, size: int, log=print):
    """새 exe 를 받아 실행 중인 exe 를 교체하고 재시작을 예약한다.
    성공하면 호출 쪽에서 앱을 종료해야 한다(재시작 프로세스가 대기 중)."""
    exe = sys.executable
    tmp = os.path.join(_update_dir(), "gendisk-sync-new.exe")
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    got = os.path.getsize(tmp)
    if got != size:                      # 잘린 다운로드로 자기 자신을 덮지 않게
        os.remove(tmp)
        raise RuntimeError(f"다운로드 크기 불일치 ({got} != {size})")
    old = exe + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass
    os.rename(exe, old)                  # 실행 중에도 이름 변경은 허용된다
    try:
        shutil.move(tmp, exe)
    except OSError:
        os.rename(old, exe)              # 실패 시 원복
        raise
    log(f"업데이트 파일 교체 완료: {exe}")
    # 앱이 자리를 비운 뒤(단일 인스턴스 잠금 해제) 새 버전을 실행한다.
    subprocess.Popen(["cmd", "/c", f'ping -n 3 127.0.0.1 >nul & start "" "{exe}"'],
                     creationflags=_NOWINDOW)


def cleanup_old():
    """이전 업데이트가 남긴 <exe>.old 제거 (시작 시 호출; 실패는 무시)."""
    if not getattr(sys, "frozen", False):
        return
    for p in (sys.executable + ".old", os.path.join(_update_dir(), "gendisk-sync-new.exe")):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
