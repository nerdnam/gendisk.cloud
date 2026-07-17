"""genDISK Drive — 브랜디드 클라우드 드라이브 노드를 탐색기 사이드바에 띄운다.

언패키지·무서명·무-UAC 로 동작한다. Win11 25H2 실측 결과:
  · 노드 렌더는 HKCU 만으로는 안 되고 HKLM\\...\\SyncRootManager 항목이 있어야 한다.
  · HKLM\\SyncRootManager 는 비관리자(BUILTIN\\Users)도 쓸 수 있다(ACL 허용).
  · HKLM\\Classes\\CLSID 는 관리자 전용 → 그래서 CLSID 백킹은 HKCU 에 둔다.
따라서 [HKLM SyncRootManager(비관리자) + HKCU Classes\\CLSID 위임 폴더] 조합으로
관리자 권한/서명/패키지 없이 브랜디드 드라이브 노드를 만든다.

호출되는 바이너리는 Microsoft shell32.dll 뿐(위임 폴더 호스트). 커스텀 DLL 없음.
실제 온디맨드 동작(플레이스홀더/하이드레이션)은 vfs.py 의 CfAPI 가 담당한다.
"""
import ctypes
import os
import re
import subprocess
import winreg

# 이 드라이브 노드의 셸 네임스페이스 CLSID (고정)
NS_CLSID = "{17313618-1A3D-4726-91B9-E43B80FBA65C}"
# Windows 내장 제네릭 파일-폴더 위임 인스턴스 (그대로 사용)
DELEGATE_CLSID = "{0E5AAE11-A475-4c5b-AB00-C66DE400274E}"
DISPLAY_NAME = "genDISK Drive"   # 사이드바에 보이는 이름
PROVIDER_NAME = "genDISK"        # SyncRootId 내부 식별자(변경 금지)

_HKCU = winreg.HKEY_CURRENT_USER
_HKLM = winreg.HKEY_LOCAL_MACHINE
_EXPLORER = r"Software\Microsoft\Windows\CurrentVersion\Explorer"
_SRM = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\SyncRootManager"
# HKCU 의 CLSID 는 64/32 비트 셸 호스트 각각을 위해 두 곳에 둔다
_CLSID_ROOTS = (r"Software\Classes\CLSID", r"Software\Classes\Wow6432Node\CLSID")
_WOW64 = winreg.KEY_WOW64_64KEY


def _set(hive, path, name, value, t=winreg.REG_SZ):
    k = winreg.CreateKeyEx(hive, path, 0, winreg.KEY_WRITE | _WOW64)
    try:
        winreg.SetValueEx(k, name, 0, t, value)
    finally:
        winreg.CloseKey(k)


def _del_tree(hive, path):
    try:
        k = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | winreg.KEY_WRITE | _WOW64)
    except FileNotFoundError:
        return
    try:
        while True:
            try:
                sub = winreg.EnumKey(k, 0)
            except OSError:
                break
            _del_tree(hive, path + "\\" + sub)
    finally:
        winreg.CloseKey(k)
    try:
        winreg.DeleteKeyEx(hive, path, _WOW64)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def current_sid() -> str:
    out = subprocess.check_output(["whoami", "/user"], text=True,
                                  encoding="utf-8", errors="replace")
    m = re.search(r"S-1-5-21-[\d-]+", out)
    if not m:
        raise RuntimeError("현재 사용자 SID 를 찾지 못했습니다")
    return m.group(0)


def sync_root_id(sid: str) -> str:
    return f"{PROVIDER_NAME}!{sid}!Personal"


def _write_clsid(clsid_base, root, icon):
    """HKCU 의 CLSID 트리 하나(64 또는 32비트)를 shell32 위임 폴더로 등록."""
    _set(_HKCU, clsid_base, None, DISPLAY_NAME)
    _set(_HKCU, clsid_base, "System.IsPinnedToNameSpaceTree", 1, winreg.REG_DWORD)
    _set(_HKCU, clsid_base, "SortOrderIndex", 0x42, winreg.REG_DWORD)
    _set(_HKCU, clsid_base + r"\DefaultIcon", None, icon + ",0", winreg.REG_EXPAND_SZ)
    _set(_HKCU, clsid_base + r"\InProcServer32", None,
         r"%systemroot%\system32\shell32.dll", winreg.REG_EXPAND_SZ)
    _set(_HKCU, clsid_base + r"\InProcServer32", "ThreadingModel", "Both")
    _set(_HKCU, clsid_base + r"\Instance", "CLSID", DELEGATE_CLSID)
    _set(_HKCU, clsid_base + r"\Instance\InitPropertyBag", "Attributes", 0x11, winreg.REG_DWORD)
    _set(_HKCU, clsid_base + r"\Instance\InitPropertyBag", "TargetFolderPath", root,
         winreg.REG_EXPAND_SZ)
    _set(_HKCU, clsid_base + r"\ShellFolder", "FolderValueFlags", 0x28, winreg.REG_DWORD)
    _set(_HKCU, clsid_base + r"\ShellFolder", "Attributes", 0xF080004D, winreg.REG_DWORD)


def set_folder_icon(root: str, icon: str):
    """싱크루트 폴더 자체에 커스텀 아이콘(로고)을 입힌다(desktop.ini).
    위임 폴더 노드가 DefaultIcon 대신 대상 폴더 아이콘을 쓰는 경우까지 대비."""
    ini = os.path.join(root, "desktop.ini")
    try:
        with open(ini, "w", encoding="utf-8") as f:
            f.write("[.ShellClassInfo]\n")
            f.write(f"IconResource={icon},0\n")
            f.write(f"IconFile={icon}\nIconIndex=0\n")
        FILE_ATTRIBUTE_READONLY = 0x01
        FILE_ATTRIBUTE_HIDDEN = 0x02
        FILE_ATTRIBUTE_SYSTEM = 0x04
        k32 = ctypes.windll.kernel32
        k32.SetFileAttributesW(ini, FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
        # 폴더에 read-only 속성이 있으면 셸이 desktop.ini(커스텀 아이콘)를 읽는다.
        # (READONLY 는 폴더 자체의 쓰기 금지가 아니라 '사용자 지정됨' 표식일 뿐)
        k32.SetFileAttributesW(root, FILE_ATTRIBUTE_READONLY)
    except OSError:
        pass


def register_drive(root: str, icon: str, sid: str = None):
    """탐색기 사이드바에 'genDISK Drive' 브랜디드 드라이브 노드를 등록한다(관리자 불필요).

    icon 은 반드시 영구 로컬 경로(.ico)여야 한다 — PyInstaller onefile 의 _MEIPASS
    임시경로를 쓰면 종료 후 아이콘이 사라진다. 앱은 시작 시 아이콘을 %LOCALAPPDATA%
    아래로 복사하고 그 경로를 넘긴다.
    """
    sid = sid or current_sid()
    set_folder_icon(root, icon)
    # (1) HKCU 셸 네임스페이스 확장 CLSID (64/32비트) — 위임 폴더
    for croot in _CLSID_ROOTS:
        _write_clsid(croot + "\\" + NS_CLSID, root, icon)
    # (2) HKCU Desktop\NameSpace 에 얹기 + 데스크톱 아이콘 숨김
    _set(_HKCU, _EXPLORER + r"\Desktop\NameSpace" + "\\" + NS_CLSID, None, DISPLAY_NAME)
    _set(_HKCU, _EXPLORER + r"\HideDesktopIcons\NewStartPanel", NS_CLSID, 1, winreg.REG_DWORD)
    # (3) HKLM SyncRootManager (비관리자 쓰기 가능) — 클라우드 브랜딩 + 링크
    srid = sync_root_id(sid)
    srm = _SRM + "\\" + srid
    _set(_HKLM, srm, "NamespaceCLSID", NS_CLSID)
    _set(_HKLM, srm, "DisplayNameResource", DISPLAY_NAME, winreg.REG_EXPAND_SZ)
    _set(_HKLM, srm, "IconResource", icon + ",0", winreg.REG_EXPAND_SZ)
    _set(_HKLM, srm, "Flags", 0x22, winreg.REG_DWORD)
    _set(_HKLM, srm + r"\UserSyncRoots", sid, root)
    _refresh_shell()
    return srid


def unregister_drive(sid: str = None):
    sid = sid or current_sid()
    for croot in _CLSID_ROOTS:
        _del_tree(_HKCU, croot + "\\" + NS_CLSID)
    _del_tree(_HKCU, _EXPLORER + r"\Desktop\NameSpace" + "\\" + NS_CLSID)
    try:
        k = winreg.OpenKey(_HKCU, _EXPLORER + r"\HideDesktopIcons\NewStartPanel",
                           0, winreg.KEY_WRITE)
        try:
            winreg.DeleteValue(k, NS_CLSID)
        finally:
            winreg.CloseKey(k)
    except FileNotFoundError:
        pass
    _del_tree(_HKLM, _SRM + "\\" + sync_root_id(sid))
    _refresh_shell()


def _refresh_shell():
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
