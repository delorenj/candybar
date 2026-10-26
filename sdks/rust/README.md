# candybar-sdk (Rust)

Placeholder — implemented by Story 4.3 (`sdks/rust`, crate `candybar-sdk`, Rust >= 1.99.1).

One process protocol, three languages: invocation context arrives via
`CANDYBAR_*` env vars; the result JSON is written to `CANDYBAR_RESULT_FILE`
(AD-8). SDKs never open a NATS connection.
