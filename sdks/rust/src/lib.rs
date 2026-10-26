//! # candybar-sdk
//!
//! Author [candybar](https://github.com/delorenj/candybar) cron jobs in Rust.
//!
//! A candybar is a scheduled job. The candybar executor spawns it as a
//! subprocess, and this crate implements the executor's process protocol
//! (AD-8) for you:
//!
//! - **In:** five env vars, parsed into a [`Context`]:
//!   `CANDYBAR_INVOCATION_ID`, `CANDYBAR_ID`, `CANDYBAR_TRIGGER`
//!   (`scheduled` | `manual` | `misfire`), `CANDYBAR_TICK_ID` (empty for manual
//!   runs) and `CANDYBAR_RESULT_FILE`.
//! - **Out:** UTF-8 JSON written atomically to `CANDYBAR_RESULT_FILE`:
//!
//!   ```json
//!   {"status":"success","message":"optional","metrics":{"token_spend":1545,"cost_usd":0.0042}}
//!   ```
//!
//!   `message` and `cost_usd` are omitted when unset, and `metrics` never
//!   carries another key. The exit code agrees with the status: [`EXIT_SUCCESS`],
//!   [`EXIT_FAIL`], or [`EXIT_PROTOCOL`] when the process was not started by
//!   the executor.
//!
//! The executor measures runtime, memory and CPU itself. The handler only
//! self-reports the LLM tokens it consumed (and, optionally, their cost) via
//! [`Context::add_tokens`] and [`Context::add_cost`]. Spend is reported even
//! when the handler fails or panics, because a failed LLM call still burned
//! tokens.
//!
//! This crate has **no dependencies** and never opens a network or NATS
//! connection. The executor owns execution, metering and events (AD-7).
//!
//! ## Example
//!
//! ```no_run
//! use std::error::Error;
//! use std::process::ExitCode;
//!
//! use candybar_sdk::{Context, Outcome};
//!
//! fn main() -> ExitCode {
//!     candybar_sdk::run(job)
//! }
//!
//! fn job(ctx: &Context) -> Result<Outcome, Box<dyn Error>> {
//!     // ... call your model here, then report what it consumed:
//!     ctx.add_tokens(1200 + 345);
//!     ctx.add_cost(0.0042)?;
//!     Ok(Outcome::success_with(format!("ran on a {} trigger", ctx.trigger())))
//! }
//! ```
//!
//! A handler can return `()`, a `String`/`&str` (success with that message),
//! an [`Outcome`], or a `Result` of any of those with an error that implements
//! [`Display`](fmt::Display). An `Err` or a panic becomes `{"status":"fail"}`
//! with the error text (or `panic: <payload>`) as the message.
//!
//! ## Async handlers
//!
//! The SDK is runtime-agnostic. Block on your runtime inside the handler, for
//! example `tokio::runtime::Runtime::new()?.block_on(async { ... })`.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use std::any::Any;
use std::error::Error;
use std::ffi::OsString;
use std::fmt;
use std::fs;
use std::io::{self, Write};
use std::panic::{self, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};

/// Env var carrying this invocation's id (a UUID).
pub const ENV_INVOCATION_ID: &str = "CANDYBAR_INVOCATION_ID";
/// Env var carrying the candybar's registry id (a UUID).
pub const ENV_CANDYBAR_ID: &str = "CANDYBAR_ID";
/// Env var carrying the trigger: `scheduled`, `manual` or `misfire`.
pub const ENV_TRIGGER: &str = "CANDYBAR_TRIGGER";
/// Env var carrying the tick id (`tick:<unix_epoch_minute>`); empty for manual runs.
pub const ENV_TICK_ID: &str = "CANDYBAR_TICK_ID";
/// Env var carrying the path the result JSON must be written to.
pub const ENV_RESULT_FILE: &str = "CANDYBAR_RESULT_FILE";

/// The vars that must be present and non-empty. `CANDYBAR_TICK_ID` is
/// legitimately empty for manual triggers, so it is optional.
pub const REQUIRED_ENV: [&str; 4] = [ENV_INVOCATION_ID, ENV_CANDYBAR_ID, ENV_TRIGGER, ENV_RESULT_FILE];

/// Result messages are capped at this many characters, the same cap the
/// executor applies to failure messages in event data.
pub const MESSAGE_MAX_CHARS: usize = 500;

/// The largest reportable token spend. The registry stores `token_spend` as a
/// signed 64-bit integer, so totals saturate here instead of overflowing it.
pub const TOKEN_SPEND_MAX: u64 = i64::MAX as u64;

/// Exit code for a successful invocation.
pub const EXIT_SUCCESS: u8 = 0;
/// Exit code for a failed invocation: `Err`, panic, [`Outcome::fail`], or an
/// unwritable result file.
pub const EXIT_FAIL: u8 = 1;
/// Exit code when the process was not started under the executor's contract
/// (a required env var is missing or empty). The handler does not run.
pub const EXIT_PROTOCOL: u8 = 2;

/// What caused this invocation.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Trigger {
    /// The candybar's cron schedule matched a heartbeat tick.
    Scheduled,
    /// Someone ran `candybar run <id>`.
    Manual,
    /// The latest fire missed while the daemon was down, run on catch-up.
    Misfire,
    /// A trigger this SDK version does not know. It is passed through rather
    /// than rejected, so a newer executor never breaks an older candybar.
    Other(String),
}

impl Trigger {
    /// Parse the `CANDYBAR_TRIGGER` value. Never fails: unknown values become
    /// [`Trigger::Other`].
    pub fn parse(raw: &str) -> Trigger {
        match raw {
            "scheduled" => Trigger::Scheduled,
            "manual" => Trigger::Manual,
            "misfire" => Trigger::Misfire,
            other => Trigger::Other(other.to_owned()),
        }
    }

    /// The wire value, e.g. `"scheduled"`.
    pub fn as_str(&self) -> &str {
        match self {
            Trigger::Scheduled => "scheduled",
            Trigger::Manual => "manual",
            Trigger::Misfire => "misfire",
            Trigger::Other(other) => other,
        }
    }
}

impl fmt::Display for Trigger {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The process was not started under the executor's contract.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtocolError {
    missing: Vec<&'static str>,
    not_unicode: Vec<&'static str>,
}

impl ProtocolError {
    /// Required vars that were unset or empty.
    pub fn missing(&self) -> &[&'static str] {
        &self.missing
    }

    /// Vars that were set but are not valid UTF-8.
    pub fn not_unicode(&self) -> &[&'static str] {
        &self.not_unicode
    }
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("not running under the candybar executor:")?;
        if !self.missing.is_empty() {
            write!(f, " missing {}", self.missing.join(", "))?;
        }
        if !self.not_unicode.is_empty() {
            if !self.missing.is_empty() {
                f.write_str(";")?;
            }
            write!(f, " not valid UTF-8: {}", self.not_unicode.join(", "))?;
        }
        Ok(())
    }
}

impl Error for ProtocolError {}

/// A rejected [`Context::add_cost`] value. The accumulated cost is unchanged.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MetricError {
    value: f64,
    overflow: bool,
}

impl MetricError {
    /// The value that was rejected.
    pub fn value(&self) -> f64 {
        self.value
    }
}

impl fmt::Display for MetricError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.overflow {
            write!(f, "cost_usd total overflows after adding {}", self.value)
        } else {
            write!(f, "cost_usd must be a finite number >= 0, got {}", self.value)
        }
    }
}

impl Error for MetricError {}

/// The invocation context, parsed from the five `CANDYBAR_*` env vars.
///
/// Identity fields are read-only. Token spend and cost accumulate through
/// [`add_tokens`](Context::add_tokens) and [`add_cost`](Context::add_cost).
/// `Context` is `Sync`, so threads spawned by the handler can report spend
/// through a shared `&Context`.
#[derive(Debug)]
pub struct Context {
    invocation_id: String,
    candybar_id: String,
    trigger: Trigger,
    tick_id: Option<String>,
    result_file: PathBuf,
    token_spend: AtomicU64,
    cost_usd: Mutex<Option<f64>>,
}

impl Context {
    /// Parse the context from this process's environment.
    pub fn from_env() -> Result<Context, ProtocolError> {
        Context::from_lookup(|name| std::env::var_os(name))
    }

    /// Parse the context from any env lookup. Use this to unit-test your
    /// handler without touching the process environment:
    ///
    /// ```
    /// use std::collections::HashMap;
    /// use std::ffi::OsString;
    ///
    /// let env = HashMap::from([
    ///     ("CANDYBAR_INVOCATION_ID", "0199f1c2-0000-7000-8000-000000000001"),
    ///     ("CANDYBAR_ID", "0199f1c2-0000-7000-8000-000000000002"),
    ///     ("CANDYBAR_TRIGGER", "manual"),
    ///     ("CANDYBAR_TICK_ID", ""),
    ///     ("CANDYBAR_RESULT_FILE", "/tmp/result.json"),
    /// ]);
    /// let ctx = candybar_sdk::Context::from_lookup(|k| env.get(k).map(OsString::from)).unwrap();
    /// assert_eq!(ctx.trigger(), &candybar_sdk::Trigger::Manual);
    /// assert_eq!(ctx.tick_id(), None);
    /// ```
    pub fn from_lookup<L>(lookup: L) -> Result<Context, ProtocolError>
    where
        L: Fn(&str) -> Option<OsString>,
    {
        let mut missing = Vec::new();
        let mut not_unicode = Vec::new();
        let mut text = |name: &'static str, required: bool| -> Option<String> {
            match lookup(name) {
                None => {
                    if required {
                        missing.push(name);
                    }
                    None
                }
                Some(raw) if raw.is_empty() => {
                    if required {
                        missing.push(name);
                    }
                    None
                }
                Some(raw) => match raw.into_string() {
                    Ok(value) => Some(value),
                    Err(_) => {
                        not_unicode.push(name);
                        None
                    }
                },
            }
        };
        let invocation_id = text(ENV_INVOCATION_ID, true);
        let candybar_id = text(ENV_CANDYBAR_ID, true);
        let trigger = text(ENV_TRIGGER, true);
        let tick_id = text(ENV_TICK_ID, false);
        // The result path is a filesystem path, not text: no UTF-8 requirement.
        let result_file = match lookup(ENV_RESULT_FILE) {
            Some(raw) if !raw.is_empty() => Some(PathBuf::from(raw)),
            _ => {
                missing.push(ENV_RESULT_FILE);
                None
            }
        };
        match (invocation_id, candybar_id, trigger, result_file) {
            (Some(invocation_id), Some(candybar_id), Some(trigger), Some(result_file))
                if missing.is_empty() && not_unicode.is_empty() =>
            {
                Ok(Context {
                    invocation_id,
                    candybar_id,
                    trigger: Trigger::parse(&trigger),
                    tick_id,
                    result_file,
                    token_spend: AtomicU64::new(0),
                    cost_usd: Mutex::new(None),
                })
            }
            _ => Err(ProtocolError { missing, not_unicode }),
        }
    }

    /// This invocation's id (`CANDYBAR_INVOCATION_ID`).
    pub fn invocation_id(&self) -> &str {
        &self.invocation_id
    }

    /// The candybar's registry id (`CANDYBAR_ID`).
    pub fn candybar_id(&self) -> &str {
        &self.candybar_id
    }

    /// What caused this invocation (`CANDYBAR_TRIGGER`).
    pub fn trigger(&self) -> &Trigger {
        &self.trigger
    }

    /// The heartbeat tick id, or `None` for manual runs (`CANDYBAR_TICK_ID`).
    pub fn tick_id(&self) -> Option<&str> {
        self.tick_id.as_deref()
    }

    /// Where the result JSON is written (`CANDYBAR_RESULT_FILE`).
    pub fn result_file(&self) -> &Path {
        &self.result_file
    }

    /// Add to the total tokens consumed (input + output) by this invocation.
    /// The total saturates at [`TOKEN_SPEND_MAX`].
    pub fn add_tokens(&self, tokens: u64) {
        self.token_spend
            .update(Ordering::Relaxed, Ordering::Relaxed, |current| {
                current.saturating_add(tokens).min(TOKEN_SPEND_MAX)
            });
    }

    /// Add to this invocation's cost in US dollars. A negative, NaN or
    /// infinite value (or a total that would overflow) is rejected with a
    /// [`MetricError`] and leaves the accumulated cost unchanged, so the
    /// result file always stays valid. Propagate it with `?` to fail the
    /// invocation.
    pub fn add_cost(&self, usd: f64) -> Result<(), MetricError> {
        if !usd.is_finite() || usd < 0.0 {
            return Err(MetricError {
                value: usd,
                overflow: false,
            });
        }
        let mut cost = lock(&self.cost_usd);
        let total = cost.unwrap_or(0.0) + usd;
        if !total.is_finite() {
            return Err(MetricError {
                value: usd,
                overflow: true,
            });
        }
        // `-0.0 + 0.0` is `0.0`: never report a negative zero.
        *cost = Some(total + 0.0);
        Ok(())
    }

    /// The token spend accumulated so far.
    pub fn token_spend(&self) -> u64 {
        self.token_spend.load(Ordering::Relaxed)
    }

    /// The cost accumulated so far, or `None` if no cost was reported.
    pub fn cost_usd(&self) -> Option<f64> {
        *lock(&self.cost_usd)
    }
}

/// Lock that survives poisoning: a panic elsewhere must not lose the spend.
fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// The status written to the result file.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Status {
    /// `"success"`
    Success,
    /// `"fail"`
    Fail,
}

impl Status {
    /// The wire value.
    pub fn as_str(self) -> &'static str {
        match self {
            Status::Success => "success",
            Status::Fail => "fail",
        }
    }
}

/// A handler's explicit verdict. Metrics are never part of an outcome: they
/// accumulate on the [`Context`], so they cannot be counted twice and are
/// still reported when the handler fails.
#[must_use = "an Outcome does nothing unless the handler returns it"]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outcome {
    status: Status,
    message: Option<String>,
}

impl Outcome {
    /// Success with no message.
    pub fn success() -> Outcome {
        Outcome {
            status: Status::Success,
            message: None,
        }
    }

    /// Success with a message.
    pub fn success_with(message: impl Into<String>) -> Outcome {
        Outcome {
            status: Status::Success,
            message: Some(message.into()),
        }
    }

    /// Failure with a message. The process exits with [`EXIT_FAIL`].
    pub fn fail(message: impl Into<String>) -> Outcome {
        Outcome {
            status: Status::Fail,
            message: Some(message.into()),
        }
    }

    /// The verdict.
    pub fn status(&self) -> Status {
        self.status
    }

    /// Whether the verdict is success.
    pub fn is_success(&self) -> bool {
        self.status == Status::Success
    }

    /// The message, if any.
    pub fn message(&self) -> Option<&str> {
        self.message.as_deref()
    }
}

impl From<()> for Outcome {
    fn from((): ()) -> Outcome {
        Outcome::success()
    }
}

impl From<&str> for Outcome {
    fn from(message: &str) -> Outcome {
        Outcome::success_with(message)
    }
}

impl From<String> for Outcome {
    fn from(message: String) -> Outcome {
        Outcome::success_with(message)
    }
}

/// Anything a handler may return. Implemented for `()`, `&str`, `String`,
/// [`Outcome`], and `Result<T, E>` where `T: Into<Outcome>` and
/// `E: Display`. An `Err` becomes a fail outcome carrying the error's
/// `Display` text.
pub trait IntoOutcome {
    /// Convert into the verdict written to the result file.
    fn into_outcome(self) -> Outcome;
}

impl IntoOutcome for Outcome {
    fn into_outcome(self) -> Outcome {
        self
    }
}

impl IntoOutcome for () {
    fn into_outcome(self) -> Outcome {
        Outcome::success()
    }
}

impl IntoOutcome for &str {
    fn into_outcome(self) -> Outcome {
        Outcome::success_with(self)
    }
}

impl IntoOutcome for String {
    fn into_outcome(self) -> Outcome {
        Outcome::success_with(self)
    }
}

impl<T, E> IntoOutcome for Result<T, E>
where
    T: Into<Outcome>,
    E: fmt::Display,
{
    fn into_outcome(self) -> Outcome {
        match self {
            Ok(value) => value.into(),
            Err(err) => {
                let text = err.to_string();
                if text.is_empty() {
                    Outcome::fail("handler returned an error with an empty message")
                } else {
                    Outcome::fail(text)
                }
            }
        }
    }
}

/// Run `handler` under the protocol and return the process exit code. This is
/// the whole SDK in one call:
///
/// ```no_run
/// fn main() -> std::process::ExitCode {
///     candybar_sdk::run(|ctx| {
///         ctx.add_tokens(42);
///         "done"
///     })
/// }
/// ```
pub fn run<F, R>(handler: F) -> ExitCode
where
    F: FnOnce(&Context) -> R,
    R: IntoOutcome,
{
    ExitCode::from(invoke(|name| std::env::var_os(name), handler))
}

/// The testable core of [`run`]: parse the context through `lookup`, run the
/// handler (catching panics), write the result file, and return the exit code.
/// Diagnostics go to stderr, which the executor keeps as the failure's stderr
/// tail.
pub fn invoke<L, F, R>(lookup: L, handler: F) -> u8
where
    L: Fn(&str) -> Option<OsString>,
    F: FnOnce(&Context) -> R,
    R: IntoOutcome,
{
    invoke_to(lookup, handler, &mut io::stderr())
}

fn invoke_to<L, F, R>(lookup: L, handler: F, err: &mut dyn Write) -> u8
where
    L: Fn(&str) -> Option<OsString>,
    F: FnOnce(&Context) -> R,
    R: IntoOutcome,
{
    let ctx = match Context::from_lookup(&lookup) {
        Ok(ctx) => ctx,
        Err(protocol) => {
            let _ = writeln!(err, "candybar-sdk: {protocol}");
            // Leave a fail result naming the problem when we know where to write it.
            if let Some(path) = lookup(ENV_RESULT_FILE).filter(|raw| !raw.is_empty()) {
                let json = encode_result(Status::Fail, Some(&protocol.to_string()), 0, None);
                if let Err(write_err) = write_result_file(Path::new(&path), &json) {
                    let _ = writeln!(err, "candybar-sdk: cannot write result file: {write_err}");
                }
            }
            return EXIT_PROTOCOL;
        }
    };

    let outcome = match panic::catch_unwind(AssertUnwindSafe(|| handler(&ctx).into_outcome())) {
        Ok(outcome) => outcome,
        // The default panic hook has already printed the location to stderr.
        Err(payload) => Outcome::fail(format!("panic: {}", panic_text(payload.as_ref()))),
    };
    if !outcome.is_success() {
        let _ = writeln!(
            err,
            "candybar-sdk: invocation failed: {}",
            clean_message(outcome.message().unwrap_or(""))
        );
    }

    let json = encode_result(
        outcome.status(),
        outcome.message(),
        ctx.token_spend(),
        ctx.cost_usd(),
    );
    if let Err(write_err) = write_result_file(ctx.result_file(), &json) {
        let _ = writeln!(
            err,
            "candybar-sdk: cannot write result file {}: {write_err}",
            ctx.result_file().display()
        );
        return EXIT_FAIL;
    }
    if outcome.is_success() {
        EXIT_SUCCESS
    } else {
        EXIT_FAIL
    }
}

fn panic_text(payload: &(dyn Any + Send)) -> &str {
    if let Some(text) = payload.downcast_ref::<&'static str>() {
        text
    } else if let Some(text) = payload.downcast_ref::<String>() {
        text
    } else {
        "<non-string panic payload>"
    }
}

/// Encode the AD-8 result JSON in canonical key order: `status`, `message`
/// (omitted when `None`; capped at [`MESSAGE_MAX_CHARS`], NUL replaced by
/// U+FFFD), `metrics` (`token_spend` always, capped at [`TOKEN_SPEND_MAX`];
/// `cost_usd` only when `Some` and valid).
pub fn encode_result(
    status: Status,
    message: Option<&str>,
    token_spend: u64,
    cost_usd: Option<f64>,
) -> String {
    let mut out = String::with_capacity(96);
    out.push_str("{\"status\":\"");
    out.push_str(status.as_str());
    out.push('"');
    if let Some(message) = message {
        out.push_str(",\"message\":");
        push_json_string(&mut out, &clean_message(message));
    }
    out.push_str(",\"metrics\":{\"token_spend\":");
    out.push_str(&token_spend.min(TOKEN_SPEND_MAX).to_string());
    if let Some(cost) = cost_usd.filter(|cost| cost.is_finite() && *cost >= 0.0) {
        out.push_str(",\"cost_usd\":");
        // Debug is the shortest round-trip form, always a valid JSON number
        // for finite values (e.g. `0.0042`, `2.0`, `1e-7`, `1e21`).
        out.push_str(&format!("{:?}", cost + 0.0));
    }
    out.push_str("}}");
    out
}

fn truncate_chars(text: &str, max: usize) -> &str {
    match text.char_indices().nth(max) {
        Some((byte_index, _)) => &text[..byte_index],
        None => text,
    }
}

/// Cap a message and replace NUL, which the registry's Postgres text columns
/// reject: a NUL reaching `error_message` or `stderr_tail` would fail the
/// executor's terminal write and re-run the job on every redelivery.
fn clean_message(text: &str) -> String {
    truncate_chars(text, MESSAGE_MAX_CHARS).replace('\0', "\u{FFFD}")
}

fn push_json_string(out: &mut String, text: &str) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0C}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Write `json` atomically: a temp file in the same directory, then rename,
/// so the executor never reads a partial result.
fn write_result_file(path: &Path, json: &str) -> io::Result<()> {
    let file_name = path
        .file_name()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "result file path has no file name"))?;
    let dir = match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent,
        _ => Path::new("."),
    };
    let mut tmp_name = OsString::from(".");
    tmp_name.push(file_name);
    tmp_name.push(format!(".{}.tmp", std::process::id()));
    let tmp = dir.join(tmp_name);
    let written = fs::write(&tmp, json.as_bytes()).and_then(|()| fs::rename(&tmp, path));
    if written.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    written
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::atomic::AtomicUsize;

    struct TempDir(PathBuf);

    impl TempDir {
        fn new() -> TempDir {
            static COUNTER: AtomicUsize = AtomicUsize::new(0);
            let n = COUNTER.fetch_add(1, Ordering::Relaxed);
            let dir = std::env::temp_dir().join(format!("candybar-sdk-test-{}-{n}", std::process::id()));
            let _ = fs::remove_dir_all(&dir);
            fs::create_dir_all(&dir).unwrap();
            TempDir(dir)
        }

        fn path(&self, name: &str) -> PathBuf {
            self.0.join(name)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn env_for(result_file: &Path) -> HashMap<&'static str, OsString> {
        HashMap::from([
            (
                ENV_INVOCATION_ID,
                OsString::from("0199f1c2-0000-7000-8000-000000000001"),
            ),
            (
                ENV_CANDYBAR_ID,
                OsString::from("0199f1c2-0000-7000-8000-000000000002"),
            ),
            (ENV_TRIGGER, OsString::from("scheduled")),
            (ENV_TICK_ID, OsString::from("tick:29883066")),
            (ENV_RESULT_FILE, result_file.as_os_str().to_owned()),
        ])
    }

    /// Run a handler through the protocol; return (exit code, result file, stderr).
    fn exec<F, R>(env: &HashMap<&'static str, OsString>, handler: F) -> (u8, Option<String>, String)
    where
        F: FnOnce(&Context) -> R,
        R: IntoOutcome,
    {
        let mut stderr = Vec::new();
        let code = invoke_to(|name| env.get(name).cloned(), handler, &mut stderr);
        let result = env
            .get(ENV_RESULT_FILE)
            .filter(|raw| !raw.is_empty())
            .and_then(|path| fs::read_to_string(path).ok());
        (code, result, String::from_utf8(stderr).unwrap())
    }

    #[test]
    fn bare_success_writes_zero_metrics() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, stderr) = exec(&env, |_| ());
        assert_eq!(code, EXIT_SUCCESS);
        assert_eq!(
            result.unwrap(),
            r#"{"status":"success","metrics":{"token_spend":0}}"#
        );
        assert!(stderr.is_empty());
    }

    #[test]
    fn success_with_message_and_accumulated_metrics() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, _) = exec(&env, |ctx| -> Result<&str, MetricError> {
            ctx.add_tokens(100);
            ctx.add_tokens(23);
            ctx.add_cost(0.25)?;
            Ok("done")
        });
        assert_eq!(code, EXIT_SUCCESS);
        assert_eq!(
            result.unwrap(),
            r#"{"status":"success","message":"done","metrics":{"token_spend":123,"cost_usd":0.25}}"#
        );
    }

    #[test]
    fn explicit_fail_keeps_metrics_and_exits_one() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, stderr) = exec(&env, |ctx| {
            ctx.add_tokens(7);
            Outcome::fail("quota")
        });
        assert_eq!(code, EXIT_FAIL);
        assert_eq!(
            result.unwrap(),
            r#"{"status":"fail","message":"quota","metrics":{"token_spend":7}}"#
        );
        assert!(stderr.contains("quota"), "{stderr}");
    }

    #[test]
    fn err_maps_to_fail_with_display_text_and_keeps_metrics() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, stderr) = exec(&env, |ctx| -> Result<(), Box<dyn Error>> {
            ctx.add_tokens(11);
            Err("boom".into())
        });
        assert_eq!(code, EXIT_FAIL);
        assert_eq!(
            result.unwrap(),
            r#"{"status":"fail","message":"boom","metrics":{"token_spend":11}}"#
        );
        assert!(stderr.contains("invocation failed: boom"), "{stderr}");
    }

    #[test]
    fn nul_never_reaches_the_result_or_stderr() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let long_tail = "x".repeat(2000);
        let (code, result, stderr) = exec(&env, |_| -> Result<(), String> {
            Err(format!("bad\0byte {long_tail}"))
        });
        assert_eq!(code, EXIT_FAIL);
        let result = result.unwrap();
        assert!(result.contains("\"message\":\"bad\u{FFFD}byte x"), "{result}");
        assert!(!result.contains('\0') && !result.contains("\\u0000"));
        assert!(!stderr.contains('\0'), "{stderr:?}");
        assert!(stderr.contains("bad\u{FFFD}byte"));
        // The stderr line carries the same capped text as the result file.
        assert!(
            stderr.trim_end().chars().count()
                <= "candybar-sdk: invocation failed: ".len() + MESSAGE_MAX_CHARS
        );
    }

    #[test]
    fn err_with_empty_display_still_has_a_message() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, _) = exec(&env, |_| -> Result<(), String> { Err(String::new()) });
        assert_eq!(code, EXIT_FAIL);
        assert!(result.unwrap().contains("empty message"));
    }

    #[test]
    fn panic_maps_to_fail_and_keeps_metrics() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, _) = exec(&env, |ctx| -> Outcome {
            ctx.add_tokens(13);
            panic!("kaboom");
        });
        assert_eq!(code, EXIT_FAIL);
        assert_eq!(
            result.unwrap(),
            r#"{"status":"fail","message":"panic: kaboom","metrics":{"token_spend":13}}"#
        );
    }

    #[test]
    fn formatted_panic_payload_is_captured() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, _) = exec(&env, |_| -> Outcome { panic!("bad value {}", 42) });
        assert_eq!(code, EXIT_FAIL);
        assert!(result.unwrap().contains(r#""message":"panic: bad value 42""#));
    }

    #[test]
    fn non_string_panic_payload_is_named() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, _) = exec(&env, |_| -> Outcome { std::panic::panic_any(7_u32) });
        assert_eq!(code, EXIT_FAIL);
        assert!(result.unwrap().contains("non-string panic payload"));
    }

    #[test]
    fn invalid_costs_are_rejected_and_leave_the_total_unchanged() {
        let tmp = TempDir::new();
        let ctx = Context::from_lookup(|name| env_for(&tmp.path("r.json")).get(name).cloned()).unwrap();
        ctx.add_cost(0.5).unwrap();
        for bad in [-1.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let err = ctx.add_cost(bad).unwrap_err();
            assert!(err.to_string().contains("finite number >= 0"), "{err}");
        }
        assert_eq!(ctx.cost_usd(), Some(0.5));
        let overflow = ctx
            .add_cost(f64::MAX)
            .and_then(|()| ctx.add_cost(f64::MAX))
            .unwrap_err();
        assert!(overflow.to_string().contains("overflows"), "{overflow}");
        assert_eq!(ctx.cost_usd(), Some(0.5 + f64::MAX));
    }

    #[test]
    fn bad_cost_propagated_with_question_mark_fails_the_invocation() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, _) = exec(&env, |ctx| -> Result<(), MetricError> {
            ctx.add_tokens(5);
            ctx.add_cost(-1.0)?;
            Ok(())
        });
        assert_eq!(code, EXIT_FAIL);
        let result = result.unwrap();
        assert!(result.starts_with(r#"{"status":"fail","message":"cost_usd must be a finite number >= 0"#));
        assert!(result.ends_with(r#""metrics":{"token_spend":5}}"#), "{result}");
    }

    #[test]
    fn negative_zero_cost_is_reported_as_zero() {
        let tmp = TempDir::new();
        let ctx = Context::from_lookup(|name| env_for(&tmp.path("r.json")).get(name).cloned()).unwrap();
        ctx.add_cost(-0.0).unwrap();
        assert_eq!(ctx.cost_usd().unwrap().to_bits(), 0.0_f64.to_bits());
    }

    #[test]
    fn token_spend_saturates_at_the_registry_bound() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, _) = exec(&env, |ctx| {
            ctx.add_tokens(u64::MAX);
            ctx.add_tokens(5);
        });
        assert_eq!(code, EXIT_SUCCESS);
        assert!(result.unwrap().contains(r#""token_spend":9223372036854775807}"#));
        assert_eq!(
            encode_result(Status::Success, None, u64::MAX, None)
                .matches("9223372036854775807")
                .count(),
            1
        );
    }

    #[test]
    fn threads_can_report_spend_through_a_shared_context() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("result.json"));
        let (code, result, _) = exec(&env, |ctx| {
            std::thread::scope(|scope| {
                for _ in 0..8 {
                    scope.spawn(|| {
                        for _ in 0..100 {
                            ctx.add_tokens(1);
                        }
                        ctx.add_cost(0.5).unwrap();
                    });
                }
            });
        });
        assert_eq!(code, EXIT_SUCCESS);
        assert_eq!(
            result.unwrap(),
            r#"{"status":"success","metrics":{"token_spend":800,"cost_usd":4.0}}"#
        );
    }

    #[test]
    fn manual_trigger_has_no_tick() {
        let tmp = TempDir::new();
        let mut env = env_for(&tmp.path("result.json"));
        env.insert(ENV_TRIGGER, "manual".into());
        env.insert(ENV_TICK_ID, "".into());
        let ctx = Context::from_lookup(|name| env.get(name).cloned()).unwrap();
        assert_eq!(ctx.trigger(), &Trigger::Manual);
        assert_eq!(ctx.tick_id(), None);
        env.remove(ENV_TICK_ID); // unset is tolerated the same way
        let ctx = Context::from_lookup(|name| env.get(name).cloned()).unwrap();
        assert_eq!(ctx.tick_id(), None);
    }

    #[test]
    fn context_exposes_every_env_field() {
        let tmp = TempDir::new();
        let path = tmp.path("result.json");
        let env = env_for(&path);
        let ctx = Context::from_lookup(|name| env.get(name).cloned()).unwrap();
        assert_eq!(ctx.invocation_id(), "0199f1c2-0000-7000-8000-000000000001");
        assert_eq!(ctx.candybar_id(), "0199f1c2-0000-7000-8000-000000000002");
        assert_eq!(ctx.trigger(), &Trigger::Scheduled);
        assert_eq!(ctx.tick_id(), Some("tick:29883066"));
        assert_eq!(ctx.result_file(), path.as_path());
        assert_eq!((ctx.token_spend(), ctx.cost_usd()), (0, None));
    }

    #[test]
    fn triggers_parse_and_unknown_ones_pass_through() {
        assert_eq!(Trigger::parse("scheduled"), Trigger::Scheduled);
        assert_eq!(Trigger::parse("misfire"), Trigger::Misfire);
        assert_eq!(Trigger::parse("manual").to_string(), "manual");
        let other = Trigger::parse("replay");
        assert_eq!(other, Trigger::Other("replay".into()));
        assert_eq!(other.as_str(), "replay");

        let tmp = TempDir::new();
        let mut env = env_for(&tmp.path("result.json"));
        env.insert(ENV_TRIGGER, "replay".into());
        let (code, result, _) = exec(&env, |ctx| ctx.trigger().to_string());
        assert_eq!(code, EXIT_SUCCESS);
        assert!(result.unwrap().contains(r#""message":"replay""#));
    }

    #[test]
    fn outside_the_executor_writes_nothing_and_exits_two() {
        let ran = std::cell::Cell::new(false);
        let env: HashMap<&'static str, OsString> = HashMap::new();
        let (code, result, stderr) = exec(&env, |_| ran.set(true));
        assert_eq!(code, EXIT_PROTOCOL);
        assert!(result.is_none());
        assert!(!ran.get(), "handler must not run without the protocol env");
        for name in REQUIRED_ENV {
            assert!(stderr.contains(name), "{stderr}");
        }
    }

    #[test]
    fn partial_env_writes_a_fail_result_naming_the_missing_var() {
        let tmp = TempDir::new();
        let mut env = env_for(&tmp.path("result.json"));
        env.remove(ENV_CANDYBAR_ID);
        env.insert(ENV_TRIGGER, "".into()); // empty counts as missing
        let ran = std::cell::Cell::new(false);
        let (code, result, stderr) = exec(&env, |_| ran.set(true));
        assert_eq!(code, EXIT_PROTOCOL);
        assert!(!ran.get());
        assert_eq!(
            result.unwrap(),
            r#"{"status":"fail","message":"not running under the candybar executor: missing CANDYBAR_ID, CANDYBAR_TRIGGER","metrics":{"token_spend":0}}"#
        );
        assert!(
            stderr.contains("missing CANDYBAR_ID, CANDYBAR_TRIGGER"),
            "{stderr}"
        );
    }

    #[cfg(unix)]
    #[test]
    fn non_unicode_identity_is_a_protocol_error() {
        use std::os::unix::ffi::OsStringExt;
        let tmp = TempDir::new();
        let mut env = env_for(&tmp.path("result.json"));
        env.insert(ENV_CANDYBAR_ID, OsString::from_vec(vec![0xff, 0xfe]));
        let err = Context::from_lookup(|name| env.get(name).cloned()).unwrap_err();
        assert_eq!(err.not_unicode(), [ENV_CANDYBAR_ID]);
        assert!(err.missing().is_empty());
        assert!(err.to_string().ends_with("not valid UTF-8: CANDYBAR_ID"), "{err}");
    }

    #[test]
    fn unwritable_result_file_exits_one_with_stderr() {
        let tmp = TempDir::new();
        let env = env_for(&tmp.path("no-such-dir/result.json"));
        let (code, result, stderr) = exec(&env, |_| ());
        assert_eq!(code, EXIT_FAIL);
        assert!(result.is_none());
        assert!(stderr.contains("cannot write result file"), "{stderr}");
        assert!(
            fs::read_dir(&tmp.0).unwrap().next().is_none(),
            "no temp file left behind"
        );
    }

    #[test]
    fn write_replaces_an_existing_file_and_leaves_no_temp() {
        let tmp = TempDir::new();
        let path = tmp.path("result.json");
        fs::write(&path, "stale").unwrap();
        let (code, result, _) = exec(&env_for(&path), |_| ());
        assert_eq!(code, EXIT_SUCCESS);
        assert!(result.unwrap().starts_with(r#"{"status":"success""#));
        let names: Vec<_> = fs::read_dir(&tmp.0)
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(names, [OsString::from("result.json")]);
    }

    #[test]
    fn long_and_exotic_messages_stay_valid_and_capped() {
        let exotic = "quote\" backslash\\ newline\n tab\t bell\u{07} nul\u{0} esc\u{1b} emoji🍬 é";
        let encoded = encode_result(Status::Success, Some(exotic), 0, None);
        assert_eq!(
            encoded,
            "{\"status\":\"success\",\"message\":\"quote\\\" backslash\\\\ newline\\n tab\\t bell\\u0007 nul\u{FFFD} esc\\u001b emoji🍬 é\",\"metrics\":{\"token_spend\":0}}"
        );

        let long: String = "🍬".repeat(2000);
        let encoded = encode_result(Status::Fail, Some(&long), 0, None);
        let message = encoded
            .split("\"message\":\"")
            .nth(1)
            .unwrap()
            .split('"')
            .next()
            .unwrap();
        assert_eq!(message.chars().count(), MESSAGE_MAX_CHARS);
    }

    #[test]
    fn cost_encodings_are_json_numbers() {
        for (cost, expected) in [
            (0.0042, "0.0042"),
            (2.0, "2.0"),
            (0.0, "0.0"),
            (1e-7, "1e-7"),
            (1e21, "1e21"),
        ] {
            let encoded = encode_result(Status::Success, None, 0, Some(cost));
            assert!(
                encoded.ends_with(&format!("\"cost_usd\":{expected}}}}}")),
                "{encoded}"
            );
        }
        // Invalid costs never reach the file, even through the raw encoder.
        for bad in [-1.0, f64::NAN, f64::INFINITY] {
            assert!(!encode_result(Status::Success, None, 0, Some(bad)).contains("cost_usd"));
        }
    }

    #[test]
    fn handler_return_types_convert() {
        assert_eq!(().into_outcome(), Outcome::success());
        assert_eq!("hi".into_outcome(), Outcome::success_with("hi"));
        assert_eq!(String::from("hi").into_outcome().message(), Some("hi"));
        assert_eq!(Ok::<(), String>(()).into_outcome(), Outcome::success());
        assert_eq!(
            Ok::<Outcome, String>(Outcome::fail("x")).into_outcome().status(),
            Status::Fail
        );
        assert_eq!(Err::<(), &str>("nope").into_outcome(), Outcome::fail("nope"));
    }

    #[test]
    fn protocol_constants_match_the_executor_contract() {
        assert_eq!(REQUIRED_ENV.len() + 1, 5, "five CANDYBAR_* vars in total");
        assert_eq!((EXIT_SUCCESS, EXIT_FAIL, EXIT_PROTOCOL), (0, 1, 2));
        assert_eq!(TOKEN_SPEND_MAX, 9_223_372_036_854_775_807);
        assert_eq!(MESSAGE_MAX_CHARS, 500);
    }

    #[test]
    fn context_is_send_and_sync() {
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<Context>();
    }
}
