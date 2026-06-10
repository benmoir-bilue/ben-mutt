from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from dataclasses import dataclass, field


CONFIG_DIR = Path(os.environ.get("BEM_CONFIG_DIR", Path.home() / ".config" / "bem"))
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
CONFIG_FILE = CONFIG_DIR / "config.toml"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
]


@dataclass
class Config:
    editor: str = field(default_factory=lambda: os.environ.get("EDITOR", "vim"))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    threads_per_page: int = 50
    realname: str = ""
    from_address: str = ""
    theme: str = "dark"
    sort_threads: str = "date"  # date | from | subject
    safe_mode: bool = True
    ai_model_fast: str = "claude-haiku-4-5-20251001"   # triage, summarise, explain
    ai_model_smart: str = "claude-sonnet-4-6"           # reply-draft, custom

    @classmethod
    def load(cls) -> Config:
        if not CONFIG_FILE.exists():
            return cls()
        with open(CONFIG_FILE, "rb") as f:
            data = tomllib.load(f)
        obj = cls()
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        return obj

    def save(self) -> None:
        """Write the config as TOML. Rewrites the whole file (comments are lost).

        Values that merely mirror environment variables are not persisted, so
        the API key never lands on disk unless the user put it there.
        """
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = dict(self.__dict__)
        if data.get("anthropic_api_key") == os.environ.get("ANTHROPIC_API_KEY"):
            data.pop("anthropic_api_key", None)
        if data.get("editor") == os.environ.get("EDITOR"):
            data.pop("editor", None)
        lines = [f"{k} = {_toml_value(v)}\n" for k, v in data.items()]
        CONFIG_FILE.write_text("".join(lines))
        CONFIG_FILE.chmod(0o600)

    def ensure_config_dir(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    # JSON string escaping is a valid TOML basic string for our purposes
    return json.dumps(str(v))
