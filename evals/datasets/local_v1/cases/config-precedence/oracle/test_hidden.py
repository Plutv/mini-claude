from app.config import DEFAULTS, merge_config


def test_each_source_overrides_only_lower_priority_sources() -> None:
    result = merge_config(
        {"host": "toml", "port": "7000"},
        {"host": "dotenv"},
        {"log_level": "DEBUG"},
    )
    assert result == {"host": "dotenv", "port": "7000", "log_level": "DEBUG"}


def test_inputs_and_defaults_are_not_mutated() -> None:
    toml_values = {"port": "7000"}
    dotenv_values = {"port": "7100"}
    environ = {"port": "7200"}
    defaults_before = dict(DEFAULTS)
    merge_config(toml_values, dotenv_values, environ)
    assert toml_values == {"port": "7000"}
    assert dotenv_values == {"port": "7100"}
    assert environ == {"port": "7200"}
    assert DEFAULTS == defaults_before
