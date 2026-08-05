"""탐색기에 genDISK FTP 서버를 '네트워크 위치'로 등록한다.

온디맨드(Cloud Files) 방식과 달리 **로컬에 목록·파일을 전혀 두지 않는다**.
탐색기가 FTP 서버를 직접 읽고 쓰므로(윈도우 기본 FTP 지원), 진짜 서버를
보는 것과 동일하게 동작한다 — 열 때마다 서버의 현재 상태.

구현: '내 PC'의 네트워크 위치는 %APPDATA%\\Microsoft\\Windows\\Network Shortcuts\\
<이름>\\target.lnk (URL 을 가리키는 셸 링크)다. 탐색기의 '네트워크 위치 추가'
마법사가 만드는 것과 같은 구조를 COM(IShellLink) 으로 직접 만든다.
"""
import ctypes
import os
import shutil
import subprocess
from ctypes import POINTER, byref, c_void_p
from ctypes.wintypes import DWORD, LPCWSTR

HRESULT = ctypes.c_long          # wintypes 에 없다(파이썬 버전별) — 직접 정의

ole32 = ctypes.OleDLL("ole32")

CLSID_ShellLink = "{00021401-0000-0000-C000-000000000046}"
IID_IShellLinkW = "{000214F9-0000-0000-C000-000000000046}"
IID_IPersistFile = "{0000010B-0000-0000-C000-000000000046}"
CLSCTX_INPROC_SERVER = 1


class GUID(ctypes.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        ole32.CLSIDFromString(LPCWSTR(s), byref(self))


def network_shortcuts_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Network Shortcuts")


def _create_link(url: str, lnk_path: str, description: str = ""):
    """URL 을 가리키는 셸 링크(.lnk) 생성 — IShellLinkW + IPersistFile."""
    ole32.CoInitialize(None)
    try:
        ptr = c_void_p()
        ole32.CoCreateInstance(byref(GUID(CLSID_ShellLink)), None,
                               CLSCTX_INPROC_SERVER, byref(GUID(IID_IShellLinkW)),
                               byref(ptr))
        vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents

        def call(index, restype, argtypes, *args):
            fn = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])
            return fn(ptr, *args)

        # IShellLinkW: 20=SetPath, 7=SetDescription (IUnknown 3개 이후 배치)
        call(20, HRESULT, [LPCWSTR], url)
        if description:
            call(7, HRESULT, [LPCWSTR], description)

        pf = c_void_p()
        call(0, HRESULT, [c_void_p, POINTER(c_void_p)],
             byref(GUID(IID_IPersistFile)), byref(pf))       # QueryInterface
        pvt = ctypes.cast(pf, POINTER(POINTER(c_void_p))).contents
        save = ctypes.WINFUNCTYPE(HRESULT, c_void_p, LPCWSTR, ctypes.c_int)(pvt[6])
        save(pf, lnk_path, 1)                                 # IPersistFile::Save
        for p, v in ((pf, pvt), (ptr, vtbl)):
            ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(v[2])(p)   # Release
    finally:
        ole32.CoUninitialize()


def ftp_url(host: str, port: int, username: str = "") -> str:
    """탐색기에 넣을 FTP 주소. 아이디를 넣어두면 매번 묻지 않는다(비밀번호는 미포함)."""
    hostpart = f"{host}:{port}" if port and port != 21 else host
    return f"ftp://{username}@{hostpart}/" if username else f"ftp://{hostpart}/"


def add(host: str, port: int, username: str = "", name: str = "genDISK") -> str:
    """탐색기 '내 PC'에 네트워크 위치를 만든다. 만들어진 폴더 경로 반환."""
    folder = os.path.join(network_shortcuts_dir(), name)
    os.makedirs(folder, exist_ok=True)
    _create_link(ftp_url(host, port, username),
                 os.path.join(folder, "target.lnk"), f"{name} (FTP)")
    return folder


def remove(name: str = "genDISK") -> bool:
    folder = os.path.join(network_shortcuts_dir(), name)
    if not os.path.isdir(folder):
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return not os.path.isdir(folder)


def exists(name: str = "genDISK") -> bool:
    return os.path.isfile(os.path.join(network_shortcuts_dir(), name, "target.lnk"))


def open_in_explorer(host: str, port: int, username: str = ""):
    """탐색기로 FTP 주소를 연다 (지금 바로 보기)."""
    subprocess.Popen(["explorer.exe", ftp_url(host, port, username)])
