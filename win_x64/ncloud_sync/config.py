"""설정 저장/불러오기 (%APPDATA%\\ncloud-sync\\config.json)."""
import json
import os
from dataclasses import asdict, dataclass, field


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "ncloud-sync")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


@dataclass
class Config:
    server_url: str = ""
    username: str = ""
    token: str = ""            # 세션 토큰 (비밀번호는 저장하지 않음)
    space: str = "home"
    local_folder: str = ""
    interval_sec: int = 30
    enabled: bool = False       # 자동 동기화 활성 여부

    @classmethod
    def load(cls) -> "Config":
        try:
            with open(config_path(), encoding="utf-8") as f:
                data = json.load(f)
            known = {k: data[k] for k in asdict(cls()) if k in data}
            return cls(**known)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self):
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    def is_ready(self) -> bool:
        return bool(self.server_url and self.token and self.local_folder)
