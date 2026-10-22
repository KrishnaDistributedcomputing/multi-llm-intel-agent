"""Local public/synthetic-data development commands.

Usage: uv run python -m agent.dev init|api|gateway|token
"""

import argparse
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path

import uvicorn

from agent.config import DEVELOPMENT_NOTICE, Settings
from agent.main import create_app

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".local" / "dev.env"
logger = logging.getLogger(__name__)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DEVELOPMENT_NOTICE)
    parser.add_argument("command", choices=["init", "api", "gateway", "token"])
    return parser


def initialize() -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ENV_FILE.exists():
        logger.info("Keeping existing development credentials at %s", ENV_FILE)
        return
    content = (
        f"AGENT_API_KEY={secrets.token_urlsafe(32)}\n"
        f"LITELLM_MASTER_KEY={secrets.token_urlsafe(32)}\n"
        "ALLOW_CLOUD=false\n"
        "GATEWAY_URL=http://127.0.0.1:4001\n"
        "OLLAMA_URL=http://127.0.0.1:11435\n"
    )
    descriptor = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
    logger.info("Created local-only development credentials at %s", ENV_FILE)


def development_settings() -> Settings:
    if not ENV_FILE.is_file():
        raise FileNotFoundError("Run 'python -m agent.dev init' first")
    return Settings(
        _env_file=ENV_FILE,
        allow_cloud=False,
        catalog_path=ROOT / "models.yaml",
        usage_db_path=ROOT / "data" / "usage.sqlite3",
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = create_parser().parse_args()
    logger.warning(DEVELOPMENT_NOTICE)
    try:
        if args.command == "init":
            initialize()
            return 0
        settings = development_settings()
        if args.command == "token":
            print(settings.agent_api_key.get_secret_value())
        elif args.command == "api":
            uvicorn.run(create_app(settings), host="127.0.0.1", port=8000, access_log=False)
        elif args.command == "gateway":
            env = os.environ.copy()
            env["LITELLM_MASTER_KEY"] = settings.litellm_master_key.get_secret_value()
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    "intel-agent-dev-gateway",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:uid=10001,gid=10001,mode=1770",
                    "-p",
                    "127.0.0.1:4001:4000",
                    "--mount",
                    f"type=bind,source={ROOT / 'models.yaml'},target=/app/models.yaml,readonly",
                    "-e",
                    "LITELLM_MASTER_KEY",
                    "-e",
                    "ALLOW_CLOUD=false",
                    "-e",
                    "OLLAMA_URL=http://host.docker.internal:11435",
                    "-e",
                    "LITELLM_LOCAL_MODEL_COST_MAP=True",
                    "-e",
                    "HF_HUB_OFFLINE=1",
                    "-e",
                    "HOME=/tmp",
                    "intel-agent-local/gateway:0.1.0",
                ],
                check=True,
                env=env,
            )
        return 0
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        logger.error("Development command failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
