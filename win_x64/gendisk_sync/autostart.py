"""Windows 로그인 시 자동 실행 등록/해제 (HKCU Run 레지스트리 키).

포터블 단일 exe 는 사용자가 바탕화면/다운로드 등에서 실행하고 이름을 바꾸거나 지우기
쉽다. Run 키가 그런 휘발성 경로를 가리키면 그 파일이 사라졌을 때 자동시작이 조용히
실패한다. 그래서 자동시작을 켤 때 exe 를 **안정적 위치**(%LOCALAPPDATA%\\genDISK\\)로
복사하고 Run 키가 그 사본을 가리키게 한다. 앱 시작 시에도 경로를 자가 치유한다.
"""
import os
import sys

APP_NAME = "gendisk-sync"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _stable_exe() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "genDISK")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "gendisk-sync.exe")


def _ensure_stable_copy() -> str:
    """현재 exe 를 안정적 위치로 복사(없거나 버전이 다르면)하고 그 경로를 돌려준다."""
    src = sys.executable
    stable = _stable_exe()
    try:
        if os.path.abspath(src) == os.path.abspath(stable):
            return stable                      # 이미 안정 위치에서 실행 중
        need = (not os.path.isfile(stable)
                or os.path.getsize(stable) != os.path.getsize(src))
        if need:
            import shutil
            shutil.copy2(src, stable)          # 최신 버전으로 갱신
        return stable
    except OSError:
        # 복사 실패(잠김 등) → 기존 안정 사본이 있으면 그걸, 없으면 현재 경로로 폴백
        return stable if os.path.isfile(stable) else src


def _command() -> str:
    """자동 시작 시 실행할 명령. --startup 플래그로 시작하면 창을 최소화한다."""
    if getattr(sys, "frozen", False):          # PyInstaller .exe
        return f'"{_ensure_stable_copy()}" --startup'
    # 스크립트 실행(개발): pythonw로 창 없이
    main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        pyw = pyw[:-len("python.exe")] + "pythonw.exe"
    return f'"{pyw}" "{main}" --startup'


def enable():
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _command())


def disable():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except FileNotFoundError:
        pass


def _current_value():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_NAME)[0]
    except FileNotFoundError:
        return None


def is_enabled() -> bool:
    return _current_value() is not None


def sync(enabled: bool):
    """설정값에 맞춰 등록 상태를 맞춘다. 켤 때는 항상 재등록해 오래된 경로를 자가 치유한다."""
    if enabled:
        enable()
    elif is_enabled():
        disable()
