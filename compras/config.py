"""Settings from the environment, optionally loaded from a .env file (COMPRAS_ENV_FILE, default ./.env)."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    """KEY=VALUE lines; values already in the environment win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    gmail_user: str
    gmail_password: str
    data_dir: Path
    since: str           # Gmail date, YYYY/MM/DD

    @property
    def db_path(self) -> Path:
        return self.data_dir / "compras.db"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"


def settings() -> Settings:
    load_env_file(Path(os.environ.get("COMPRAS_ENV_FILE", ROOT / ".env")))
    return Settings(
        gmail_user=os.environ.get("GMAIL_USER", ""),
        gmail_password=os.environ.get("GMAIL_APP_PASSWORD", ""),
        data_dir=Path(os.environ.get("COMPRAS_DATA_DIR", ROOT / "data")),
        since=os.environ.get("COMPRAS_GMAIL_SINCE", "2025/01/01"),
    )
