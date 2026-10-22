"""Build a fail-closed LiteLLM configuration, then start the gateway."""

import os
from pathlib import Path

import yaml

from agent.config import CONTEXT_TOKENS, Catalog, GatewaySettings, load_catalog


def build_config(settings: GatewaySettings, catalog: Catalog) -> dict[str, object]:
    if settings.deployment_profile == "federal-review":
        raise ValueError("The inference gateway cannot run in the federal-review profile")
    models: list[dict[str, object]] = []
    for route in catalog.models:
        if not route.enabled or (route.kind == "cloud" and not settings.allow_cloud):
            continue
        params: dict[str, object] = {"model": route.model, "timeout": 120, "num_retries": 0}
        if route.kind == "local":
            params.update(
                {
                    "api_base": settings.ollama_url,
                    "num_ctx": CONTEXT_TOKENS,
                    "num_gpu": 0,
                    "use_mmap": True,
                }
            )
            if route.ollama_model.startswith("qwen3:"):
                params["think"] = False
        else:
            if not route.key_env or not os.environ.get(route.key_env):
                raise ValueError(f"Missing credential for enabled route {route.id}")
            if "REPLACE_WITH" in route.model or "REPLACE_WITH" in (route.api_base or ""):
                raise ValueError(f"Configure the deployment/endpoint for {route.id}")
            params["api_key"] = f"os.environ/{route.key_env}"
            if route.api_base:
                params["api_base"] = route.api_base
            if route.api_version:
                params["api_version"] = route.api_version
        models.append(
            {
                "model_name": route.id,
                "litellm_params": params,
                "model_info": {"supports_function_calling": True},
            }
        )
    return {
        "model_list": models,
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "disable_spend_logs": True,
        },
        "litellm_settings": {
            "telemetry": False,
            "turn_off_message_logging": True,
            "drop_params": False,
            "request_timeout": 120,
        },
        "router_settings": {"num_retries": 0},
    }


def main() -> None:
    settings = GatewaySettings()
    config = build_config(settings, load_catalog(settings.catalog_path))
    path = Path("/tmp/litellm.yaml")
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    path.chmod(0o600)
    os.execvp(
        "litellm",
        [
            "litellm",
            "--config",
            str(path),
            "--host",
            "0.0.0.0",
            "--port",
            "4000",
            "--num_workers",
            "1",
        ],
    )


if __name__ == "__main__":
    main()
