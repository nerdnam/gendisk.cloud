"""ncloud 서버와 통신하는 HTTP 클라이언트 (표준 라이브러리만 사용, 외부 의존성 없음).

로그인은 세션 쿠키를 발급하는데, 그 쿠키 값이 곧 세션 토큰이다. 값을 추출해
이후 요청에 Authorization: Bearer 로 실어 보낸다 (서버가 쿠키/Bearer 둘 다 허용).
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class AuthError(Exception):
    """세션 만료·인증 실패 (재로그인 필요)."""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


class NCloudClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---------- 저수준 요청 ----------
    def _request(self, method: str, path: str, *, params=None, json_body=None,
                 data: bytes | None = None, content_type: str | None = None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            return resp
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            if e.code == 401:
                raise AuthError(detail)
            raise ApiError(e.code, detail)

    def _json(self, method, path, **kw):
        resp = self._request(method, path, **kw)
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    # ---------- 인증 ----------
    def login(self, username: str, password: str) -> str:
        """로그인해 세션 토큰을 얻는다. 성공 시 self.token 설정 후 토큰 반환."""
        url = self.base_url + "/api/auth/login"
        body = json.dumps({"username": username, "password": password}).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise AuthError(detail)
        # Set-Cookie: ncloud_session=<token>; ... 에서 토큰 추출
        token = None
        for key, value in resp.getheaders():
            if key.lower() == "set-cookie" and value.startswith("ncloud_session="):
                token = value.split(";", 1)[0].split("=", 1)[1]
                break
        if not token:
            raise AuthError("세션 토큰을 받지 못했습니다")
        self.token = token
        return token

    def status(self) -> dict:
        return self._json("GET", "/api/auth/status")

    # ---------- 저장소 ----------
    def spaces(self) -> list[dict]:
        return self._json("GET", "/api/files/spaces")["spaces"]

    def usage(self) -> dict:
        return self._json("GET", "/api/files/usage")

    # ---------- 동기화 ----------
    def enumerate(self, space: str, path: str = "") -> dict:
        return self._json("GET", "/api/sync/enumerate",
                          params={"space": space, "path": path})

    def download(self, space: str, path: str) -> bytes:
        resp = self._request("GET", "/api/files/download",
                            params={"space": space, "path": path})
        return resp.read()

    def put(self, space: str, path: str, data: bytes) -> dict:
        return self._json("POST", "/api/sync/put",
                         params={"space": space, "path": path},
                         data=data, content_type="application/octet-stream")

    def mkdir(self, space: str, path: str):
        try:
            self._json("POST", "/api/files/mkdir",
                      json_body={"path": path, "space": space})
        except ApiError as e:
            if e.status != 409:  # 이미 존재하면 무시
                raise

    def delete(self, space: str, path: str):
        try:
            self._json("POST", "/api/files/delete",
                      json_body={"path": path, "space": space})
        except ApiError as e:
            if e.status != 404:  # 이미 없으면 무시
                raise
