# Document operations

Use the main [local operations guide](local-operations.md) for database, backup, recovery, reports,
and startup. For application documents, run:

```bash
cd /Users/ping58972/Documents/job-hunting-machine
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
uv sync --locked
uv run --locked jhm db init
uv run --locked jhm resume check
uv run --locked jhm doctor
```

`jhm resume check` validates both templates and reports the selected local compiler. A missing
compiler is an operator error; install TeX separately and rerun the check. Generated files remain
in ignored application/task/version directories under `resumes/` and `cover-letters/`. Compiler
work remains under `data/latex-build/`.

The resume worker needs a ModelGateway result to choose verified facts. A real model is separately
gated:

```bash
export OPENAI_ALLOW_LIVE=1
export OPENAI_API_KEY='key-from-secure-storage'
uv run --locked jhm resume worker --live-models --once
```

This enables model selection only. It does not enable browser mutation, application submission,
Gmail send, Slack delivery, or any Google document service.
