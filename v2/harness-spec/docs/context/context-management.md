# Context Management

The harness uses markdown files as durable project memory.

## `/context/project.md`

Purpose: long-term project overview and stable project memory.

Should include:

- Project summary
- Target users
- Product purpose
- Core features
- Current tech stack summary
- Current architecture summary
- Important project-level principles
- Major decisions

This file should not become a detailed task log.

Allowed to update:

- `product_manager`
- `system_architect`
- `software_engineer` only if project-level implementation changed
- `devops_engineer` only for infra/deployment summary

`qa_engineer`, `recovery_engineer`, and `code_reviewer` should avoid updating this file unless there is a major project-level issue.

## `/context/latest.md`

Purpose: latest task/prompt/change only.

This file should be overwritten or refreshed by each agent run.

Should include:

- Timestamp
- run_id
- task_id
- agent
- summary
- files changed
- result status
- next recommended action

## `/context/progress.md`

Purpose: recent rolling execution history.

Rules:

- Keep latest 15 entries only.
- Newest entry first.
- When more than 15 entries exist, delete the oldest entries.

Each entry should include:

- Timestamp
- run_id
- task_id
- agent
- summary
- status
- next action

## `/specs/prd.md`

Purpose: source of truth for product requirements.

Rules:

- No agent may create, modify, overwrite, append, or delete this file.
- Only the human user may manually edit `/specs/prd.md`.
- Agents may read it.
- If an agent believes the PRD should change, it must propose the change in terminal output or a report, but must not modify `/specs/prd.md`.

## `/specs/tech-design.md`

Purpose: source of truth for technical design.

Created/updated by:

- `system_architect`

Other agents may read it.

If a technical design change is needed during development, create a `Tech Design Change Proposal` in the relevant report instead of directly changing the file.
