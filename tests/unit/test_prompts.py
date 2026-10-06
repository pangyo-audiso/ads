"""M6 role prompts: every agent renders cleanly, carries the bus contract and its role duties."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ads import launcher
from ads.config import load_config
from ads.paths import AGENTS


@pytest.fixture
def cfg(tmp_runtime: Path):
    return load_config(runtime=tmp_runtime)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture
def rendered(cfg, tmp_runtime: Path, project: Path) -> dict[str, str]:
    out = {}
    for agent in AGENTS:
        _, prompt_path = launcher.render_agent_files(cfg, tmp_runtime, project, agent)
        out[agent] = prompt_path.read_text(encoding="utf-8")
    return out


ROLE_PHRASES: dict[str, list[str]] = {
    "orchestrator": ["plan", "dev", "test", "--supersede", "verbatim", "--to human",
                     "one phase per human instruction", "Evaluator: PASSED"],
    "planner": ["evaluator", "--type review-request", "/plan/drafts/", "/plan/<YYYY-MM-DD>-<slug>.md",
                "evaluator_status", "review_rounds", "last_review", "Evaluator review",
                "exactly what the evaluator reviewed"],
    "evaluator": ["/work/reviews/<slug>.v<N>.md", "--type review", "--result revise", "Omissions",
                  "Conflicts", "Ordering", "Testability"],
    "developer": ["coder-1", "coder-2", "never", "One open task per coder", "disjoint",
                  "/docs/dev-<YYYY-MM-DD>-<slug>.md", ".git", "Always report"],
    "coder-1": ["developer", "Never", "git commit", "Changed files", "--result partial"],
    "coder-2": ["developer", "Never", "git commit", "Changed files", "--result partial"],
    "tester": ["/docs/test-<YYYY-MM-DD>-<slug>.md", "reproduction command", "pass/fail"],
}


@pytest.mark.parametrize("agent", AGENTS)
def test_no_unresolved_placeholders(rendered, agent) -> None:
    text = rendered[agent]
    left = re.findall(r"\{(" + "|".join(launcher.PLACEHOLDERS) + r")\}", text)
    assert not left, f"{agent}: unresolved {set(left)}"


@pytest.mark.parametrize("agent", AGENTS)
def test_common_contract(rendered, agent, cfg, tmp_runtime: Path, project: Path) -> None:
    text = rendered[agent]
    bin_ = str(launcher.ads_bin(tmp_runtime))
    assert Path(bin_).is_absolute()
    assert f"{bin_} send" in text
    assert f"{bin_} note" in text
    assert f"You are **{agent}**" in text
    assert f"You report to **{cfg.agents[agent].reports_to}**" in text
    assert f"{tmp_runtime}/work/agents/{agent}/" in text
    assert str(project) in text
    assert "[ADS-MSG" in text and "SUPERSEDES" in text
    assert "--body-file" in text and "--parent" in text and "--result failure" in text
    assert "AskUserQuestion" in text and "Lab Notes" in text
    assert f"| {agent} (you) |" in text


@pytest.mark.parametrize("agent", AGENTS)
def test_role_section_present(rendered, agent) -> None:
    text = rendered[agent]
    assert f"## Role: {launcher.role_of(agent)}" in text
    for phrase in ROLE_PHRASES[agent]:
        assert phrase in text, f"{agent} prompt lacks {phrase!r}"


def test_max_review_rounds_from_config(rendered, cfg) -> None:
    assert f"**{cfg.protocol.max_review_rounds}** review rounds" in rendered["planner"]


def test_no_tmux_key_bindings(rendered) -> None:
    for agent, text in rendered.items():
        assert "send-keys" not in text and "C-a" not in text, agent


def test_override_precedence(cfg, tmp_runtime: Path, project: Path) -> None:
    over = tmp_runtime / ".claude/ads/prompts"
    over.mkdir(parents=True)
    (over / "planner.md").write_text("## Role: planner\nOVERRIDDEN planner for {agent} via {ads_bin}\n")
    _, prompt_path = launcher.render_agent_files(cfg, tmp_runtime, project, "planner")
    text = prompt_path.read_text()
    assert f"OVERRIDDEN planner for planner via {launcher.ads_bin(tmp_runtime)}" in text
    assert "/plan/drafts/" not in text  # packaged planner.md not used
    assert "You are **planner**" in text  # packaged common.md still used
    # other agents unaffected
    _, dev_path = launcher.render_agent_files(cfg, tmp_runtime, project, "developer")
    assert "OVERRIDDEN" not in dev_path.read_text()
