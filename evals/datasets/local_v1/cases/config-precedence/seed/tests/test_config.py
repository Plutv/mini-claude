from app.config import merge_config


def test_process_environment_has_highest_priority() -> None:
    result = merge_config(
        {"port": "7000"},
        {"port": "7100"},
        {"port": "7200"},
    )
    assert result["port"] == "7200"


def test_none_does_not_erase_lower_priority_value() -> None:
    result = merge_config({"host": "toml-host"}, {"host": None}, {})
    assert result["host"] == "toml-host"
