# /pr-review

A Claude Code skill that reviews GitHub pull requests with two models checking each other, and
handles the whole review loop on GitHub: notes, fixes, verdict, threads, merge. Nothing is posted,
pushed or merged without the owner typing "yes", and every publish shows a dry run first.

## What it does

| Command | On someone else's PR | On your own PR |
|---|---|---|
| `/pr-review <PR link>` | Claude reviews, Codex checks the review (up to 3 rounds). You approve one plan, then it submits a real verdict: **Approve** or **Request changes**, with inline notes. Non-blocking leftovers become linked GitHub issues. On allow-listed teammates' PRs it also fixes small, clear notes on their branch first. | The review stays private. It fixes what it found in a separate copy, Codex checks the fixes, and on "yes" it pushes and asks your reviewer to look. |
| `/pr-review fix <PR link>` | Not allowed. | Fixes every open reviewer comment, replies on each thread, re-requests review. |
| `/pr-review merge <PR link>` | Not allowed: the author merges. | Merges once a reviewer approved the latest commit, every thread is resolved and checks are green, then deletes the branch (never `main`, `master`, `develop`, the default branch, or a branch another open PR is based on). |

Run it from a Claude Code session **inside the PR's project folder** (it refuses if the folder is a
different repo). The PR's code is read at the PR's head commit, so your own working folder and
branch are never touched; fixes happen in a separate git worktree under `~/pr-reviews/.fix/`.

## How a review goes

1. **Review rounds.** Claude reviews (Opus, high effort) following `recipe.md`. Codex checks every
   note (`agree / disagree / unsure`) and lists what Claude missed. They go back and forth, 3 rounds
   at most; anything still disputed becomes a "you decide" item, left out unless you keep it.
2. **Wording pass.** Codex polishes everything that will be posted, without changing the meaning;
   if a polish drops a commit sha or test name, the original wording is kept.
3. **Plan.** You see a scorecard and one plan line, e.g.
   `Approve · fix 2 · 1 new issue · 1 optional · 0 left open`, and reply
   `yes` · `fix 4` / `leave 4` · `keep 4` · `drop 3, 5` · `edit` · `cancel`.
4. **Publish.** Dry run first, then the real run. Every GitHub write is recorded as it happens, so a
   run that stops half-way can be re-run without posting anything twice.

Verdict rules: a blocker that is left → Request changes; otherwise Approve. On Approve, should-fix
leftovers become GitHub issues and every thread the review opened ends resolved (repos that require
resolved threads can merge straight away).

## Requirements

- Claude Code, with this folder at `~/.claude/skills/pr-review/`.
- `gh` (GitHub CLI) logged in with every account listed in the configuration below
  (`gh auth status`). Calls use `gh auth token -u <account>` per request; the active `gh` account is
  never switched.
- `git` able to push over HTTPS as those accounts for the repos you'll fix (e.g. a per-owner
  credential helper in `~/.gitconfig`).
- `codex` CLI logged in, with access to the model in `scripts/codex_check.py`
  (`DEFAULT_MODEL = "gpt-6-sol"`, effort `xhigh`). ChatGPT-plan logins can't use `gpt-6.1-sol`.
- Python 3 (standard library only). pytest only for the tests.

## Configuration

Who-is-who lives in a JSON file outside the skill folder, so nothing personal is in the repo:

```bash
mkdir -p ~/.config/pr-review
cp ~/.claude/skills/pr-review/config.example.json ~/.config/pr-review/config.json
```

then fill in your own accounts. Set the environment variable `PR_REVIEW_CONFIG` to use a different
path. Without the file every script stops with "No pr-review config at <path>"; a file that is not
valid JSON, or is missing a key, stops with a message naming the file.

| Key | What it means |
|---|---|
| `owner_accounts` | Repo owner (any case) → the GitHub login that reviews and posts there. Repos owned by anyone not listed are refused. |
| `own_accounts` | Your own logins. PRs opened by these get the private self-check and fix flow. |
| `commit_identities` | `[name, email]` for fix commits, per posting account (use an email verified on that account). |
| `fix_allowed_authors` | Teammates who agreed that your reviews may push fixes to their PR branches. Everyone else: review only. Optional; empty means nobody. |

Example:

```json
{
  "owner_accounts": {"example-org": "alice-dev", "acme-corp": "alice-dev", "alice-work": "alice-work"},
  "own_accounts": ["alice-dev", "alice-work"],
  "commit_identities": {"alice-dev": ["Alice Example", "alice@example.com"],
                        "alice-work": ["Alice Example", "alice@work.example.com"]},
  "fix_allowed_authors": ["bob-dev"]
}
```

Other knobs: `PR_REVIEWS_DIR` (environment; default `~/pr-reviews`), the Claude model/effort in
`SKILL.md`'s frontmatter, the Codex model in `scripts/codex_check.py`.

## Where things live

- `SKILL.md` — the instructions Claude follows, step by step.
- `recipe.md`, `fix-recipe.md` — how to review, how to fix.
- `codex-check.md`, `fix-check.md`, `wording-pass.md` + `*.schema.json` — Codex prompts and the
  exact JSON shapes it must answer in. PR text and reviewer comments are fenced as untrusted data.
- `scripts/` — the exact parts: reading the PR (`pr_context.py`), open threads (`threads.py`), the
  fix copy (`fixcopy.py`), Codex runs (`codex_check.py`, `fix_check.py`, `wording.py`), drafts
  (`draft.py`, `fixdraft.py`), publishing (`review_publish.py`, `publish.py`, shared `pushlib.py`),
  merging (`merge.py`). Every script prints JSON; problems print `{"error": "..."}` and exit 2.
- `~/pr-reviews/` (outside the repo) — saved drafts and work files, one set per PR.

## Safety

- Never force-pushes. Never pushes to a PR from a fork. Fixes only allow-listed teammates' PRs.
- Never posts a review on your own PR, never approves it, never merges someone else's.
- Resolves only threads its own accounts opened; reviewers resolve theirs.
- Publishing refuses on: new commits on the PR, uncommitted changes in the fix copy, a commit no
  note accounts for, issues turned off when issues are needed, or any change since the dry run.

## Tests

From the skill folder:

```bash
python3 -m pytest tests -q
```
All GitHub, git and Codex calls are faked; the tests never touch the network. They use a made-up
config from `tests/conftest.py`, never your own.
