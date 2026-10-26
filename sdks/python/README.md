# candybar-sdk (Python)

Write a Candybar cron job as a Python function. The SDK speaks the AD-8
process protocol for you: it reads the invocation context from the
`CANDYBAR_*` env vars, calls your function, and writes the result JSON the
executor reads. It has no dependencies and never opens a NATS connection:
the candybar executor owns execution, metering, accounting and events.

Requires Python >= 3.13.

## Install

```bash
pip install ./sdks/python          # or: uv pip install ./sdks/python
```

The candybar container image ships the SDK in its venv, so jobs under the
jobs mount can `import candybar_sdk` with `#!/usr/bin/env python3`.

Outside the image, the `python3` that a job's shebang finds on the daemon's
`PATH` must have the SDK installed. `uv run candybar daemon` puts the repo
venv (which has it) first; running `.venv/bin/candybar daemon` directly does
not, and the job then fails with `ModuleNotFoundError: candybar_sdk` in its
stderr tail. Alternatively, point the shebang at a specific interpreter.

## Write a candybar

```python
#!/usr/bin/env python3
from candybar_sdk import Context, Result, candybar

@candybar
def main(ctx: Context):
    usage = call_my_model()                       # your work
    ctx.add_tokens(usage.input + usage.output)    # total tokens, int >= 0
    ctx.add_cost(usage.cost_usd)                  # optional, USD >= 0
    if usage.refused:
        return Result.fail("model refused")
    return "summarised 12 threads"                # success + message

if __name__ == "__main__":
    main.run()
```

Make it executable (`chmod +x`); the executor runs the file directly, with no
arguments, from the file's own directory.

| Handler does | Result file | Exit |
| --- | --- | --- |
| returns `None` | `{"status": "success", "metrics": {...}}` | 0 |
| returns `"text"` | success, `message: "text"` | 0 |
| returns `Result.success(msg)` / `Result.fail(msg)` | that status and message | 0 / 1 |
| raises | fail, `message: "<Type>: <text>"`, traceback on stderr | 1 |
| `sys.exit(0)` / `sys.exit(n)` | success / fail `"exit n"` | 0 / 1 |
| returns anything else | fail, names the type | 1 |

`async def` handlers work too; the SDK runs them with `asyncio.run`.

Messages are capped at 500 characters. Token spend and cost are reported
even when the handler fails, because a failed model call still spent tokens.

`ctx` carries `invocation_id`, `candybar_id`, `trigger` (`scheduled`,
`manual` or `misfire`), `tick_id` (`None` for manual runs) and
`result_file`.

Run outside the executor (the env vars are missing), the SDK prints which
vars are missing and exits 2 without calling your function.

## Register it

Registration is the ordinary `candybar.toml` manifest; the SDK registers
nothing itself:

```toml
[candybar]
name = "thread-summariser"
schedule = "0 * * * *"
entrypoint = "summarise.py"
```

```bash
candybar register ./my-job
```

A working example lives in [`examples/hello`](examples/hello).

## Unit-test your handler

Calling the decorated object with a `Context` calls your function directly:

```python
ctx = Context.from_env({"CANDYBAR_INVOCATION_ID": "i", "CANDYBAR_ID": "c",
                        "CANDYBAR_TRIGGER": "manual", "CANDYBAR_RESULT_FILE": "/tmp/r.json"})
assert main(ctx) == "summarised 12 threads"
```

`main.invoke(environ)` runs the full protocol and returns the exit code
without exiting.
