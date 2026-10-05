# What a good PR review looks for

Read the PR title and description first: judge the change against what it says it does.
Read the changed code AND the code around it as it is in the PR (`git show <head_sha>:<path>`,
`git grep <pattern> <head_sha>`) — callers, tests, config. Never read the working tree: it may be
on a different branch.

Look for, in this order:
1. **Correctness** — wrong logic, off-by-one, wrong condition, unhandled None/empty/error paths,
   broken behaviour for existing callers.
2. **Security & data** — secrets in code, injection, missing auth checks, unsafe deletes or
   migrations, personal data in logs.
3. **Does it do what the PR says** — missing pieces, or changes the description doesn't mention.
4. **Tests** — risky new logic with no test; tests that can't fail.
5. **Maintainability** — only when it will genuinely cost the next person time.

Severity:
- **blocker** — wrong or unsafe; must be fixed before merging.
- **should-fix** — a real problem, but not dangerous.
- **nit** — small/style. Keep nits few; skip them entirely on a large PR.

When reviewing a teammate's PR, also decide per note whether it is a **fix** (small, one obvious
change, you'd make it yourself in a minute) or a **leave** (needs the author's judgement). Only PRs by
allow-listed teammates (`fix_allowed_authors` in your config) get fixes. A blocker you leave means
Request changes; anything less still means Approve.

Each note has a short **title** (under 10 words, names the problem) and a body in this layout:

**What's wrong:** the problem, with the file and line (new-file line number from the diff).
**Why it matters:** the real consequence for users, data or the next developer.
**What to do:** a concrete fix.

Plain, direct, friendly — written to the PR author. No praise padding.
In the summary, point to notes by their title or topic, never by number: GitHub doesn't show note numbers.
Never invent a problem to look thorough; zero notes is a valid review.
