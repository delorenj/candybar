# shellcheck shell=bash
# Shared data-only fleet.env loader. This file is trusted template code; the
# fleet.env it reads is configuration data and is never evaluated by a shell.

if [[ "${__PJANGLER_FLEET_ENV_LIBRARY_LOADED:-0}" == "1" ]]; then
  return 0
fi
__PJANGLER_FLEET_ENV_LIBRARY_LOADED=1

# fleet.env is shared configuration, not authority to inject code into Python,
# shell, Node, or dynamic-loader children. PATH remains intact so explicitly
# configured Hermes/PJangler/provider tools still resolve.
subprocess_injection_key_is_unsafe() {
  local key="$1" loader_key="$1"
  case "$key" in
    PYTHONPATH|PYTHONHOME|PYTHONSTARTUP|PYTHONUSERBASE|\
    BASH_ENV|ENV|BASHOPTS|SHELLOPTS|BASH_COMPAT|BASH_LOADABLES_PATH|\
    BASH_XTRACEFD|PROMPT_COMMAND|PS0|PS1|PS2|PS3|PS4|\
    NODE_OPTIONS|NODE_PATH|GLIBC_TUNABLES|BASH_FUNC_*|DYLD_*)
      return 0
      ;;
  esac

  # GNU and multilib loaders consume the same control stem with an optional
  # _32/_64 ABI suffix. Do not delete unrelated application keys such as
  # LD_SDK_KEY merely because they share the LD_ prefix.
  case "$loader_key" in
    LD_*_32|LD_*_64) loader_key="${loader_key%_*}" ;;
  esac
  case "$loader_key" in
    LD_ASSUME_KERNEL|LD_AUDIT|LD_BIND_NOT|LD_BIND_NOW|LD_DEBUG|\
    LD_DEBUG_OUTPUT|LD_DYNAMIC_WEAK|LD_HWCAP_MASK|LD_LIBRARY_PATH|\
    LD_ORIGIN_PATH|LD_POINTER_GUARD|LD_PREFER_MAP_32BIT_EXEC|LD_PRELOAD|\
    LD_PROFILE|LD_PROFILE_OUTPUT|LD_SHOW_AUXV|LD_TRACE_LOADED_OBJECTS|\
    LD_TRACE_PRELINKING|LD_USE_LOAD_BIAS|LD_VERBOSE|LD_WARN)
      return 0
      ;;
  esac
  return 1
}

scrub_subprocess_interpreter_injection() {
  local key declaration function_name

  builtin set +x +v
  while IFS= read -r key; do
    if subprocess_injection_key_is_unsafe "$key"; then
      case "$key" in
        BASHOPTS|SHELLOPTS|BASH_XTRACEFD)
          builtin export -n "$key" 2>/dev/null || true
          ;;
        *)
          builtin unset -v "$key" 2>/dev/null || true
          ;;
      esac
    fi
  done < <(builtin compgen -A variable)

  # Imported exported functions are live functions rather than ordinary
  # BASH_FUNC_* variables. Remove the whole class before selecting any child.
  while IFS= read -r declaration; do
    function_name="${declaration##* }"
    [[ -n "$function_name" ]] && builtin unset -f -- "$function_name"
  done < <(builtin declare -Fx)

  builtin export PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1
}

# Apply staged records as one transaction. Existing variables always win. New
# variables are removed in reverse order if any assignment/export fails.
apply_fleet_environment_records() {
  local -n __pjangler_fleet_keys_ref="$1"
  local -n __pjangler_fleet_values_ref="$2"
  local __pjangler_fleet_index __pjangler_fleet_key
  local -a __pjangler_fleet_applied=()

  if (( ${#__pjangler_fleet_keys_ref[@]} != ${#__pjangler_fleet_values_ref[@]} )); then
    builtin printf 'fleet environment apply failed: mismatched staging arrays\n' >&2
    return 1
  fi

  for ((__pjangler_fleet_index = 0; __pjangler_fleet_index < ${#__pjangler_fleet_keys_ref[@]}; __pjangler_fleet_index++)); do
    __pjangler_fleet_key="${__pjangler_fleet_keys_ref[__pjangler_fleet_index]}"
    if [[ ! "$__pjangler_fleet_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
      || subprocess_injection_key_is_unsafe "$__pjangler_fleet_key"; then
      builtin printf 'fleet environment apply failed: rejected variable name\n' >&2
      for ((__pjangler_fleet_index = ${#__pjangler_fleet_applied[@]} - 1; __pjangler_fleet_index >= 0; __pjangler_fleet_index--)); do
        builtin unset -v "${__pjangler_fleet_applied[__pjangler_fleet_index]}" 2>/dev/null || true
      done
      return 1
    fi

    if builtin declare -p "$__pjangler_fleet_key" >/dev/null 2>&1; then
      continue
    fi
    if ! builtin printf -v "$__pjangler_fleet_key" '%s' \
      "${__pjangler_fleet_values_ref[__pjangler_fleet_index]}"; then
      builtin printf 'fleet environment apply failed: assignment rejected\n' >&2
      for ((__pjangler_fleet_index = ${#__pjangler_fleet_applied[@]} - 1; __pjangler_fleet_index >= 0; __pjangler_fleet_index--)); do
        builtin unset -v "${__pjangler_fleet_applied[__pjangler_fleet_index]}" 2>/dev/null || true
      done
      return 1
    fi
    __pjangler_fleet_applied+=("$__pjangler_fleet_key")
    if ! builtin export "$__pjangler_fleet_key"; then
      builtin printf 'fleet environment apply failed: export rejected\n' >&2
      for ((__pjangler_fleet_index = ${#__pjangler_fleet_applied[@]} - 1; __pjangler_fleet_index >= 0; __pjangler_fleet_index--)); do
        builtin unset -v "${__pjangler_fleet_applied[__pjangler_fleet_index]}" 2>/dev/null || true
      done
      return 1
    fi
  done
}

# Consume a parser child through a complete, double-NUL-terminated protocol.
# Nothing reaches this shell until child status and the entire frame validate.
import_fleet_environment_stream() {
  local __pjangler_fleet_fd="$1" __pjangler_fleet_pid="$2"
  local __pjangler_fleet_count __pjangler_fleet_index
  local __pjangler_fleet_record __pjangler_fleet_key __pjangler_fleet_value
  local -a __pjangler_fleet_records=() __pjangler_fleet_keys=() __pjangler_fleet_values=()
  local -A __pjangler_fleet_seen=()

  builtin mapfile -d '' -t -u "$__pjangler_fleet_fd" __pjangler_fleet_records || true
  exec {__pjangler_fleet_fd}<&-
  if ! builtin wait "$__pjangler_fleet_pid"; then
    builtin printf 'fleet environment frame rejected: parser child failed\n' >&2
    return 1
  fi

  __pjangler_fleet_count="${#__pjangler_fleet_records[@]}"
  if (( __pjangler_fleet_count < 3 )) \
    || [[ "${__pjangler_fleet_records[0]}" != "PJANGLER_FLEET_ENV_V1" ]] \
    || [[ "${__pjangler_fleet_records[__pjangler_fleet_count - 2]}" != "PJANGLER_FLEET_ENV_END" ]] \
    || [[ -n "${__pjangler_fleet_records[__pjangler_fleet_count - 1]}" ]]; then
    builtin printf 'fleet environment frame rejected: incomplete framing\n' >&2
    return 1
  fi

  for ((__pjangler_fleet_index = 1; __pjangler_fleet_index < __pjangler_fleet_count - 2; __pjangler_fleet_index++)); do
    __pjangler_fleet_record="${__pjangler_fleet_records[__pjangler_fleet_index]}"
    if [[ "$__pjangler_fleet_record" != *=* ]]; then
      builtin printf 'fleet environment frame rejected: malformed record\n' >&2
      return 1
    fi
    __pjangler_fleet_key="${__pjangler_fleet_record%%=*}"
    __pjangler_fleet_value="${__pjangler_fleet_record#*=}"
    if [[ ! "$__pjangler_fleet_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
      || subprocess_injection_key_is_unsafe "$__pjangler_fleet_key"; then
      builtin printf 'fleet environment frame rejected: unsafe variable\n' >&2
      return 1
    fi
    if [[ -n "${__pjangler_fleet_seen[$__pjangler_fleet_key]+present}" ]]; then
      builtin printf 'fleet environment frame rejected: duplicate variable\n' >&2
      return 1
    fi
    __pjangler_fleet_seen["$__pjangler_fleet_key"]=1
    __pjangler_fleet_keys+=("$__pjangler_fleet_key")
    __pjangler_fleet_values+=("$__pjangler_fleet_value")
  done

  apply_fleet_environment_records __pjangler_fleet_keys __pjangler_fleet_values
}

# fleet.env is parsed by an isolated interpreter. The parser path is explicit
# so every caller binds to its colocated, attested copy rather than PATH state.
import_fleet_environment() {
  local __pjangler_fleet_path="$1" __pjangler_fleet_parser="$2"
  local __pjangler_fleet_python __pjangler_fleet_fd __pjangler_fleet_pid

  if [[ "$__pjangler_fleet_parser" != /* ]] \
    || [[ ! -f "$__pjangler_fleet_parser" || -L "$__pjangler_fleet_parser" ]]; then
    builtin printf 'fleet environment frame rejected: trusted parser is unavailable\n' >&2
    return 1
  fi
  __pjangler_fleet_python="$(builtin type -P python3)" || {
    builtin printf 'fleet environment frame rejected: python3 is unavailable\n' >&2
    return 1
  }

  exec {__pjangler_fleet_fd}< <(
    "$__pjangler_fleet_python" -I "$__pjangler_fleet_parser" "$__pjangler_fleet_path"
  )
  __pjangler_fleet_pid=$!
  import_fleet_environment_stream "$__pjangler_fleet_fd" "$__pjangler_fleet_pid"
}

# Public loader used by rendered provisioners, launchers, and maintenance
# scripts. Missing configuration remains optional; every existing path,
# including symlinks and non-regular files, must pass the parser's file checks.
load_fleet_environment() {
  local __pjangler_fleet_path="$1" __pjangler_fleet_parser="$2"
  scrub_subprocess_interpreter_injection
  if [[ ! -e "$__pjangler_fleet_path" && ! -L "$__pjangler_fleet_path" ]]; then
    return 0
  fi
  if ! import_fleet_environment "$__pjangler_fleet_path" "$__pjangler_fleet_parser"; then
    builtin printf 'fleet environment import failed: %s\n' "$__pjangler_fleet_path" >&2
    return 1
  fi
  scrub_subprocess_interpreter_injection
}
