from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

SITE_FIELD_SPECS: dict[str, list[tuple[str, str]]] = {
    "aha": [("base_url", "BASE_URL"), ("username", "USERNAME"), ("password", "PASSWORD")],
    "em": [
        ("base_url", "BASE_URL"),
        ("journal_code", "JOURNAL_CODE"),
        ("username", "USERNAME"),
        ("password", "PASSWORD"),
    ],
    "bmc": [("submission_url", "SUBMISSION_URL"), ("username", "USERNAME"), ("password", "PASSWORD")],
}

DEFAULT_OUTPUT_VALUES = {
    "OUTPUT_DIR": "outputs",
    "STORE_JSON_NAME": "statuses.json",
    "CURRENT_CSV_NAME": "statuses.csv",
    "REFRESH_LOG_NAME": "refresh_log.csv",
}

DEFAULT_NOTIFICATION_VALUES = {
    "SMTP_HOST": "",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "SMTP_FROM": "",
    "SMTP_TO": "",
    "SMTP_USE_TLS": "true",
    "SMTP_USE_SSL": "false",
    "WECOM_WEBHOOK_URL": "",
}

DEFAULT_NETWORK_VALUES = {
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "NO_PROXY": "",
}

NOTIFICATION_KEYS = list(DEFAULT_NOTIFICATION_VALUES)
OUTPUT_KEYS = list(DEFAULT_OUTPUT_VALUES)
NETWORK_KEYS = list(DEFAULT_NETWORK_VALUES)
SITE_ENV_PREFIXES = ("AHA_", "EM_", "BMC_")


def collect_site_configs(
    site_key: str,
    env_specs: list[tuple[str, str]] | None = None,
    env_map: Mapping[str, str] | None = None,
) -> list[tuple[str, dict[str, str]]]:
    prefix = site_key.upper()
    env_specs = env_specs or SITE_FIELD_SPECS[site_key]
    env_map = env_map or os.environ
    valid_suffixes = {suffix for _, suffix in env_specs}
    indexed_pattern = re.compile(rf"^{prefix}_(\d+)_([A-Z0-9_]+)$")
    indexed_values: dict[int, dict[str, str]] = {}

    for env_name, raw_value in env_map.items():
        match = indexed_pattern.match(env_name)
        if not match:
            continue
        suffix = match.group(2)
        if suffix not in valid_suffixes:
            continue
        value = raw_value.strip()
        if not value:
            continue
        index = int(match.group(1))
        indexed_values.setdefault(index, {})[suffix] = value

    if indexed_values:
        configs: list[tuple[str, dict[str, str]]] = []
        for index in sorted(indexed_values):
            raw_config = indexed_values[index]
            config: dict[str, str] = {}
            missing: list[str] = []
            for param_name, env_suffix in env_specs:
                value = raw_config.get(env_suffix, "").strip()
                if not value:
                    missing.append(f"{prefix}_{index}_{env_suffix}")
                config[param_name] = value
            if missing:
                raise ValueError(f"Incomplete {prefix}_{index} config. Missing: {', '.join(missing)}")
            configs.append((f"{site_key}_{index}", config))
        return configs

    legacy_config: dict[str, str] = {}
    legacy_present = False
    missing_legacy: list[str] = []
    for param_name, env_suffix in env_specs:
        env_name = f"{prefix}_{env_suffix}"
        value = env_map.get(env_name, "").strip()
        if value:
            legacy_present = True
        else:
            missing_legacy.append(env_name)
        legacy_config[param_name] = value

    if legacy_present:
        if missing_legacy:
            raise ValueError(f"Incomplete {prefix} config. Missing: {', '.join(missing_legacy)}")
        return [(site_key, legacy_config)]

    return []


def require_site_configs(
    site_key: str,
    env_specs: list[tuple[str, str]] | None = None,
    env_map: Mapping[str, str] | None = None,
) -> list[tuple[str, dict[str, str]]]:
    env_specs = env_specs or SITE_FIELD_SPECS[site_key]
    configs = collect_site_configs(site_key, env_specs=env_specs, env_map=env_map)
    if configs:
        return configs
    prefix = site_key.upper()
    legacy_names = ", ".join(f"{prefix}_{env_suffix}" for _, env_suffix in env_specs)
    indexed_names = ", ".join(f"{prefix}_1_{env_suffix}" for _, env_suffix in env_specs)
    raise ValueError(f"No {prefix} config found. Use legacy names ({legacy_names}) or indexed names ({indexed_names}).")


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def write_env_file(path: Path, env_map: Mapping[str, str]) -> None:
    merged = {**DEFAULT_OUTPUT_VALUES, **DEFAULT_NOTIFICATION_VALUES, **DEFAULT_NETWORK_VALUES, **env_map}
    lines: list[str] = [
        "# Managed by journalcheck GUI",
        "# Numbered configs are recommended. Example: AHA_1_*, EM_1_*, BMC_1_*",
        "",
    ]

    for site_key in ("aha", "em", "bmc"):
        prefix = site_key.upper()
        specs = SITE_FIELD_SPECS[site_key]
        configs = collect_site_configs(site_key, env_specs=specs, env_map=merged)
        for site_name, config in configs:
            index = site_name.split("_", 1)[1] if "_" in site_name else "1"
            for param_name, env_suffix in specs:
                lines.append(f"{prefix}_{index}_{env_suffix}={config.get(param_name, '')}")
            lines.append("")

    for key in OUTPUT_KEYS:
        lines.append(f"{key}={merged.get(key, DEFAULT_OUTPUT_VALUES[key])}")
    lines.append("")

    for key in NOTIFICATION_KEYS:
        lines.append(f"{key}={merged.get(key, DEFAULT_NOTIFICATION_VALUES[key])}")
    lines.append("")

    for key in NETWORK_KEYS:
        lines.append(f"{key}={merged.get(key, DEFAULT_NETWORK_VALUES[key])}")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def apply_env_map(env_map: Mapping[str, str]) -> None:
    for key in list(os.environ):
        if key.startswith(("AHA_", "EM_", "BMC_", "SMTP_", "WECOM_")) or key in OUTPUT_KEYS or key in NETWORK_KEYS:
            os.environ.pop(key, None)
    for key, value in env_map.items():
        if key.startswith(("AHA_", "EM_", "BMC_", "SMTP_", "WECOM_")) or key in OUTPUT_KEYS or key in NETWORK_KEYS:
            if value.strip():
                os.environ[key] = value.strip()


def build_env_map(site_configs: dict[str, list[dict[str, str]]], extras: Mapping[str, str] | None = None) -> dict[str, str]:
    env_map = {**DEFAULT_OUTPUT_VALUES, **DEFAULT_NOTIFICATION_VALUES, **DEFAULT_NETWORK_VALUES}
    extras = extras or {}
    for site_key, configs in site_configs.items():
        prefix = site_key.upper()
        specs = SITE_FIELD_SPECS[site_key]
        for index, config in enumerate(configs, start=1):
            for param_name, env_suffix in specs:
                env_map[f"{prefix}_{index}_{env_suffix}"] = config.get(param_name, "").strip()
    for key, value in extras.items():
        if key.startswith(SITE_ENV_PREFIXES):
            continue
        env_map[key] = value.strip()
    return env_map
