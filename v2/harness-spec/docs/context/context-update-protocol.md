# Context Update Protocol

After each agent run:

1. Update `/context/latest.md` with the latest run summary.
2. Update `/context/progress.md` with newest entry first.
3. Keep only latest 15 progress entries.
4. Update `/context/project.md` only when appropriate.
5. Create the run-specific report under `/runs/<run-id>/`.
6. Save command logs under `/runs/<run-id>/logs/` when commands are executed.
7. Save codebase map under `/runs/<run-id>/codebase-map.md` when generated.
8. Update `/cache/codebase-map.json` when codebase structure changes.

Do not allow agents to freely rewrite all context files.

In the hook-assisted version, most of this should happen through `after_agent_run` or related shared handlers, not duplicated manually inside every agent implementation.
