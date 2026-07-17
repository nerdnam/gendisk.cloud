"""genDISK Drive 컨트롤러 — 브랜디드 온디맨드 드라이브의 수명주기 관리.

vfs.Provider(CfAPI 온디맨드) 를 서버 클라이언트에 연결하고, navdrive(탐색기 노드)를
등록/해제한다. 트레이 앱이 소유하며, 콜백(list_dir/fetch_range)은 세션 만료 시 자동
재로그인 후 1회 재시도한다. 관리자 권한/서명/패키지 불필요(navdrive 참고).
"""
import os
import shutil
import threading

from . import navdrive, vfs
from .client import AuthError, GenDiskClient
from .icon import icon_path

# CfAPI ProviderId (고정)
PROVIDER_GUID = "{61B70D09-051E-4A68-87A3-F6DD4A72F9C0}"


def stable_icon_path() -> str:
    """번들 아이콘(.ico)을 %LOCALAPPDATA%\\genDISK 로 복사하고 그 영구 경로를 돌려준다.
    레지스트리에 넣는 아이콘 경로는 앱 종료 후에도 살아있어야 하므로(onefile 의 _MEIPASS
    임시경로 금지) 영구 위치로 복사한다."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "genDISK")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "gendisk-icon.ico")
    try:
        src = icon_path()
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
    except OSError:
        pass
    return dst


def _log_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "genDISK", "drive.log")


class DriveController:
    def __init__(self, cfg, on_reauth=None, log=print):
        """cfg: Config. on_reauth: () -> bool (재로그인 시도, 성공 시 cfg.token 갱신)."""
        self.cfg = cfg
        self.on_reauth = on_reauth
        self._applog = log
        self.log = self._make_log(log)
        self.provider = None
        self._lock = threading.Lock()

    def _make_log(self, applog):
        """GUI 로그 + 파일 로그(%LOCALAPPDATA%\\genDISK\\drive.log) 동시 기록(진단용)."""
        path = _log_path()

        def _log(msg):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(str(msg) + "\n")
            except OSError:
                pass
            try:
                applog(msg)
            except Exception:
                pass
        return _log

    def _list_spaces(self):
        return self._with_reauth(lambda c: c.spaces())

    # --- 서버 호출 (세션 만료 시 1회 재로그인 후 재시도) ---
    def _client(self):
        return GenDiskClient(self.cfg.server_url, self.cfg.token)

    def _with_reauth(self, fn):
        try:
            return fn(self._client())
        except AuthError:
            if self.on_reauth and self.on_reauth():
                return fn(self._client())
            raise

    def _list_dir(self, space, rel):
        return self._with_reauth(lambda c: c.list_dir(space, rel))

    def _fetch_range(self, meta, offset, length):
        return self._with_reauth(
            lambda c: c.download_range(meta["space"], meta["path"], offset, length))

    # --- 수명주기 ---
    @property
    def running(self) -> bool:
        return self.provider is not None

    def start(self):
        """싱크루트 등록 + 연결 + 최상위 채우기 + 탐색기 노드 등록. (네트워크 → 스레드에서 호출)"""
        with self._lock:
            if self.provider is not None:
                return
            root = self.cfg.vfs_root_path()
            os.makedirs(root, exist_ok=True)
            icon = stable_icon_path()
            vfs.set_expose_placeholders()
            prov = vfs.Provider(root, PROVIDER_GUID, self._fetch_range,
                                list_dir=self._list_dir, list_spaces=self._list_spaces,
                                space=self.cfg.space, log=self.log)
            prov.register()
            prov.connect()                 # 내부에서 populate_root()
            navdrive.register_drive(root, icon)
            self.provider = prov
            self.log(f"[drive] genDISK Drive 연결됨: {root}")

    def stop(self, remove_node: bool = False):
        """provider 연결 해제. remove_node=True 면 탐색기 노드+싱크루트도 제거."""
        with self._lock:
            if self.provider is not None:
                try:
                    self.provider.disconnect()
                except Exception as e:  # noqa: BLE001
                    self.log(f"[drive] disconnect: {e}")
                self.provider = None
            if remove_node:
                try:
                    navdrive.unregister_drive()
                except Exception as e:  # noqa: BLE001
                    self.log(f"[drive] unregister node: {e}")
                try:
                    vfs.C.CfUnregisterSyncRoot(self.cfg.vfs_root_path())
                except Exception:
                    pass
            self.log("[drive] genDISK Drive 해제됨")
