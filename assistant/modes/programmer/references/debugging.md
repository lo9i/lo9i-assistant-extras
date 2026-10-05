# Debugging

Adapted from Hermes Agent's systematic-debugging skill (MIT), itself adapted from obra/superpowers.

The rule: no fix without the cause. A change that makes the symptom go away without explaining it usually moves the bug somewhere else.

## 1. Make it fail on demand

Before reading code to build a theory, get one command that shows the user's exact symptom and will pass once it's fixed. Try, in order:

1. A failing test at the point that reaches the bug.
2. A `curl` or script against the running service.
3. The CLI with fixture input, comparing the output with what's expected.
4. A small throwaway script that calls the failing path.
5. `git bisect run <command>` when it worked at a known earlier commit.

Make it fast and deterministic: fix the time, seed randomness, use temporary files. For an intermittent bug, run it in a loop (`for i in $(seq 100); do <cmd> || break; done`) until it fails often enough to study.

If you can't reproduce it, gather more data (logs, versions, configuration) instead of guessing.

## 2. Find where it goes wrong

- Read the whole error and stack trace: file, line, the values involved.
- Check what changed: `git log --oneline -10`, `git diff`, `git log -p --follow <file>`.
- Follow the bad value upstream to where it's first wrong, and fix it there, not where it surfaces.
- Across components (client, API, database), check what goes in and out at each boundary to find the one that breaks.
- Compare with similar code in the same project that works, and list every difference.

## 3. Test one idea at a time

- Write down two to four possible causes, each with a prediction: "if X is the cause, then Y will show Z". Test the likeliest and cheapest first.
- Change one thing at a time. Mark temporary debug lines with a unique tag (`DEBUG-a4f2`) so they're found and removed in one search.
- When an idea is wrong, form a new one. Don't stack another change on top of a failed one.
- When you don't understand something, say so and ask the user.

## 4. Fix it

1. Turn the reproduction into a regression test, and watch it fail.
2. Make one change that addresses the cause.
3. Run that test, then the whole suite.
4. Remove every debug line.

After three fixes that didn't work, stop. Each failure revealing a new problem somewhere else points at the design, not at one more fix. Explain what you found and discuss the design with the user before trying again.
