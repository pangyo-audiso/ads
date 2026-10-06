# Plan: wordcount (v2)

## Goal and context
The human asked for: "a tiny Python CLI `wordcount.py` that prints line/word/char counts of a file given as argument, with pytest tests. Keep it minimal."

Project directory `/tmp/ads-e2e` is an empty git repository (no commits; only an empty `docs/`). Environment checked on 2026-10-05: `python3` is 3.14.8 and `python3 -m pytest` is pytest 9.1.1. That `python3` is the ads venv, `/home/dev1/workspace/vibe-coding/.venv/bin/python3`; `/usr/bin/python3` has no pytest.

## Scope
- One script, `/tmp/ads-e2e/wordcount.py`, standard library only.
- One test file, `/tmp/ads-e2e/test_wordcount.py`.
- A two-line `/tmp/ads-e2e/.gitignore` (`__pycache__/`, `.pytest_cache/`), because running pytest creates both directories in the project root.

## Non-goals
- No stdin input, no multiple files, no totals line, no flags (`-l`, `-w`, `-c`), no `--help` beyond the usage line.
- No packaging (`pyproject.toml`, console entry point), no README, no CI, no linter config.
- No byte counting and no encodings other than UTF-8.
- Committing is not a step of this plan; whether and when to commit is left to developer and orchestrator.

## Design decisions
1. **Invocation**: `python3 wordcount.py <file>`. Exactly one argument.
2. **Output** (stdout, one line, single spaces, trailing newline): `<lines> <words> <chars> <file>`, where `<file>` is the argument exactly as given. Example: `2 3 16 notes.txt` for a file containing `hello world\nfoo\n`.
3. **Counting rules** (file read as text, `encoding="utf-8"`, `newline=""` so no newline translation happens):
   - lines = number of `"\n"` characters (same as `wc -l`; a final line without a trailing newline is not counted).
   - words = `len(text.split())` (runs of non-whitespace).
   - chars = `len(text)` (Unicode code points, like `wc -m`; `"\r\n"` counts as 2).
4. **Errors**:
   - Wrong number of arguments: print `usage: wordcount.py FILE` to stderr, exit code 2.
   - File cannot be read (`OSError`, e.g. missing file or a directory) or is not valid UTF-8 (`UnicodeDecodeError`): print `wordcount.py: <file>: <reason>` to stderr, exit code 1. Nothing on stdout. Only the prefix `wordcount.py: <file>: ` is specified; the reason text is free.
5. **Structure** (so tests need no subprocess for the logic):
   - `count(text: str) -> tuple[int, int, int]` returns `(lines, words, chars)`.
   - `main(argv: list[str] | None = None) -> int` uses `sys.argv[1:]` when `argv` is `None`, prints, and returns the exit code.
   - `if __name__ == "__main__": sys.exit(main())`.
   - `sys.argv` handled by hand; no `argparse` (keeps the usage/exit-code behaviour explicit and the file tiny).
6. **Test location**: `test_wordcount.py` sits next to `wordcount.py` in the project root, not in `tests/`. With pytest's default import mode the test file's own directory is put on `sys.path`, so `import wordcount` works with plain `pytest` and no `conftest.py`, `pytest.ini` or package install.

## Steps
Both steps are small; one coder can do both in order. Step 2 depends on step 1.

### Step 1: implement `wordcount.py`
- Files: `/tmp/ads-e2e/wordcount.py` (new), `/tmp/ads-e2e/.gitignore` (new).
- Implement decisions 1-5. Target size: about 30 lines.
- `.gitignore` contains exactly the two lines `__pycache__/` and `.pytest_cache/`.
- Acceptance (run in `/tmp/ads-e2e`):
  - `f=$(mktemp) && printf 'hello world\nfoo\n' > "$f" && python3 wordcount.py "$f"; rm "$f"` prints exactly `2 3 16 <that temp path>` and `wordcount.py` exits 0.
  - `python3 wordcount.py` prints the usage line to stderr, nothing to stdout, exits 2.
  - `python3 wordcount.py /no/such/file` prints a line starting with `wordcount.py: /no/such/file: ` to stderr, nothing to stdout, exits 1.

### Step 2: write `test_wordcount.py`
- Files: `/tmp/ads-e2e/test_wordcount.py` (new).
- Tests use only pytest built-ins (`tmp_path`, `capsys`, `pytest.mark.parametrize`); files are written with `Path.write_bytes` so line endings are exact. Wherever a case below says `path`, the test passes `str(path)` to `main` and to `subprocess.run`, matching the `list[str]` signature.
- Required cases:
  1. `count` parametrized over:
     - `""` -> `(0, 0, 0)`
     - `"hello world\nfoo\n"` -> `(2, 3, 16)`
     - `"no newline"` -> `(0, 2, 10)`
     - `"  a \t b\n\n"` -> `(2, 2, 9)`
     - `"h\u00e9llo\n"` -> `(1, 1, 6)` (code points, not bytes; written with the escape so the source file cannot hold a decomposed `é`)
     - `"a\r\nb\r\n"` -> `(2, 2, 6)`
  2. `main([path])` on a `tmp_path` file containing `b"hello world\nfoo\n"`: returns 0, stdout is exactly `f"2 3 16 {path}\n"`, stderr is empty.
  3. `main([path])` on a file containing `b"a\r\nb\r\n"`: stdout starts with `2 2 6 ` (proves no newline translation on read).
  4. `main([])` and `main(["a", "b"])`: return 2, stdout empty, stderr contains `usage:`.
  5. `main([missing_path])`: returns 1, stdout empty, stderr starts with `f"wordcount.py: {missing_path}: "`.
  6. `main([path])` on a file containing `b"\xff\xfe"`: returns 1, stdout empty, stderr starts with `f"wordcount.py: {path}: "`.
  7. `main([str(tmp_path)])` (a directory): returns 1, stdout empty, stderr starts with `f"wordcount.py: {tmp_path}: "`.
  8. One end-to-end run with `subprocess.run([sys.executable, "wordcount.py", path], cwd=<project root from Path(__file__).parent>, capture_output=True, text=True)`: return code 0 and stdout `f"2 3 16 {path}\n"` (covers the `__main__` block and `sys.argv` default).
- Acceptance: `python3 -m pytest -q` in `/tmp/ads-e2e` exits 0 with all cases above present and passing. If that `python3` has no pytest, use `/home/dev1/workspace/vibe-coding/.venv/bin/python3 -m pytest -q`. Afterwards `git status --short` lists no `__pycache__/` or `.pytest_cache/` entry.

## Test strategy
- Unit tests on `count` fix the counting rules; in-process `main` tests fix output format, exit codes and stream usage; one subprocess test fixes the real CLI entry.
- Final check by developer/tester: `cd /tmp/ads-e2e && python3 -m pytest -q` passes (or `/home/dev1/workspace/vibe-coding/.venv/bin/python3 -m pytest -q` when `python3` has no pytest), plus the three manual commands in step 1.

## Risks and open questions
- **"char" means characters, not bytes** (decision 3). This differs from `wc -c` for non-ASCII files. Chosen because the request says "char"; switching to bytes would be a one-line change plus test updates.
- **Line count follows `wc -l`**: a file without a trailing newline reports one line fewer than an editor shows. Chosen for predictability and parity with `wc`.
- **pytest availability**: only the ads venv has pytest (9.1.1); `/usr/bin/python3` does not. The acceptance commands name the venv fallback `/home/dev1/workspace/vibe-coding/.venv/bin/python3 -m pytest -q`.
- No open questions block implementation.

## Changes since v1
Applied all seven minor findings of `/home/dev1/workspace/vibe-coding/work/reviews/wordcount.v1.md` (m1-m7), using the fixes the evaluator proposed. None was rejected.
