"""탐색기 미리 보기·썸네일 끄기/되돌리기.

배경: 탐색기는 썸네일을 만들려고 폴더 안 파일들의 **내용을 실제로 읽는다**.
genDISK Drive 는 FTP 서버 직결이라 그때마다 서버에서 파일을 내려받게 되어,
폴더 하나 여는 것이 파일 수십 개 다운로드가 된다(미리 보기 창은 선택할 때마다 또 읽음).
미리 보기를 끄면 이 트래픽이 아예 발생하지 않는다.

설정은 HKCU 라 관리자 권한이 필요 없다. 두 가지를 함께 끈다:
  · IconsOnly=1      — 썸네일 대신 아이콘 표시 (폴더 옵션의 그 항목)
  · NoPreviewPane=1  — 미리 보기 창 비활성

주의: 이 설정은 **탐색기 전역**이다(윈도우가 폴더별 썸네일 정책을 제공하지 않음).
그래서 앱이 끌 때 원래 값을 저장해 두고, 되돌릴 때 그대로 복원한다.
"""
import ctypes
import winreg

_ADVANCED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
_POLICIES = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
_BACKUP = r"Software\gendisk-sync\explorer-backup"   # 원래 값 보관 (복원용)

SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000


def _get(path: str, name: str):
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
    except OSError:
        return None
    try:
        return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None
    finally:
        winreg.CloseKey(k)


def _set(path: str, name: str, value, required: bool = True) -> bool:
    """레지스트리 값 설정(또는 삭제). required=False 면 권한 거부 시 조용히 넘어간다
    — 정책 키는 환경에 따라 쓰기가 막혀 있을 수 있다."""
    try:
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE)
    except OSError:
        if required:
            raise
        return False
    try:
        if value is None:
            try:
                winreg.DeleteValue(k, name)
            except OSError:
                pass
        else:
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(value))
        return True
    except OSError:
        if required:
            raise
        return False
    finally:
        winreg.CloseKey(k)


def _notify_explorer():
    """설정이 새 탐색기 창에 반영되도록 알린다."""
    try:
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except Exception:  # noqa: BLE001
        pass


def is_disabled() -> bool:
    """썸네일이 꺼져 있으면 True. (미리 보기 창 정책은 환경에 따라 못 쓸 수 있어
    판정 기준에서 제외한다 — 서버 부하의 대부분은 썸네일이다.)"""
    return _get(_ADVANCED, "IconsOnly") == 1


def disable() -> bool:
    """미리 보기·썸네일 끄기. 원래 값을 백업해 둔다(이미 꺼져 있으면 그대로)."""
    if is_disabled():
        return False
    # 백업은 '처음 끌 때' 한 번만 — 반복 호출로 백업이 오염되지 않게
    if _get(_BACKUP, "saved") != 1:
        icons = _get(_ADVANCED, "IconsOnly")
        pane = _get(_POLICIES, "NoPreviewPane")
        _set(_BACKUP, "IconsOnly", 0 if icons is None else icons)
        _set(_BACKUP, "IconsOnlyExisted", 0 if icons is None else 1)
        _set(_BACKUP, "NoPreviewPane", 0 if pane is None else pane)
        _set(_BACKUP, "NoPreviewPaneExisted", 0 if pane is None else 1)
        _set(_BACKUP, "saved", 1)
    _set(_ADVANCED, "IconsOnly", 1)              # 썸네일 대신 아이콘 (핵심)
    _set(_POLICIES, "NoPreviewPane", 1, required=False)   # 미리 보기 창 (가능하면)
    _notify_explorer()
    return True


def restore() -> bool:
    """끄기 전 상태로 되돌린다. 백업이 없으면 기본값(켬)으로."""
    if _get(_BACKUP, "saved") == 1:
        icons = _get(_BACKUP, "IconsOnly")
        pane = _get(_BACKUP, "NoPreviewPane")
        _set(_ADVANCED, "IconsOnly",
             icons if _get(_BACKUP, "IconsOnlyExisted") == 1 else None)
        _set(_POLICIES, "NoPreviewPane",
             pane if _get(_BACKUP, "NoPreviewPaneExisted") == 1 else None,
             required=False)
        _set(_BACKUP, "saved", 0)
    else:
        _set(_ADVANCED, "IconsOnly", 0)
        _set(_POLICIES, "NoPreviewPane", None, required=False)
    _notify_explorer()
    return True
