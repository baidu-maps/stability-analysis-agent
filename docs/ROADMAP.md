# Roadmap

This is the long-form version of what we'd like to ship next in
**Stability Analysis Agent**. The README provides the project overview and
links here for the detailed roadmap, deferred ideas, RFCs, and design notes that haven't
earned a place on the main page yet.

## TL;DR

| Track | Status | Next milestone |
|---|---|---|
| Crash analysis | ✅ GA | more crash-log format adapters |
| Plugin / Skill framework | ✅ GA | presets marketplace, JSON Schema export |
| Closed-loop presets | ✅ GA | presets for engine-build, iOS-XCUITest, Hypium |
| ANR analysis | 🚧 in design | Android `am_anr`, iOS watchdog |
| OOM / memory | 📋 planned | heap snapshot diffing |
| Freeze / hang | 📋 planned | stack sampling + thread state |
| Distributed traces | 💭 exploring | open-telemetry-aware timeline |

## Currently in design

### ANR (Application Not Responding) analyzer

Different from crash: ANR is "the app is alive but stuck." It needs:

- A separate parser for `am_anr` / `am_anr_info` / iOS `RBSAssertionReliability`
  / Harmony `AppFreeze`.
- A **stuckness** detector that operates on absence-of-progress rather than
  presence-of-failure.
- Thread state, mutex wait chains, and last-known-good source frame.

Tracked under issue tracker (search for `feature/analyzer/anr`).

### Skill marketplace / registry

Today the only install path is "drop a directory or `.zip` and run
`sa-agent skill install`". A registry of community Skills could:

- Provide a CLI flow `sa-agent skill install <registry-name>` with
  signature verification.
- List presets published by trusted publishers (close-loop maintainers,
  major OSS teams).
- Make Skill contributions discoverable to newcomers.

We are exploring — not committed — because a registry adds operational
overhead and a permanent moderation surface. The path of least resistance
is still: ship the template, let folks publish to GitHub.

## Currently planned

### `automation-testing` → engine-build preset

A 4th preset that wraps `mk/cmake/build.sh` for projects whose primary
verification is "the engine still builds". Today this exists as a
template in the closed-source workspace; lifting it into the open is
a low-effort P1.

### `automation-testing` → iOS XCUITest preset

iOS teams primarily verify via XCUITest `xcodebuild test`. A pre-filled
preset with the right `xcodebuild` invocation + simulator handling
would make the closed loop accessible to iOS-only teams.

### `automation-testing` → Harmony Hypium preset

Same idea for Harmony. This exists partially in the closed-source
workspace (`harmony-hypium-demo-verify`); promoting it to a `--preset`
is straightforward.

### OOM heap snapshot diffing

Given two heap snapshots (before / after a suspected leak), point the
agent at the diff. Produces a "top N retainers" view it can reason about.
RAG-friendly: leak signatures already exist in the literature.

## Currently exploring

### Distributed trace correlation

Stability events that span multiple services (a slow downstream is a
"freeze" at the client) are common. Correlating a stack with an OpenTelemetry
trace would let the agent reason about causality, not just symptom.

Stretch goal — no committed timeline.

### Stream LLM output into the daemon

The daemon already speaks SSE; the missing piece is piping the LLM's
token stream back. Once this lands, the IDE integration story gets a
few orders of magnitude more responsive.

### Reach parity for Windows native dumps

We support Windows `.dmp` files today but with limited symbolization
coverage (no `dbghelp` yet). Help wanted.

## Community contributions — what we'd love to see

The fastest way to influence this roadmap is to **open a Skill / Tool /
Workflow PR** with your domain-specific code. Anyone can ship one of:

| You want to… | See |
|---|---|
| Add a Symbolization Backend (Tool) | [extensions/tools/example_tool.py](../../extensions/tools/example_tool.py) |
| Add a Bug-Platform Fetcher (Skill) | preset `bug-platform-fetcher` + [BUG_PLATFORM_FETCHER_TEMPLATE.md](./BUG_PLATFORM_FETCHER_TEMPLATE.md) |
| Add a Verifier for Your Stack (Skill) | preset `automation-testing` + [CLOSE_LOOP_SKILL_TEMPLATES.md](./CLOSE_LOOP_SKILL_TEMPLATES.md) |
| Replace / extend `sa-agent` core (Workflow) | [TOOL_SYSTEM_EXTENSION.md](./tools/tool_system/TOOL_SYSTEM_EXTENSION.md) |
| Add a new Crash Log format adapter | [CRASH_LOG_FORMATS.md](./tools/CRASH_LOG_FORMATS.md) |

Your PR becomes part of someone's `quick start` story next release.
That's how the loop keeps closing.

## How this document evolves

- Every minor release (v1.3, v1.4, …) — checkpoint with new items.
- Quarterly review — anything not making it to a PR gets cut from
  *Currently planned* and parked under *Considering*.
- *Currently exploring* is open-ended until someone designs it solidly.

If you're picking up work from this file, please open an Issue first so
the milestone table in the README stays consistent.
