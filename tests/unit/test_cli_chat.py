from kama_claude.cli.commands.chat import _contains_surrogate


def test_contains_surrogate_rejects_surrogateescaped_terminal_input() -> None:
    assert _contains_surrogate("hello\udcff") is True


def test_contains_surrogate_accepts_normal_unicode() -> None:
    assert _contains_surrogate("我们前面做了什么？") is False
