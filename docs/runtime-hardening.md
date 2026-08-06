# Agent Runtime Hardening

This iteration adapts three mechanisms from `claude-code-from-scratch` to
KamaClaude's daemon/IPC architecture and fixes one concurrency issue found in
KamaClaude itself.

## 1. Optimistic file-write protection

- `read_file` records the absolute path, byte length, and SHA-256 of the observed file.
- `write_file` refuses to overwrite an existing file that was never read in the current run.
- Before overwrite, the current SHA-256 must still match the observed version.
- A successful write updates the observed version, so subsequent writes by the same run remain valid.
- New-file creation does not require a prior read.

Unlike the reference project's mtime-only check, hashing also detects changes that retain the same
timestamp and size. The tracker is intentionally scoped to one run. It is an optimistic guard, not
an operating-system-level compare-and-swap, so a very small validate/write race window remains.

## 2. Large tool-result artifacts

Successful tool results larger than 32 KiB are written in full under the run's `artifacts/`
directory. The model context and `tool.call_finished` event receive only:

- a head/tail preview;
- the artifact's absolute path;
- byte length;
- SHA-256.

This preserves recoverability while preventing a single shell/search result from dominating the
conversation context. Results at or below the threshold remain inline.

## 3. Fail-closed tool concurrency

`BaseTool.parallel_safe` defaults to `False`. Only a tool that explicitly opts in can overlap with
another safe tool. Consecutive safe calls use `asyncio.gather`; unsafe calls remain serial. Results
are appended to the LLM conversation in the original tool-call order even if completion order
differs.

The first opted-in production tool is `read_file`. Shell, file writes, task mutation, sub-agents,
and MCP tools remain serial until their side-effect and permission semantics are audited.

## 4. Run-level event isolation

The Core still has a process-wide EventBus, but per-run `EventWriter` instances now filter by
`run_id` and unsubscribe when closed. Concurrent runs therefore cannot contaminate each other's
`events.jsonl` files or leak closed writer callbacks.

For one-shot `kama run`, `agent.run` can atomically register a `run:<run_id>` subscription before
the background task starts. This removes the subscribe/start race and prevents one CLI from
printing another client's run. Existing global subscriptions remain supported for monitoring and
the current chat/TUI flow.

## Verification

```bash
python -m pytest -q
python scripts/benchmark_runtime_improvements.py
```

Verified on Python 3.12 under WSL:

- 284 tests passed.
- Four synthetic 50 ms I/O calls: 201.07 ms serial, 50.42 ms parallel (3.99x).
- A 100,000-byte tool result produced a 3,272-byte context reference (96.73% reduction).

The timing result is a reproducible microbenchmark for scheduling overhead, not a production p95
latency claim. Real speedup depends on tool latency, provider behavior, and how many independent
tool calls the model emits in one turn.
