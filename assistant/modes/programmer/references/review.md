# Reviewing your own change

Adapted from Hermes Agent's requesting-code-review and simplify-code skills (MIT).

Read your whole diff (`git diff`, plus `git status` for new files) as if someone else wrote it. For a large change, `delegate` a review: give the subagent the repository path, the task, and ask for concrete bugs with file and line, not style opinions.

## Correctness

- Does it do what was asked, in every case the task covers? Check empty input, missing files, errors from outside calls, concurrent use.
- Is every new branch exercised by a test?
- Did a rename or a changed signature miss a caller? Search for the old name.

## Security

Scan the added lines:

```bash
git diff | grep '^+' | grep -inE "(api_key|secret|password|token)\s*[:=]\s*['\"][^'\"]{6,}"
git diff | grep '^+' | grep -nE "shell=True|os\.system\(|\beval\(|\bexec\(|pickle\.loads?\("
```

- User input never reaches a shell, an SQL string or a file path unchecked. Queries are parameterized.
- No secrets in code, tests, logs or the commit.

## Fit and simplicity

- It reads like the code around it: same naming, structure and idioms.
- No leftover debug output, commented-out code, unused imports or TODOs nobody asked for.
- No code that does what an existing helper already does: search for one before keeping new utility code.
- No layers, options or abstractions for needs that don't exist yet.
- Nothing outside the task changed. Undo unrelated edits.

## Last checks

Run the formatter, linter, type checker and the whole test suite the project uses (its `AGENTS.md`, `CONTRIBUTING.md`, CI configuration or `package.json` scripts say which). Every one must pass, or the report says which fails and why.
