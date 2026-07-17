"""genDISK Drive — Windows Cloud Files(cldapi) 온디맨드 가상 파일시스템 프로바이더.

%USERPROFILE%\\genDISK 를 싱크루트로 등록하고, 서버 파일을 "플레이스홀더"(디스크상
0바이트, 크기만 표시)로 심는다. 탐색기에서 파일을 열면 Windows 가 FETCH_DATA 콜백을
호출 → 서버에서 실제 바이트를 받아 CfExecute(TRANSFER_DATA) 로 채운다(하이드레이션).

콜백 객체(CF_CALLBACK)는 반드시 살아 있어야 한다(GC 되면 콜백 스레드가 죽는다) →
Provider 인스턴스 속성으로 붙잡아 둔다.
"""
import ctypes
import json
import os
from ctypes import POINTER, byref

from . import cfapi as C

SECTOR = 4096
CHUNK = 1 << 20  # 1 MiB (4KB 배수)


def _now_filetime() -> int:
    ft = C.LARGE_INTEGER(0)
    ctypes.windll.kernel32.GetSystemTimeAsFileTime(byref(ft))
    return ft.value


def set_expose_placeholders():
    """이 프로세스가 플레이스홀더를 '플레이스홀더로' 보게 한다(프로바이더 필수)."""
    C.RtlSetProcessPlaceholderCompatibilityMode(bytes([C.PHCM_EXPOSE_PLACEHOLDERS]))


class Provider:
    def __init__(self, root: str, provider_guid: str, fetch_range, list_dir=None,
                 list_spaces=None, space: str = "home", provider_name: str = "genDISK",
                 identity: bytes = b"genDISK", log=print):
        self.root = os.path.abspath(root)
        self.provider_guid = provider_guid
        self.provider_name = provider_name
        self.identity = identity
        self.fetch_range = fetch_range      # (meta:dict, offset:int, length:int) -> bytes
        self.list_dir = list_dir            # (space:str, rel_posix:str) -> [entry dict]
        self.list_spaces = list_spaces      # () -> [{id,name,readonly}] (다중 저장소 모드)
        self.space = space
        self.log = log
        self._space_map = {}                # 폴더이름 -> space id (다중 저장소)
        # 볼륨 상대 루트 경로(콜백의 NormalizedPath 는 드라이브 문자 없는 볼륨 상대 경로)
        self._root_volrel = os.path.splitdrive(self.root)[1]
        self.conn_key = None
        self._connected = False
        # GC 방지용 참조 보관
        self._cb_fetch_data = None
        self._cb_fetch_ph = None
        self._cb_table = None
        self._reg_idbuf = None

    # ------------------------------------------------------------------ 등록
    def register(self):
        reg = C.CF_SYNC_REGISTRATION()
        reg.StructSize = ctypes.sizeof(C.CF_SYNC_REGISTRATION)
        reg.ProviderName = self.provider_name
        reg.ProviderVersion = "1.0"
        idbuf = ctypes.create_string_buffer(self.identity, len(self.identity))
        self._reg_idbuf = idbuf  # 호출 동안 살아 있어야 함
        reg.SyncRootIdentity = ctypes.cast(idbuf, C.LPCVOID)
        reg.SyncRootIdentityLength = len(self.identity)
        reg.FileIdentity = None
        reg.FileIdentityLength = 0
        reg.ProviderId = C.GUID(self.provider_guid)

        pol = C.CF_SYNC_POLICIES()
        pol.StructSize = ctypes.sizeof(C.CF_SYNC_POLICIES)
        pol.Hydration.Primary = C.CF_HYDRATION_POLICY_PROGRESSIVE
        pol.Hydration.Modifier = C.CF_HYDRATION_POLICY_MODIFIER_NONE
        pol.Population.Primary = C.CF_POPULATION_POLICY_FULL
        pol.Population.Modifier = C.CF_POPULATION_POLICY_MODIFIER_NONE
        pol.InSync = C.CF_INSYNC_POLICY_NONE
        pol.HardLink = C.CF_HARDLINK_POLICY_NONE
        pol.PlaceholderManagement = C.CF_PLACEHOLDER_MANAGEMENT_POLICY_DEFAULT

        hr = C.CfRegisterSyncRoot(self.root, byref(reg), byref(pol),
                                  C.CF_REGISTER_FLAG_UPDATE)
        if not C.hr_ok(hr):
            raise OSError(f"CfRegisterSyncRoot 실패 {C.hr_str(hr)}")
        self.log(f"[vfs] registered sync root: {self.root}")

    def unregister(self):
        hr = C.CfUnregisterSyncRoot(self.root)
        self.log(f"[vfs] unregister -> {C.hr_str(hr)}")

    # ------------------------------------------------------------------ 연결
    def connect(self):
        regs = (C.CF_CALLBACK_REGISTRATION * 3)()
        self._cb_fetch_data = C.CF_CALLBACK(self._on_fetch_data)
        self._cb_fetch_ph = C.CF_CALLBACK(self._on_fetch_placeholders)
        regs[0].Type = C.CF_CALLBACK_TYPE_FETCH_DATA
        regs[0].Callback = self._cb_fetch_data
        regs[1].Type = C.CF_CALLBACK_TYPE_FETCH_PLACEHOLDERS
        regs[1].Callback = self._cb_fetch_ph
        regs[2].Type = C.CF_CALLBACK_TYPE_NONE            # 종료 표식
        regs[2].Callback = C.CF_CALLBACK()               # NULL
        self._cb_table = regs

        conn = C.CF_CONNECTION_KEY()
        hr = C.CfConnectSyncRoot(
            self.root, regs, None,
            C.CF_CONNECT_FLAG_REQUIRE_FULL_FILE_PATH,
            byref(conn))
        if not C.hr_ok(hr):
            raise OSError(f"CfConnectSyncRoot 실패 {C.hr_str(hr)}")
        self.conn_key = conn
        self._connected = True
        self.log(f"[vfs] connected (key={conn.Internal:#x})")
        # 루트는 실제 폴더라 자동 FETCH_PLACEHOLDERS 가 안 온다 → 최상위를 즉시 채운다.
        # 하위 폴더는 플레이스홀더 디렉터리라 열릴 때 FETCH_PLACEHOLDERS 로 채워진다.
        if self.list_dir is not None:
            try:
                self.populate_root()
            except Exception as e:  # noqa: BLE001
                self.log(f"[vfs] populate_root error: {e!r}")

    def _space_entries(self):
        """다중 저장소 모드: 접근 가능한 저장소들을 최상위 폴더 항목으로. 매핑도 갱신."""
        spaces = self.list_spaces() or []
        self._space_map = {}
        out = []
        for s in spaces:
            name = (s.get("name") or s["id"]).strip() or s["id"]
            # 폴더명에 못 쓰는 문자 정리
            for ch in '\\/:*?"<>|':
                name = name.replace(ch, "_")
            if name in self._space_map:            # 이름 충돌 방지
                name = f"{name} ({s['id']})"
            self._space_map[name] = s["id"]
            out.append({"name": name, "path": "", "is_dir": True, "_space": s["id"]})
        return out

    def _children_for(self, rel: str):
        """드라이브 상대경로 rel(posix)의 자식 항목 목록(각 항목에 _space 주입)."""
        if self.list_spaces is not None:
            if rel == "":
                return self._space_entries()
            if not self._space_map:
                self._space_entries()              # 재시작 후 매핑 복구
            parts = rel.split("/")
            sid = self._space_map.get(parts[0])
            if sid is None:
                return []
            subpath = "/".join(parts[1:])
            raw = self.list_dir(sid, subpath) if self.list_dir else []
            return [dict(e, _space=sid) for e in raw]
        # 단일 저장소 모드
        raw = self.list_dir(self.space, rel) if self.list_dir else []
        return [dict(e, _space=self.space) for e in raw]

    def populate_root(self):
        """드라이브 루트를 플레이스홀더로 심는다(다중 저장소면 저장소 폴더들, 아니면 최상위 파일)."""
        entries = self._children_for("")
        existing = set(os.listdir(self.root)) if os.path.isdir(self.root) else set()
        fresh = [e for e in entries if e["name"] not in existing]
        if not fresh:
            self.log(f"[vfs] populate_root: nothing new ({len(entries)} entries)")
            return
        arr, keep = self._build_placeholders(fresh)  # noqa: F841
        processed = C.DWORD(0)
        hr = C.CfCreatePlaceholders(self.root, arr, len(fresh),
                                    C.CF_CREATE_FLAG_NONE, byref(processed))
        if not C.hr_ok(hr):
            raise OSError(f"CfCreatePlaceholders(root) {C.hr_str(hr)}")
        self.log(f"[vfs] populate_root: seeded {processed.value}/{len(fresh)}")

    def disconnect(self):
        if self._connected and self.conn_key is not None:
            hr = C.CfDisconnectSyncRoot(self.conn_key)
            self.log(f"[vfs] disconnect -> {C.hr_str(hr)}")
            self._connected = False

    # -------------------------------------------------------------- 플레이스홀더
    def seed(self, items):
        """items: [{'name': str, 'size': int, 'identity': dict}] — 루트에 심는다."""
        n = len(items)
        if n == 0:
            return 0
        arr = (C.CF_PLACEHOLDER_CREATE_INFO * n)()
        keep = []
        now = _now_filetime()
        for i, it in enumerate(items):
            ci = arr[i]
            ci.RelativeFileName = it["name"]
            keep.append(it["name"])
            ci.FsMetadata.FileSize = int(it["size"])
            bi = ci.FsMetadata.BasicInfo
            bi.CreationTime = now
            bi.LastAccessTime = now
            bi.LastWriteTime = now
            bi.ChangeTime = now
            bi.FileAttributes = C.FILE_ATTRIBUTE_NORMAL
            idjson = json.dumps(it["identity"]).encode("utf-8")
            idbuf = ctypes.create_string_buffer(idjson, len(idjson))
            keep.append(idbuf)
            ci.FileIdentity = ctypes.cast(idbuf, C.LPCVOID)
            ci.FileIdentityLength = len(idjson)
            ci.Flags = C.CF_PLACEHOLDER_CREATE_FLAG_MARK_IN_SYNC
        processed = C.DWORD(0)
        hr = C.CfCreatePlaceholders(self.root, arr, n, C.CF_CREATE_FLAG_NONE,
                                    byref(processed))
        if not C.hr_ok(hr):
            # 개별 항목 결과도 찍어 원인 파악
            for i in range(n):
                self.log(f"[vfs] seed[{i}] {items[i]['name']} -> {C.hr_str(arr[i].Result)}")
            raise OSError(f"CfCreatePlaceholders 실패 {C.hr_str(hr)}")
        self.log(f"[vfs] seeded {processed.value}/{n} placeholders")
        return processed.value

    # ------------------------------------------------------------------ 콜백
    def _on_fetch_data(self, info_p, params_p):
        req_off = req_len = 0
        info = None
        try:
            info = info_p[0]
            fdp = ctypes.cast(params_p, POINTER(C.FETCH_DATA_PARAMS))[0]
            req_off = int(fdp.FileOffset)
            req_len = int(fdp.RequiredLength)
            file_size = int(info.FileSize)
            meta = {}
            if info.FileIdentity and info.FileIdentityLength:
                raw = ctypes.string_at(info.FileIdentity, info.FileIdentityLength)
                meta = json.loads(raw.decode("utf-8"))
            path = info.NormalizedPath or ""
            self.log(f"[vfs] FETCH_DATA {path} off={req_off} len={req_len} "
                     f"size={file_size} meta={meta}")
            self._hydrate(info.ConnectionKey, info.TransferKey,
                          meta, file_size, req_off, req_len)
        except Exception as e:  # noqa: BLE001
            self.log(f"[vfs] FETCH_DATA error: {e!r}")
            if info is not None:
                try:
                    self._transfer_fail(info.ConnectionKey, info.TransferKey,
                                        req_off, req_len)
                except Exception as e2:  # noqa: BLE001
                    self.log(f"[vfs] fail-report error: {e2!r}")

    def _hydrate(self, conn, xfer, meta, file_size, req_off, req_len):
        start = req_off - (req_off % SECTOR)
        req_end = req_off + req_len
        end = min(file_size, ((req_end + SECTOR - 1) // SECTOR) * SECTOR)
        if end <= start:
            end = min(file_size, start + SECTOR)
        pos = start
        while pos < end:
            want = min(CHUNK, end - pos)
            data = self.fetch_range(meta, pos, want)
            if not data:
                raise IOError(f"빈 응답 off={pos} want={want}")
            self._transfer(conn, xfer, pos, data)
            pos += len(data)

    def _transfer(self, conn, xfer, offset, data: bytes):
        op = C.CF_OPERATION_INFO()
        op.StructSize = ctypes.sizeof(C.CF_OPERATION_INFO)
        op.Type = C.CF_OPERATION_TYPE_TRANSFER_DATA
        op.ConnectionKey = conn
        op.TransferKey = xfer
        p = C.TRANSFER_DATA_PARAMS()
        p.ParamSize = ctypes.sizeof(C.TRANSFER_DATA_PARAMS)
        p.Flags = C.CF_OPERATION_TRANSFER_DATA_FLAG_NONE
        p.CompletionStatus = C.STATUS_SUCCESS
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        p.Buffer = ctypes.cast(buf, C.LPCVOID)
        p.Offset = offset
        p.Length = len(data)
        hr = C.CfExecute(byref(op), ctypes.cast(byref(p), C.LPCVOID))
        if not C.hr_ok(hr):
            raise OSError(f"CfExecute(TRANSFER_DATA) {C.hr_str(hr)}")

    def _transfer_fail(self, conn, xfer, offset, length):
        """하이드레이션 실패를 Windows 에 알려 열기가 매달리지 않게 한다."""
        op = C.CF_OPERATION_INFO()
        op.StructSize = ctypes.sizeof(C.CF_OPERATION_INFO)
        op.Type = C.CF_OPERATION_TYPE_TRANSFER_DATA
        op.ConnectionKey = conn
        op.TransferKey = xfer
        p = C.TRANSFER_DATA_PARAMS()
        p.ParamSize = ctypes.sizeof(C.TRANSFER_DATA_PARAMS)
        p.Flags = C.CF_OPERATION_TRANSFER_DATA_FLAG_NONE
        p.CompletionStatus = C.STATUS_UNSUCCESSFUL
        p.Buffer = None
        p.Offset = offset
        p.Length = max(0, length)
        C.CfExecute(byref(op), ctypes.cast(byref(p), C.LPCVOID))

    # 콜백의 NormalizedPath(볼륨 상대) → 서버 상대 경로(posix)
    def _rel_from_normalized(self, normp: str) -> str:
        p = (normp or "").replace("/", "\\")
        base = self._root_volrel
        if p.lower().startswith(base.lower()):
            p = p[len(base):]
        return p.strip("\\").replace("\\", "/")

    def _build_placeholders(self, entries):
        """서버 목록 → CF_PLACEHOLDER_CREATE_INFO 배열 + (호출 동안 살릴) keepalive."""
        n = len(entries)
        arr = (C.CF_PLACEHOLDER_CREATE_INFO * n)()
        keep = []
        now = _now_filetime()
        for i, e in enumerate(entries):
            ci = arr[i]
            name = e["name"]
            ci.RelativeFileName = name
            keep.append(name)
            is_dir = bool(e.get("is_dir"))
            bi = ci.FsMetadata.BasicInfo
            bi.CreationTime = bi.LastAccessTime = bi.LastWriteTime = bi.ChangeTime = now
            bi.FileAttributes = (C.FILE_ATTRIBUTE_DIRECTORY if is_dir
                                 else C.FILE_ATTRIBUTE_NORMAL)
            ci.FsMetadata.FileSize = 0 if is_dir else int(e.get("size") or 0)
            ident = json.dumps({"space": e.get("_space", self.space), "path": e["path"],
                                "dir": is_dir}).encode("utf-8")
            idbuf = ctypes.create_string_buffer(ident, len(ident))
            keep.append(idbuf)
            ci.FileIdentity = ctypes.cast(idbuf, C.LPCVOID)
            ci.FileIdentityLength = len(ident)
            # 파일: in-sync(디하이드레이트 상태). 디렉터리: in-sync 표시하면 "이미 채워짐"으로
            # 간주돼 FETCH_PLACEHOLDERS 가 안 온다 → 디렉터리는 표시하지 않아 온디맨드로 채운다.
            ci.Flags = (C.CF_PLACEHOLDER_CREATE_FLAG_NONE if is_dir
                        else C.CF_PLACEHOLDER_CREATE_FLAG_MARK_IN_SYNC)
        return arr, keep

    def _on_fetch_placeholders(self, info_p, params_p):
        """폴더를 열면 서버에서 자식 목록을 받아 플레이스홀더로 채운다(온디맨드)."""
        info = None
        try:
            info = info_p[0]
            rel = self._rel_from_normalized(info.NormalizedPath)
            entries = self._children_for(rel)
            self.log(f"[vfs] FETCH_PLACEHOLDERS dir='{rel}' -> {len(entries)} entries")
            arr, keep = self._build_placeholders(entries)  # noqa: F841 (keep alive)
            op = C.CF_OPERATION_INFO()
            op.StructSize = ctypes.sizeof(C.CF_OPERATION_INFO)
            op.Type = C.CF_OPERATION_TYPE_TRANSFER_PLACEHOLDERS
            op.ConnectionKey = info.ConnectionKey
            op.TransferKey = info.TransferKey
            p = C.TRANSFER_PLACEHOLDERS_PARAMS()
            p.ParamSize = ctypes.sizeof(C.TRANSFER_PLACEHOLDERS_PARAMS)
            p.Flags = C.CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAG_DISABLE_ON_DEMAND_POPULATION
            p.PlaceholderTotalCount = len(entries)
            p.PlaceholderArray = ctypes.cast(arr, C.LPVOID) if entries else None
            p.PlaceholderCount = len(entries)
            p.EntriesProcessed = 0
            hr = C.CfExecute(byref(op), ctypes.cast(byref(p), C.LPCVOID))
            if not C.hr_ok(hr):
                self.log(f"[vfs] TRANSFER_PLACEHOLDERS -> {C.hr_str(hr)}")
        except Exception as e:  # noqa: BLE001
            self.log(f"[vfs] FETCH_PLACEHOLDERS error: {e!r}")
            if info is not None:
                try:
                    op = C.CF_OPERATION_INFO()
                    op.StructSize = ctypes.sizeof(C.CF_OPERATION_INFO)
                    op.Type = C.CF_OPERATION_TYPE_TRANSFER_PLACEHOLDERS
                    op.ConnectionKey = info.ConnectionKey
                    op.TransferKey = info.TransferKey
                    p = C.TRANSFER_PLACEHOLDERS_PARAMS()
                    p.ParamSize = ctypes.sizeof(C.TRANSFER_PLACEHOLDERS_PARAMS)
                    p.Flags = C.CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAG_DISABLE_ON_DEMAND_POPULATION
                    p.EntriesProcessed = 0
                    C.CfExecute(byref(op), ctypes.cast(byref(p), C.LPCVOID))
                except Exception:
                    pass
