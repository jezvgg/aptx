"""errors: exit codes, rendering and colour."""

from __future__ import annotations

import io

import pytest

from aptx import errors
from aptx.errors import (
    AptxError,
    CantCreateError,
    DataError,
    EnvError,
    NoInputError,
    OsError_,
    SoftwareError,
    UnavailableError,
    UsageError,
)


@pytest.fixture(autouse=True)
def reset_color():
    errors.set_color(True)
    yield
    errors.set_color(True)


class FakeStream:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestCodes:
    @pytest.mark.parametrize(
        "error, code",
        [
            (UsageError("x"), 2),
            (EnvError("x"), 64),
            (DataError("x"), 65),
            (NoInputError("x"), 66),
            (UnavailableError("x"), 69),
            (SoftwareError("x"), 70),
            (OsError_("x"), 71),
            (CantCreateError("x"), 73),
        ],
    )
    def test_class_default_codes(self, error, code):
        assert error.code == code

    def test_base_defaults_to_software(self):
        assert AptxError("x").code == errors.EX_SOFTWARE

    def test_explicit_code_wins(self):
        assert AptxError("x", code=42).code == 42

    def test_ok_is_zero(self):
        assert errors.EX_OK == 0


class TestRender:
    def test_message_only(self):
        assert UsageError("bad flag").render() == "aptx: error: bad flag"

    def test_with_hint(self):
        rendered = UsageError("bad flag", hint="try --help").render()
        assert rendered == "aptx: error: bad flag\naptx: hint: try --help"

    def test_message_is_the_exception_text(self):
        assert str(UsageError("boom")) == "boom"

    def test_colour_wraps_the_prefix(self):
        assert "\033[31m" in UsageError("boom").render(color=True)

    def test_plain_has_no_escapes(self):
        assert "\033" not in UsageError("boom").render(color=False)


class TestColourState:
    def test_enabled_for_a_tty(self):
        assert errors.color_enabled(FakeStream(True)) is True

    def test_disabled_for_a_pipe(self):
        assert errors.color_enabled(FakeStream(False)) is False

    def test_set_color_false_disables(self):
        errors.set_color(False)
        assert errors.color_enabled(FakeStream(True)) is False

    def test_set_color_true_enables(self):
        errors.set_color(True)
        assert errors.color_enabled(FakeStream(True)) is True

    def test_stream_without_isatty_is_not_a_tty(self):
        assert errors.color_enabled(io.StringIO()) is False

    def test_defaults_to_stderr(self, capsys):
        # capsys replaces stderr with a non-tty stream.
        assert errors.color_enabled() is False
