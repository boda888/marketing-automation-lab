# Daily run protocol

The scheduled agent follows this file on every run. Humans may edit it; the
agent should only change it when the owner asked for that.

## 0. Setup
1. Clone/pull `main` of `boda888/marketing-automation-lab`. Never force-push. Never rewrite history.
2. Commit as the repo owner so commits count on their GitHub profile (repo-local config,
   before the first commit):
   `git config user.name "boda888" && git config user.email "53265174+boda888@users.noreply.github.com"`.
   Keep the Claude `Co-Authored-By` trailer the harness asks for; never change past commits.
3. `pip install ruff`, then for each project you touch: `pip install -e ".[dev]"`.
4. Run the full baseline: `ruff check . && ruff format --check . && python scripts/check_repo.py`
   and `pytest -q` in every project. If baseline is red, the first job is fixing it.

## 1. Orient — know what already exists (no duplicates)
- Read .agent/FOCUS.md; every commit must serve a project in it and follow its rules.
- Read `ROADMAP.md`, `.agent/state.json` (`current_project`, `next_steps`, `done`, `known_gaps`, the last 5 `run_log` entries) and the README of the current project.
- `git fetch origin main && git log --oneline -20 origin/main`: several runs happen per day,
  so another run may already have done today's step.
- Before starting any step, check it is not already done: grep `src/`, `tests/`, the README
  and `n8n/` for it. If it exists, do not rebuild it — tick it in ROADMAP / `state.json` and
  take the next step. Extending or fixing existing code is fine; a second version of it is not.
- Never add a `done`, `known_gaps`, `next_steps`, ROADMAP or README item that already says
  the same thing: edit the existing one instead. Never re-record a source already in
  `run_log` as new research.
- Check the latest CI run on `main` (`gh run list --limit 3` if `gh` works). If red, fix that first.

## 1b. Commit budget for this run
Runs are scheduled twice a day; how much each one commits varies on purpose. Draw the budget:
`python3 -c "import random; print(random.choices([0, 1, 2, 3, 4], [20, 30, 25, 15, 10])[0])"`
- The budget is the **maximum** number of commits in this run, the state update included.
  It is a cap, not a target: fewer is right when there is less meaningful work.
- `0` → make no commits and do not push; just report "budget 0, skipped".
- `1` → fold the `state.json` / ROADMAP update into the single work commit.
- Otherwise the last commit is the state update (section 4).
Never pad to reach the budget, and never fake or shift commit times.

## 2. Research (before writing code)
- Do 2–5 web searches relevant to today's step: provider rules, API docs/changelogs, n8n release notes, library versions. Prefer primary sources (official docs, RFCs, vendor changelogs).
- If research shows a roadmap assumption is outdated, update the code/README/ROADMAP accordingly — that is a valid commit.
- Record the URLs actually used in the run log (`sources`). Never cite something you did not open.

## 3. Work: up to the budget from 1b (max 4 commits)
Each commit must be **one meaningful, self-contained step** and must:
- add or change working code **with tests**, or fix a real bug/outdated fact, or add docs that describe something that now exists;
- pass `ruff check .`, `ruff format --check .`, `python scripts/check_repo.py` and the project's `pytest -q` **before** committing;
- use a conventional message: `feat(02-deliverability): parse DMARC records`, `fix(01-icp): ...`, `docs(...)`, `test(...)`, `ci: ...`, `chore(agent): ...`.

Zero work commits is the correct outcome when nothing worthwhile can be finished and tested; with a budget of 1 or more, still append an honest run-log entry explaining why (one `chore(agent)` commit).

Forbidden:
- fake or backdated dates (`GIT_AUTHOR_DATE`, `--date`, invented changelog dates);
- filler commits (whitespace, renames for activity, "update README" without substance, splitting one change into many);
- client code, client names, real personal data, API keys or secrets — use fictional companies on `.example` domains;
- claiming something was verified against a live system (n8n, CRM, ESP, LLM API) when it was not;
- disabling, skipping or weakening tests to get green;
- adding dependencies without a reason in the commit body.

Prefer finishing the current project's next step over starting a new project. Move to the next project only when the current one has a usable v0.1 (code + tests + README + n8n workflow if relevant). When a project reaches v0.1, update the root README table and ROADMAP.

## 4. Update state (last commit of the run, or folded into it when the budget is 1)
Update `.agent/state.json`:
- `done` (append), `next_steps` (rewrite as the concrete next 3–6 steps), `known_gaps`, project `status`/`version`, `current_project`;
- `last_run` = today's real date (`date -u +%F`), commit count, notes;
- append to `run_log`: `{date, commits, summary, sources}`. Keep the last 60 entries.
Tick completed boxes in `ROADMAP.md`. Commit as `chore(agent): update state after run`.

## 5. Push and verify
- `git pull --rebase origin main` then `git push origin main`.
- If the pull brought in commits from another run that already did the same step, drop your
  duplicate local commits before pushing (they were never pushed) and report it.
- If push fails, report the exact error; do not retry with different credentials or force.
- If possible, wait for CI (`gh run watch` or poll) and report the result.

## 6. Report
End with a short summary: commits (hash + message), tests run, sources used, CI status, and next step.
