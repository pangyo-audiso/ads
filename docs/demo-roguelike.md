# Demo project: ASCII roguelike ("Pangyo Dungeon")

A small, turn-based roguelike in Python `curses`. It is a good showcase for ads: big enough to exercise every agent, small enough to finish in a few sessions.

## The game

- **Screen:** `@` player, `g`/`o`/`D` monsters, `#` walls, `.` floors, `!` potions, `$` gold, `>` stairs.
- **Play:** turn-based. Move with arrow or hjkl keys; attack by bumping into a monster. Each floor has a few random rooms; take the stairs to reach the next one.
- **HUD:** HP, depth, gold and a short message log at the bottom.
- **Seeds:** `--seed N` gives the same dungeon every time, which makes automated testing possible.

## Why it shows off ads

| ads feature | How this game exercises it |
|---|---|
| Planner + evaluator review loop | Many easy-to-miss details (map generation, combat formulas, turn order, win/lose conditions, save format) give the evaluator real gaps and ordering problems to catch. |
| Repeated plan revisions | Ask for changes such as "add fog of war" or "make monsters flee at low HP"; the orchestrator passes each one to the planner unchanged. |
| Developer splitting work between two coders | Clean file split. **coder-1:** game logic (`dungeon.py`, `entities.py`, `combat.py`). **coder-2:** interface (`render.py`, `input.py`, `main.py`). Both work in parallel on disjoint files. |
| Setup and config work for coders | `pyproject.toml`, a `roguelike` console command, `config.toml` with monster stats. |
| Tester | Logic is testable without the screen: seeded maps are always connected, combat math, turn order. Scripted key sequences can drive a play-through without a real terminal. |
| One phase per instruction | "Plan it" → "Implement it" → "Test it", each a separate instruction. |
| Cancelling running work | Mid-dev: "cancel that, use a BSP map generator instead". |
| Several projects at once | Run it alongside a second, smaller game (e.g. Tetris) in another cell. |
| Lab Notes | Curses pitfalls (terminal resize, colour pairs, restoring the terminal on a crash) give agents real lessons to record with `ads note`. |

## Demo script

Start the cell from the ads folder:

```bash
.venv/bin/ads pangyo-dungeon      # creates ~/workspace/pangyo-dungeon next to ads/
```

Then type each instruction into pane 3 (bottom-right), waiting for the orchestrator's report before the next one:

1. **Plan**
   ```
   Plan an ASCII roguelike in Python curses: random rooms, @ player, 3 monster types, bump combat, potions, stairs to 5 floors, seeded RNG, HUD, game-over screen. No external deps.
   ```
   Watch: planner drafts, evaluator reviews (window 1), the final plan lands in `projects/pangyo-dungeon/plan/` with `evaluator_status`.
2. **Revise the plan**
   ```
   Revise the plan: add fog of war and a high-score file.
   ```
   Watch: the orchestrator relays the request verbatim; another review round.
3. **Implement**
   ```
   Implement the plan.
   ```
   Watch: the developer splits the work, coder-1 and coder-2 work in parallel, the dev doc lands in `pangyo-dungeon/docs/`.
4. **Test**
   ```
   Test it.
   ```
   Watch: unit tests plus a scripted, seeded play-through; the test doc lands in `pangyo-dungeon/docs/`.
5. **Optional: cancel during step 3**
   ```
   Cancel and switch the map generator to BSP.
   ```
   Watch: the running dev task is superseded; it takes effect when the current agent turn ends.

Useful while it runs: `ads status`, `ads list`, `ads note "…"`, and the supervisor log in window 2.
