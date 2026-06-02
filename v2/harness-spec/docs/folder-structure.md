# Required Folder Structure

The harness should create or support this structure:

```txt
/context
  project.md
  latest.md
  progress.md

/specs
  prd.md
  tech-design.md
  qa-report.md
  review-report.md
  deploy-report.md

/runs
  /<run-id>
    product-manager-report.md
    architect-report.md
    software-engineer-report.md
    qa-report.md
    recovery-engineer-report.md
    review-report.md
    deploy-report.md
    codebase-map.md
    /logs

/cache
  codebase-map.json

/docs
  ...
```

The exact files may be created only when relevant, but the harness should know and respect this structure.
