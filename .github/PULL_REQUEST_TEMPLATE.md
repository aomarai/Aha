## Summary
<!-- What changed and why. Link the issue ("Tracks #N"). Avoid closing keywords unless this PR
     really completes the issue. -->

## Test plan
- [ ] `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest` pass locally
- [ ] Tests assert exact behaviour and boundaries (the mutation gate needs it)
- [ ] <!-- what you tested by hand -->

**Not verified:** <!-- anything you could not check (live Discord, the cluster, ...). Say so. -->

## Checklist
- [ ] Docs updated if behaviour, configuration or the command surface changed (README, `.env.example`, AGENTS.md, CHANGELOG)
- [ ] No secrets, tokens or personal data committed; no user content logged
- [ ] New third-party actions are pinned by commit SHA
