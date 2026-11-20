# Coding broker traffic limits

This is an opt-in administrator runtime-profile setting. It does not change a
CallerPolicy, ExecutionGrant, destination, credential, workspace permission, or
execution deadline. It does not enable general network access. Production
profile changes require a separate operational approval.

## Contract and compatibility

The reviewed `host_native_coding_v1` Codex profile can contain one object,
`codex_egress_limits`. Every member is required when the object is present:

| Member | Type and meaning |
| --- | --- |
| `version` | Exact integer `1` |
| `total_bytes` | Positive integer cumulative accounting limit, or the literal string `"disabled"` |
| `total_connections` | Positive integer cumulative connection limit, or `"disabled"` |
| `request_bytes` | Positive integer maximum received JSON body and normalized upstream JSON body |
| `response_bytes` | Positive integer maximum upstream HTTP response bytes, including headers and chunk framing |

Integers are strict and at most `2**63 - 1`; booleans, numeric strings, zero,
negative numbers, nulls, unknown fields and unknown versions are rejected. The
integer representation bound is not an operational memory recommendation.
Per-message limits cannot be disabled. Only the coding profile accepts this
object. There are no new Caller/Grant policy levels and no implicit new traffic
defaults.

When the object is absent, the old contract remains: cumulative accounting limit
`max(1 MiB, output_limit_bytes * 8)`, cumulative 32 connections, and the existing
request/image limits. Explicit null is not absence.

Admission copies an explicit object into the immutable Attempt snapshot and its
digest. Execution reads that saved object, never today's profile to fill in a
missing field. An old snapshot without the field keeps the old contract after an
upgrade. A present malformed pin fails closed. Durable resume inherits the
predecessor's pin, including absence, and preserves the predecessor snapshot and
digest. A fresh admission is required to use a changed profile setting.

The two cumulative cutoffs can be disabled independently without increasing the
output budget. The existing absolute/soft deadlines, cancellation, four admitted
concurrent broker handlers, fixed endpoints, TLS checks, authentication binding,
per-request reauthorization, model/tool validation, and stop confirmation remain.
Existing image-input accounting/delivery guards remain separate; disabling the
generic cumulative cutoffs does not disable those image guards.

## Candidate configuration, not a deployment instruction

The following values were used in bounded synthetic memory experiments. They
are provisional buffer-protection choices, not measured requirements for normal
coding, and are not an implicit default:

```json
{
  "codex_egress_limits": {
    "version": 1,
    "total_bytes": "disabled",
    "total_connections": "disabled",
    "request_bytes": 16777216,
    "response_bytes": 33554432
  }
}
```

The request limit covers the complete JSON history, tool results/definitions and
other accepted fields, not just the newest prompt. Explicit policies keep UTF-8
during normalization and check the normalized body before upstream transmission.
Legacy JSON escaping remains unchanged. The fixed 16 KiB HTTP header limit is
separate. Unsupported request framing, including chunked requests, remains
rejected.

Subscription responses are parsed and buffered before redaction/delivery. The
response limit is checked before feeding each raw read into the HTTP parser or
growing the response body. Reads request at most the remaining allowance plus
one overflow-detection byte. Redaction expansion is checked before allocating
each replacement, and the resulting body is also bounded by `response_bytes`.
The generated local response header is separate from that body. The setting is
not a TLS/TCP byte limit.

## Numeric measurements

For an explicit pinned policy, the final `codex_egress_metrics` Attempt event
contains only these integer fields. Legacy/unconfigured executions do not gain
a new event:

- `accounted_request_bytes`: headers plus declared request body length reserved
  under the cumulative accounting rule; existing fixed-image URI exclusions
  remain. It can exceed actual request reads after a failed/incomplete request.
- `request_bytes_read`: bytes delivered by StreamReader to the broker, including
  headers and partial body chunks. This is neither declared length nor the
  normalized body sent upstream. Bytes still buffered when cancellation occurs
  are not counted as application reads.
- `response_bytes_read`: raw HTTP bytes delivered by the upstream StreamReader,
  including headers/framing and the overflow-detection byte, when present.
- `connections_started`: all entered handlers, including rejected connections.
- `peak_concurrent`: peak admitted handlers, at most four.
- `cap_reason`: first cap hit: `0` none, `1` cumulative bytes, `2` cumulative
  connections, `3` request body, `4` response/redaction, `5` concurrency.

These are application observations, not exact socket/TLS traffic, billing,
tokens, or confirmed upstream writes. No body, header, URL, credential, account
identity, or prompt is included. The event is persisted after runtime cleanup
and before normal Attempt completion. A controller crash or already-terminal
Attempt can prevent that terminal metric event; missing metrics are unknown,
never zero or an inferred byte count.

## Memory and operational verification

Do not size controller memory as just `4 * (request_bytes + response_bytes)`.
JSON decoding creates Python strings/containers; serialization creates further
strings/bytes; HTTP parsing, redaction, and transport buffering create additional
copies. Four active responses can overlap with another connection's parsing.
The broker is in the Kotoyose controller process, outside the provider's systemd
MemoryMax. Provider memory configuration does not protect controller buffers.

Candidate 16/32 MiB values therefore require controller headroom review before
activation, multiplied again for concurrently running Attempts. Synthetic
ASCII and DEL-text fixtures exercise four concurrent requests/responses and
report both Python allocation tracking and process RSS with the exact source
hash. They are scenario measurements, not a worst-case bound for arbitrary JSON
structure. Many small containers can consume more memory than flat-string
fixtures. Larger configured values require a new headroom assessment.

Before deployment, validate the complete profile, inspect a newly admitted
resolved snapshot, retain the existing deadline and destination/authentication
configuration, and confirm numeric metrics and stop proof with an authorized
run. Do not edit old Attempt snapshots or assume their settings were upgraded.
