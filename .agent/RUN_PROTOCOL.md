# Daily run protocol

The scheduled agent follows this file on every run. Humans may edit it; the
agent should only change it when the owner asked for that.

## 0. Setup
1. Clone/pull `main` of `boda888/marketing-automation-lab`. Never force-push. Never rewrite history.
2. `pip install ruff`, then for each project you touch: `pip install -e ".[dev]"`.
3. Run the full baseline: `ruff check . && ruff format --check . && python scripts/check_repo.py`
   and `pytest -q` in every project. If baseline is red, the first job is fixing it.

## 1. Orient
- Read `ROADMAP.md`, `.agent/state.json` (`current_project`, `next_steps`, `known_gaps`, last `run_log` entries) and the README of the current project.
- Check the latest CI run on `main` (`gh run list --limit 3` if `gh` works). If red, fix that first.

## 2. Research (before writing code)
- Do 2–5 web searches relevant to today's step: provider rules, API docs/changelogs, n8n release notes, library versions. Prefer primary sources (official docs, RFCs, vendor changelogs).
- If research shows a roadmap assumption is outdated, update the code/README/ROADMAP accordingly — that is a valid commit.
- Record the URLs actually used in the run log (`sources`). Never cite something you did not open.

## 3. Work: 0–4 commits
Each commit must be **one meaningful, self-contained step** and must:
- add or change working code **with tests**, or fix a real bug/outdated fact, or add docs that describe something that now exists;
- pass `ruff check .`, `ruff format --check .`, `python scripts/check_repo.py` and the project's `pytest -q` **before** committing;
- use a conventional message: `feat(02-deliverability): parse DMARC records`, `fix(01-icp): ...`, `docs(...)`, `test(...)`, `ci: ...`, `chore(agent): ...`.

Zero commits is the correct outcome when nothing worthwhile can be finished and tested today; in that case still append an honest run-log entry explaining why (one `chore(agent)` commit).

Forbidden:
- fake or backdated dates (`GIT_AUTHOR_DATE`, `--date`, invented changelog dates);
- filler commits (whitespace, renames for activity, "update README" without substance, splitting one change into many);
- client code, client names, real personal data, API keys or secrets — use fictional companies on `.example` domains;
- claiming something was verified against a live system (n8n, CRM, ESP, LLM API) when it was not;
- disabling, skipping or weakening tests to get green;
- adding dependencies without a reason in the commit body.

Prefer finishing the current project's next step over starting a new project. Move to the next project only when the current one has a usable v0.1 (code + tests + README + n8n workflow if relevant). When a project reaches v0.1, update the root README table and ROADMAP.

## 4. Update state (last commit of the run)
Update `.agent/state.json`:
- `done` (append), `next_steps` (rewrite as the concrete next 3–6 steps), `known_gaps`, project `status`/`version`, `current_project`;
- `last_run` = today's real date (`date -u +%F`), commit count, notes;
- append to `run_log`: `{date, commits, summary, sources}`. Keep the last 60 entries.
Tick completed boxes in `ROADMAP.md`. Commit as `chore(agent): update state after run`.

## 5. Push and verify
- `git pull --rebase origin main` then `git push origin main`.
- If push fails, report the exact error; do not retry with different credentials or force.
- If possible, wait for CI (`gh run watch` or poll) and report the result.

## 6. Report
End with a short summary: commits (hash + message), tests run, sources used, CI status, and next step.
