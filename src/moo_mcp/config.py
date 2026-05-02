from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG_PATH = Path(
    os.environ.get("MOO_MCP_CONFIG")
    or Path.home() / ".config" / "moo-mcp" / "config.toml"
)


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    user: str
    password: str
    connect_command: str | None = None
    connect_timeout: float = 10.0
    command_timeout: float = 15.0
    allow_write: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or DEFAULT_CONFIG_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"config not found at {path}. "
                "Create it from config.example.toml."
            )
        data = tomllib.loads(path.read_text())
        moo = data.get("moo", data)
        return cls(
            host=moo["host"],
            port=int(moo["port"]),
            user=moo["user"],
            password=moo["password"],
            connect_command=moo.get("connect_command"),
            connect_timeout=float(moo.get("connect_timeout", 10.0)),
            command_timeout=float(moo.get("command_timeout", 15.0)),
            allow_write=bool(moo.get("allow-write", False)),
        )
