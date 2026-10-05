# Tests first

Adapted from Hermes Agent's test-driven-development skill (MIT), itself adapted from obra/superpowers.

A test you never saw fail may not test anything. For new behavior and bug fixes, write the test first. Ask the user before skipping it for a throwaway prototype, generated code or configuration.

## The cycle

1. **Red.** Write one small test for one behavior, named after what it checks (`test_retries_three_times_then_gives_up`). Run it alone and watch it fail for the expected reason: the behavior is missing, not a typo or an import error. A test that passes right away tests something that already works, so change it.
2. **Green.** Write the simplest code that makes it pass, nothing more. Run it, then the whole suite. If it fails, fix the code, not the test.
3. **Clean up.** With everything passing, remove duplication and improve names. Run the suite again.

Repeat for the next behavior.

## Good tests

- Use the project's test framework, folders, fixtures and helpers. Read a few neighbouring tests first and write yours the same way.
- Test real code through its public interface. Mock only what can't run in a test (the network, the clock, paid services), and prefer the fakes the project already has.
- One behavior per test. A name with "and" in it is two tests.
- Check exact results, not just that nothing crashed.
- Tests must be offline, deterministic and fast, and use temporary paths, never the user's real data.

## Running them

- Run a single test while working (`pytest path/test_x.py::test_name -q`, `npx vitest run path -t "name"`), then everything before saying it's done.
- When the suite was already failing before your change, record those failures first (on a clean checkout or a temporary WIP commit) and report only the ones you added.
