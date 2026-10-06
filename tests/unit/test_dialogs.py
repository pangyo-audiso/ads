"""Dialog table and screen predicates against the real Claude Code 2.1.289 fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from ads import dialogs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dialogs"
ALL = sorted(p.name for p in FIXTURES.glob("*.txt"))
OWNED = {f: d.name for d in dialogs.DIALOGS for f in d.fixtures}
NEGATIVE = ["idle_input.txt", "idle_input_startup.txt", "busy.txt", "pasted_pointer.txt",
            "agent_output_dialog_text.txt"]


def fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_every_fixture_is_classified() -> None:
    assert set(ALL) == set(OWNED) | set(NEGATIVE)
    assert {"trust", "bypass"} <= set(OWNED.values())


@pytest.mark.parametrize("name", sorted(OWNED))
def test_fixture_matches_only_its_own_entry(name: str) -> None:
    text = fx(name)
    hits = [d.name for d in dialogs.DIALOGS
            if (not d.needs_footer or dialogs.footer_last(text))
            and all(p.search("\n".join(dialogs.tail(text))) for p in d.detect)]
    assert hits == [OWNED[name]]
    assert dialogs.match_dialog(text).name == OWNED[name]
    assert not dialogs.has_input_box(text)
    assert not dialogs.is_unknown_dialog(text)
    assert not dialogs.is_busy(text)


@pytest.mark.parametrize("name", NEGATIVE)
def test_agent_screens_match_nothing(name: str) -> None:
    text = fx(name)
    assert dialogs.match_dialog(text) is None
    assert not dialogs.is_unknown_dialog(text)
    assert dialogs.has_input_box(text)


def test_negative_fixture_contains_dialog_text_but_is_not_a_dialog() -> None:
    text = fx("agent_output_dialog_text.txt")
    assert "Yes, I trust this folder" in text and dialogs.FOOTER in text
    # even without the input box, the footer is not the last line → no match
    no_box = "\n".join(ln for ln in text.splitlines() if not ln.startswith("❯ "))
    assert not dialogs.footer_last(no_box)


@pytest.mark.parametrize("name,selected", [("trust.txt", False), ("trust_selected.txt", True),
                                           ("bypass.txt", False), ("bypass_selected.txt", True)])
def test_accept_selected(name: str, selected: bool) -> None:
    text = fx(name)
    assert dialogs.accept_selected(dialogs.match_dialog(text), text) is selected


def test_input_box_glyph_is_nbsp_not_ascii() -> None:
    # transcript echoes and dialog markers use "❯ " (ASCII space) and are not the input box
    assert dialogs.has_input_box("─" * 30 + "\n❯ hello\n" + "─" * 30)
    assert not dialogs.has_input_box("─" * 30 + "\n❯ hello\n" + "─" * 30)
    assert not dialogs.has_input_box("some text\n❯ hello\n" + "─" * 30)  # no rule above


def test_input_box_text() -> None:
    assert dialogs.input_box_text(fx("idle_input.txt")) == ""
    assert dialogs.input_box_text(fx("pasted_pointer.txt")).startswith(
        "[ADS-MSG id=m-20261005-000001 from=orchestrator type=instruct]")
    assert dialogs.input_box_text(fx("trust.txt")) is None


@pytest.mark.parametrize("name", ALL)
def test_is_busy_only_for_busy_fixture(name: str) -> None:
    assert dialogs.is_busy(fx(name)) is (name == "busy.txt")


def test_spinner_line_alone_means_busy() -> None:
    rule = "─" * 40
    screen = f"● hi\n\n✶ Coalescing… (3s)\n\n{rule}\n❯ \n{rule}\n  ⏵⏵ bypass permissions on"
    assert dialogs.is_busy(screen)
    done = screen.replace("✶ Coalescing… (3s)", "✻ Sautéed for 2s · done 6:52 PM")
    assert not dialogs.is_busy(done)


def test_unknown_dialog() -> None:
    screen = (" Something new:\n\n ❯ Do it\n   Don't\n\n Enter to confirm · Esc to cancel\n\n\n")
    assert dialogs.match_dialog(screen) is None
    assert dialogs.is_unknown_dialog(screen)
    # a known dialog is never "unknown"
    assert not dialogs.is_unknown_dialog(fx("trust.txt"))


def test_dialog_text_scrolled_above_the_tail_is_ignored() -> None:
    text = fx("trust.txt").rstrip("\n") + "\n" + "\n".join(f"line {i}" for i in range(20))
    assert dialogs.match_dialog(text) is None and not dialogs.is_unknown_dialog(text)


def test_theme_entry_is_tolerant_and_has_no_fixture() -> None:
    theme = dialogs.BY_NAME["theme"]
    assert theme.fixtures == () and theme.accept is None and theme.keys == ("Enter",)
    screen = (" Let's get started.\n\n Choose the text style that looks best with your terminal\n\n"
              " ❯ 1. Dark mode ✔\n   2. Light mode\n")
    assert dialogs.match_dialog(screen) is theme
