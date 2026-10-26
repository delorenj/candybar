"""candybar-sdk — author Candybar cron jobs in Python.

    from candybar_sdk import candybar

    @candybar
    def main(ctx):
        ctx.add_tokens(1234)
        return "processed 12 items"

    if __name__ == "__main__":
        main.run()

Register the file with an ordinary ``candybar.toml``; the executor spawns it,
the SDK speaks the AD-8 process protocol. No bus client, no dependencies.
"""

from ._protocol import (
    ENV_CANDYBAR_ID,
    ENV_INVOCATION_ID,
    ENV_RESULT_FILE,
    ENV_TICK_ID,
    ENV_TRIGGER,
    EXIT_FAIL,
    EXIT_PROTOCOL,
    EXIT_SUCCESS,
    MESSAGE_MAX,
    REQUIRED_ENV,
    Candybar,
    Context,
    ProtocolError,
    Result,
    candybar,
    result_document,
    write_result,
)

__version__ = "0.1.0"

__all__ = [
    "ENV_CANDYBAR_ID",
    "ENV_INVOCATION_ID",
    "ENV_RESULT_FILE",
    "ENV_TICK_ID",
    "ENV_TRIGGER",
    "EXIT_FAIL",
    "EXIT_PROTOCOL",
    "EXIT_SUCCESS",
    "MESSAGE_MAX",
    "REQUIRED_ENV",
    "Candybar",
    "Context",
    "ProtocolError",
    "Result",
    "__version__",
    "candybar",
    "result_document",
    "write_result",
]
