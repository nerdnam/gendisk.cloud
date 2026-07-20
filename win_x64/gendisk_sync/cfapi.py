"""cldapi.dll (Windows Cloud Filter API) ctypes 바인딩.

Windows SDK cfapi.h 를 필드 단위로 옮겼다. 64-bit(x64) 기준. 중요한 구조체는 크기를
sizeof 로 검증한다(_assert_sizes). 값/오프셋이 하나라도 틀리면 CfExecute 등이 조용히
메모리를 깨뜨리므로 반드시 맞춘다.

이 파일은 "타입/enum/프로토타입"만 정의한다. 등록/연결/하이드레이션 로직은 vfs.py.
"""
import ctypes
from ctypes import POINTER, wintypes

cldapi = ctypes.WinDLL("cldapi.dll")
ntdll = ctypes.WinDLL("ntdll.dll")

# ---------------------------------------------------------------------------
# 기본 타입
# ---------------------------------------------------------------------------
HRESULT = ctypes.c_long
LONG = ctypes.c_long
NTSTATUS = ctypes.c_long
ULONG = ctypes.c_ulong
USHORT = ctypes.c_ushort
UCHAR = ctypes.c_ubyte
DWORD = wintypes.DWORD
LARGE_INTEGER = ctypes.c_longlong
LPCWSTR = ctypes.c_wchar_p
LPCVOID = ctypes.c_void_p
LPVOID = ctypes.c_void_p


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, s: str = None):
        super().__init__()
        if s:
            s = s.strip("{}")
            p = s.split("-")
            self.Data1 = int(p[0], 16)
            self.Data2 = int(p[1], 16)
            self.Data3 = int(p[2], 16)
            rest = bytes.fromhex(p[3] + p[4])
            for i in range(8):
                self.Data4[i] = rest[i]


# cldapi 의 opaque 8바이트 키들 (모두 struct { LONGLONG Internal; })
class CF_CONNECTION_KEY(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_longlong)]


class CF_TRANSFER_KEY(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_longlong)]


class CF_REQUEST_KEY(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_longlong)]


# ---------------------------------------------------------------------------
# 등록 enum / 구조체
# ---------------------------------------------------------------------------
CF_REGISTER_FLAG_NONE = 0x00000000
CF_REGISTER_FLAG_UPDATE = 0x00000001
CF_REGISTER_FLAG_DISABLE_ON_DEMAND_POPULATION_ON_ROOT = 0x00000002
CF_REGISTER_FLAG_MARK_IN_SYNC_ON_ROOT = 0x00000004

CF_HYDRATION_POLICY_PARTIAL = 0
CF_HYDRATION_POLICY_PROGRESSIVE = 1
CF_HYDRATION_POLICY_FULL = 2
CF_HYDRATION_POLICY_ALWAYS_FULL = 3
CF_HYDRATION_POLICY_MODIFIER_NONE = 0x0000
CF_HYDRATION_POLICY_MODIFIER_VALIDATION_REQUIRED = 0x0001
CF_HYDRATION_POLICY_MODIFIER_STREAMING_ALLOWED = 0x0002
CF_HYDRATION_POLICY_MODIFIER_AUTO_DEHYDRATION_ALLOWED = 0x0004
CF_HYDRATION_POLICY_MODIFIER_ALLOW_FULL_RESTART_HYDRATION = 0x0008

CF_POPULATION_POLICY_PARTIAL = 0
CF_POPULATION_POLICY_FULL = 2
CF_POPULATION_POLICY_ALWAYS_FULL = 3
CF_POPULATION_POLICY_MODIFIER_NONE = 0x0000

CF_INSYNC_POLICY_NONE = 0x00000000
CF_HARDLINK_POLICY_NONE = 0x00000000
CF_HARDLINK_POLICY_ALLOWED = 0x00000001
CF_PLACEHOLDER_MANAGEMENT_POLICY_DEFAULT = 0x00000000


class CF_HYDRATION_POLICY(ctypes.Structure):
    _fields_ = [("Primary", USHORT), ("Modifier", USHORT)]


class CF_POPULATION_POLICY(ctypes.Structure):
    _fields_ = [("Primary", USHORT), ("Modifier", USHORT)]


class CF_SYNC_POLICIES(ctypes.Structure):
    _fields_ = [
        ("StructSize", ULONG),
        ("Hydration", CF_HYDRATION_POLICY),
        ("Population", CF_POPULATION_POLICY),
        ("InSync", ULONG),
        ("HardLink", ULONG),
        ("PlaceholderManagement", ULONG),
    ]


class CF_SYNC_REGISTRATION(ctypes.Structure):
    _fields_ = [
        ("StructSize", ULONG),
        ("ProviderName", LPCWSTR),
        ("ProviderVersion", LPCWSTR),
        ("SyncRootIdentity", LPCVOID),
        ("SyncRootIdentityLength", ULONG),
        ("FileIdentity", LPCVOID),
        ("FileIdentityLength", ULONG),
        ("ProviderId", GUID),
    ]


# ---------------------------------------------------------------------------
# 연결(Connect) enum / 콜백
# ---------------------------------------------------------------------------
CF_CONNECT_FLAG_NONE = 0x00000000
CF_CONNECT_FLAG_REQUIRE_PROCESS_INFO = 0x00000002
CF_CONNECT_FLAG_REQUIRE_FULL_FILE_PATH = 0x00000004
CF_CONNECT_FLAG_BLOCK_SELF_IMPLICIT_HYDRATION = 0x00000008

CF_CALLBACK_TYPE_FETCH_DATA = 0
CF_CALLBACK_TYPE_VALIDATE_DATA = 1
CF_CALLBACK_TYPE_CANCEL_FETCH_DATA = 2
CF_CALLBACK_TYPE_FETCH_PLACEHOLDERS = 3
CF_CALLBACK_TYPE_CANCEL_FETCH_PLACEHOLDERS = 4
CF_CALLBACK_TYPE_OPEN_COMPLETION = 5
CF_CALLBACK_TYPE_CLOSE_COMPLETION = 6
CF_CALLBACK_TYPE_DEHYDRATE = 7
CF_CALLBACK_TYPE_DEHYDRATE_COMPLETION = 8
CF_CALLBACK_TYPE_DELETE = 9
CF_CALLBACK_TYPE_DELETE_COMPLETION = 10
CF_CALLBACK_TYPE_RENAME = 11
CF_CALLBACK_TYPE_RENAME_COMPLETION = 12
CF_CALLBACK_TYPE_NONE = 0xFFFFFFFF


# CF_CALLBACK_INFO — 콜백에서 넘어오는 정보. ConnectionKey/TransferKey/FileIdentity/
# FileSize/RequestKey 를 읽어 CfExecute 로 되돌려주므로 오프셋이 정확해야 한다.
# FileIdentity/SyncRootIdentity 는 헤더상 PCWSTR 이지만 실제로는 우리가 넣은 불투명
# 바이트 blob 이라 c_void_p 로 받아 string_at 로 읽는다.
class CF_CALLBACK_INFO(ctypes.Structure):
    _fields_ = [
        ("StructSize", ULONG),
        ("ConnectionKey", CF_CONNECTION_KEY),
        ("CallbackContext", LPVOID),
        ("VolumeGuidName", LPCWSTR),
        ("VolumeDosName", LPCWSTR),
        ("VolumeSerialNumber", DWORD),
        ("SyncRootFileId", LARGE_INTEGER),
        ("SyncRootIdentity", LPCVOID),
        ("SyncRootIdentityLength", ULONG),
        ("FileId", LARGE_INTEGER),
        ("FileSize", LARGE_INTEGER),
        ("FileIdentity", LPCVOID),
        ("FileIdentityLength", ULONG),
        ("NormalizedPath", LPCWSTR),
        ("TransferKey", CF_TRANSFER_KEY),
        ("PriorityHint", UCHAR),
        ("ProcessInfo", LPVOID),          # const CF_PROCESS_INFO*
        ("RequestKey", CF_REQUEST_KEY),
    ]


# CF_CALLBACK 시그니처: void CALLBACK(const CF_CALLBACK_INFO*, const CF_CALLBACK_PARAMETERS*)
CF_CALLBACK = ctypes.WINFUNCTYPE(None, POINTER(CF_CALLBACK_INFO), LPCVOID)


class CF_CALLBACK_REGISTRATION(ctypes.Structure):
    _fields_ = [("Type", DWORD), ("Callback", CF_CALLBACK)]


# --- 콜백 파라미터 오버레이 (union → 필요한 멤버만) ---
# CF_CALLBACK_PARAMETERS = { ULONG ParamSize; union{...}; }  union 은 8정렬 → ParamSize 뒤 4패딩
class FETCH_DATA_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ParamSize", ULONG),
        ("_pad0", ULONG),
        ("Flags", DWORD),
        ("_pad1", ULONG),
        ("FileOffset", LARGE_INTEGER),
        ("RequiredLength", LARGE_INTEGER),
        ("OptionalOffset", LARGE_INTEGER),
        ("OptionalLength", LARGE_INTEGER),
        ("LastDehydrationTime", LARGE_INTEGER),
        ("LastDehydrationReason", DWORD),
    ]


class FETCH_PLACEHOLDERS_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ParamSize", ULONG),
        ("_pad0", ULONG),
        ("Flags", DWORD),
        ("_pad1", ULONG),
        ("Pattern", LPCWSTR),
    ]


# RENAME_COMPLETION 콜백 파라미터: { Flags(DWORD); PCWSTR SourcePath; }
# 레이아웃은 FETCH_PLACEHOLDERS_PARAMS 와 동일(끝에 LPCWSTR). SourcePath = 이름변경 전 경로.
class RENAME_COMPLETION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ParamSize", ULONG),
        ("_pad0", ULONG),
        ("Flags", DWORD),
        ("_pad1", ULONG),
        ("SourcePath", LPCWSTR),
    ]


# ---------------------------------------------------------------------------
# 플레이스홀더 생성
# ---------------------------------------------------------------------------
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_DIRECTORY = 0x00000010

CF_PLACEHOLDER_CREATE_FLAG_NONE = 0x00
CF_PLACEHOLDER_CREATE_FLAG_DISABLE_ON_DEMAND_POPULATION = 0x01
CF_PLACEHOLDER_CREATE_FLAG_MARK_IN_SYNC = 0x02
CF_PLACEHOLDER_CREATE_FLAG_SUPERSEDE = 0x04
CF_PLACEHOLDER_CREATE_FLAG_ALWAYS_FULL = 0x08

CF_CREATE_FLAG_NONE = 0x00000000
CF_CREATE_FLAG_STOP_ON_ERROR = 0x00000001


class FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [
        ("CreationTime", LARGE_INTEGER),
        ("LastAccessTime", LARGE_INTEGER),
        ("LastWriteTime", LARGE_INTEGER),
        ("ChangeTime", LARGE_INTEGER),
        ("FileAttributes", DWORD),
    ]


class CF_FS_METADATA(ctypes.Structure):
    _fields_ = [
        ("BasicInfo", FILE_BASIC_INFO),
        ("FileSize", LARGE_INTEGER),
    ]


class CF_PLACEHOLDER_CREATE_INFO(ctypes.Structure):
    _fields_ = [
        ("RelativeFileName", LPCWSTR),
        ("FsMetadata", CF_FS_METADATA),
        ("FileIdentity", LPCVOID),
        ("FileIdentityLength", ULONG),
        ("Flags", DWORD),
        ("Result", HRESULT),
        ("CreateUsn", LARGE_INTEGER),
    ]


# ---------------------------------------------------------------------------
# CfExecute (하이드레이션 완료 시 데이터 전송)
# ---------------------------------------------------------------------------
CF_OPERATION_TYPE_TRANSFER_DATA = 0
CF_OPERATION_TYPE_RETRIEVE_DATA = 1
CF_OPERATION_TYPE_ACK_DATA = 2
CF_OPERATION_TYPE_RESTART_HYDRATION = 3
CF_OPERATION_TYPE_TRANSFER_PLACEHOLDERS = 4
CF_OPERATION_TYPE_ACK_DEHYDRATE = 5
CF_OPERATION_TYPE_ACK_DELETE = 6
CF_OPERATION_TYPE_ACK_RENAME = 7

CF_OPERATION_TRANSFER_DATA_FLAG_NONE = 0x00000000
STATUS_SUCCESS = 0
STATUS_UNSUCCESSFUL = -1073741823  # 0xC0000001

# HRESULT_FROM_WIN32(ERROR_CLOUD_FILE_REQUEST_CANCELED) — 앱이 하이드레이션을 취소함
# (썸네일 추출 종료·파일 닫힘 등). 오류가 아니라 정상 흐름이므로 조용히 중단해야 한다.
E_CLOUD_FILE_REQUEST_CANCELED = 0x8007018E


def is_canceled(hr: int) -> bool:
    return (hr & 0xFFFFFFFF) == E_CLOUD_FILE_REQUEST_CANCELED


class CF_OPERATION_INFO(ctypes.Structure):
    _fields_ = [
        ("StructSize", ULONG),
        ("Type", DWORD),
        ("ConnectionKey", CF_CONNECTION_KEY),
        ("TransferKey", CF_TRANSFER_KEY),
        ("RequestKey", CF_REQUEST_KEY),
        ("CorrelationVector", LPVOID),
        ("SyncStatus", LPVOID),
    ]


# CF_OPERATION_PARAMETERS 의 TransferData 오버레이 (union → TransferData 만)
# ParamSize = FIELD_OFFSET(.., TransferData) + sizeof(TransferData substruct) = 8 + 32 = 40
class TRANSFER_DATA_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ParamSize", ULONG),
        ("_pad0", ULONG),
        ("Flags", DWORD),
        ("CompletionStatus", NTSTATUS),
        ("Buffer", LPCVOID),
        ("Offset", LARGE_INTEGER),
        ("Length", LARGE_INTEGER),
    ]


CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAG_NONE = 0x00000000
CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAG_STOP_ON_ERROR = 0x00000001
CF_OPERATION_TRANSFER_PLACEHOLDERS_FLAG_DISABLE_ON_DEMAND_POPULATION = 0x00000002


# CF_OPERATION_PARAMETERS 의 TransferPlaceholders 오버레이
class TRANSFER_PLACEHOLDERS_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ParamSize", ULONG),
        ("_pad0", ULONG),
        ("Flags", DWORD),
        ("_pad1", ULONG),
        ("PlaceholderTotalCount", LARGE_INTEGER),
        ("PlaceholderArray", LPVOID),
        ("PlaceholderCount", ULONG),
        ("EntriesProcessed", ULONG),
    ]


# ---------------------------------------------------------------------------
# 프로토타입
# ---------------------------------------------------------------------------
CfRegisterSyncRoot = cldapi.CfRegisterSyncRoot
CfRegisterSyncRoot.restype = HRESULT
CfRegisterSyncRoot.argtypes = [LPCWSTR, POINTER(CF_SYNC_REGISTRATION),
                               POINTER(CF_SYNC_POLICIES), DWORD]

CfUnregisterSyncRoot = cldapi.CfUnregisterSyncRoot
CfUnregisterSyncRoot.restype = HRESULT
CfUnregisterSyncRoot.argtypes = [LPCWSTR]

CfConnectSyncRoot = cldapi.CfConnectSyncRoot
CfConnectSyncRoot.restype = HRESULT
CfConnectSyncRoot.argtypes = [LPCWSTR, POINTER(CF_CALLBACK_REGISTRATION),
                              LPCVOID, DWORD, POINTER(CF_CONNECTION_KEY)]

CfDisconnectSyncRoot = cldapi.CfDisconnectSyncRoot
CfDisconnectSyncRoot.restype = HRESULT
CfDisconnectSyncRoot.argtypes = [CF_CONNECTION_KEY]   # by value

CfCreatePlaceholders = cldapi.CfCreatePlaceholders
CfCreatePlaceholders.restype = HRESULT
CfCreatePlaceholders.argtypes = [LPCWSTR, POINTER(CF_PLACEHOLDER_CREATE_INFO),
                                 DWORD, DWORD, POINTER(DWORD)]

CfExecute = cldapi.CfExecute
CfExecute.restype = HRESULT
CfExecute.argtypes = [POINTER(CF_OPERATION_INFO), LPCVOID]  # params: byref overlay

# ---------------------------------------------------------------------------
# 로컬→원격 업로드용: 실제 파일을 in-sync 플레이스홀더로 변환("동기화 보류중" 해소)
# ---------------------------------------------------------------------------
CF_CONVERT_FLAG_NONE = 0x00000000
CF_CONVERT_FLAG_MARK_IN_SYNC = 0x00000001
CF_CONVERT_FLAG_DEHYDRATE = 0x00000002

# HRESULT CfConvertToPlaceholder(HANDLE, LPCVOID FileIdentity, DWORD, CF_CONVERT_FLAGS,
#                                USN* ConvertUsn(opt), LPOVERLAPPED(opt))
CfConvertToPlaceholder = cldapi.CfConvertToPlaceholder
CfConvertToPlaceholder.restype = HRESULT
CfConvertToPlaceholder.argtypes = [wintypes.HANDLE, LPCVOID, DWORD, DWORD,
                                   POINTER(LARGE_INTEGER), LPVOID]

# HRESULT CfDehydratePlaceholder(HANDLE, LARGE_INTEGER StartOffset, LARGE_INTEGER Length,
#                                CF_DEHYDRATE_FLAGS, LPOVERLAPPED)  — Length=-1: 파일 전체
CF_DEHYDRATE_FLAG_NONE = 0x00000000
CfDehydratePlaceholder = cldapi.CfDehydratePlaceholder
CfDehydratePlaceholder.restype = HRESULT
CfDehydratePlaceholder.argtypes = [wintypes.HANDLE, LARGE_INTEGER, LARGE_INTEGER,
                                   DWORD, LPVOID]

# 파일 속성+reparse 태그로 플레이스홀더 상태를 판정(핸들 불필요). 업로드 스캔에서
# '우리 파일(in-sync)'과 '아직 서버에 없는(신규/보류) 파일'을 구분하는 데 쓴다.
CF_PLACEHOLDER_STATE_NO_STATES = 0x00000000
CF_PLACEHOLDER_STATE_PLACEHOLDER = 0x00000001
CF_PLACEHOLDER_STATE_IN_SYNC = 0x00000008
CF_PLACEHOLDER_STATE_INVALID = 0xFFFFFFFF
CfGetPlaceholderStateFromAttributeTag = cldapi.CfGetPlaceholderStateFromAttributeTag
CfGetPlaceholderStateFromAttributeTag.restype = DWORD    # CF_PLACEHOLDER_STATE
CfGetPlaceholderStateFromAttributeTag.argtypes = [DWORD, DWORD]

FILE_ATTRIBUTE_OFFLINE = 0x00001000        # 디하이드레이트(로컬 데이터 없음) — 드롭 파일 아님

# HRESULT CfUpdatePlaceholder(HANDLE, CF_FS_METADATA*(opt), LPCVOID FileIdentity, DWORD,
#   CF_FILE_RANGE*(opt), DWORD, CF_UPDATE_FLAGS, USN*(opt), LPOVERLAPPED(opt))
# 이름변경 후 FileIdentity(서버 경로)를 새 경로로 갱신 + MARK_IN_SYNC 하는 데 쓴다.
CF_UPDATE_FLAG_NONE = 0x00000000
CF_UPDATE_FLAG_MARK_IN_SYNC = 0x00000001
CfUpdatePlaceholder = cldapi.CfUpdatePlaceholder
CfUpdatePlaceholder.restype = HRESULT
CfUpdatePlaceholder.argtypes = [wintypes.HANDLE, LPCVOID, LPCVOID, DWORD,
                                LPCVOID, DWORD, DWORD, POINTER(LARGE_INTEGER), LPVOID]

# kernel32: 파일 핸들 열기/닫기 (CfConvertToPlaceholder 는 쓰기 가능한 핸들이 필요)
kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
CreateFileW = kernel32.CreateFileW
CreateFileW.restype = wintypes.HANDLE
CreateFileW.argtypes = [LPCWSTR, DWORD, DWORD, LPVOID, DWORD, DWORD, wintypes.HANDLE]
CloseHandle = kernel32.CloseHandle
CloseHandle.restype = wintypes.BOOL
CloseHandle.argtypes = [wintypes.HANDLE]

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# CHAR RtlSetProcessPlaceholderCompatibilityMode(CHAR Mode)
RtlSetProcessPlaceholderCompatibilityMode = ntdll.RtlSetProcessPlaceholderCompatibilityMode
RtlSetProcessPlaceholderCompatibilityMode.restype = ctypes.c_char
RtlSetProcessPlaceholderCompatibilityMode.argtypes = [ctypes.c_char]
PHCM_APPLICATION_DEFAULT = 0
PHCM_DISGUISE_PLACEHOLDER = 1
PHCM_EXPOSE_PLACEHOLDERS = 2


def _assert_sizes():
    s = ctypes.sizeof
    assert s(GUID) == 16, s(GUID)
    assert s(CF_CONNECTION_KEY) == 8
    assert s(CF_SYNC_POLICIES) == 24, s(CF_SYNC_POLICIES)
    assert s(CF_SYNC_REGISTRATION) == 72, s(CF_SYNC_REGISTRATION)
    assert s(CF_CALLBACK_INFO) == 144, s(CF_CALLBACK_INFO)
    assert s(CF_CALLBACK_REGISTRATION) == 16, s(CF_CALLBACK_REGISTRATION)
    assert s(FILE_BASIC_INFO) == 40, s(FILE_BASIC_INFO)
    assert s(CF_FS_METADATA) == 48, s(CF_FS_METADATA)
    assert s(CF_PLACEHOLDER_CREATE_INFO) == 88, s(CF_PLACEHOLDER_CREATE_INFO)
    assert s(CF_OPERATION_INFO) == 48, s(CF_OPERATION_INFO)
    assert s(TRANSFER_DATA_PARAMS) == 40, s(TRANSFER_DATA_PARAMS)


_assert_sizes()


def hr_ok(hr: int) -> bool:
    return hr >= 0


def hr_str(hr: int) -> str:
    return f"0x{hr & 0xFFFFFFFF:08X}"
