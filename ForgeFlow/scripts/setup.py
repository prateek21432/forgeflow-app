"""Create local settings once. Existing configuration is never overwritten."""

import argparse
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
config = root / ".env"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--local", action="store_true", help="Use SQLite and trusted local execution, without Docker"
)
args = parser.parse_args()
if config.exists():
    print(".env already exists; keeping your configuration.")
else:
    password = secrets.token_urlsafe(24)
    database = (
        "sqlite:///platform-local.db"
        if args.local
        else f"postgresql+psycopg://forgeflow:{password}@127.0.0.1:55432/forgeflow"
    )
    mode = "local" if args.local else "docker"
    values = {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "POSTGRES_PASSWORD": password,
        "DATABASE_URL": database,
        "EXECUTION_MODE": mode,
    }
    lines = []
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        name = line.split("=", 1)[0]
        lines.append(f"{name}={values[name]}" if name in values else line)
    if args.local:
        lines.append("RUNTIME_ROOT=.runtime/local")
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Created private .env. Add your own provider key to LLM_API_KEY when you want AI generation.")
