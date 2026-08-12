from __future__ import annotations


DEFAULTS = {
    "host": "127.0.0.1",
    "port": "7437",
    "log_level": "INFO",
}


def merge_config(
    toml_values: dict[str, str | None],
    dotenv_values: dict[str, str | None],
    environ: dict[str, str | None],
) -> dict[str, str]:
    """Merge config sources into a new dictionary."""
    merged = dict(DEFAULTS)
    for source in (toml_values, dotenv_values, environ):
        for key, value in source.items():
            if value is not None:
                merged[key] = value
    return merged
