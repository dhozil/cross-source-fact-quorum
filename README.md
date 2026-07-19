# Cross-Source Fact Quorum

A reusable GenLayer Intelligent Contract primitive for verifying a
single fact against **multiple independent web sources** within one
consensus round, instead of trusting a single source per observation.

## Live deployment (GenLayer Bradbury testnet)

This contract is deployed and verified working on the **Bradbury**
testnet, not just tested locally in isolation. The repository in this
submission is the exact source deployed at the address below.

| | |
|---|---|
| Network | GenLayer Bradbury Testnet |
| Contract address | `0xB149fd61faE9Ea03D01a142f9e938d9D2c5B4064` |
| Explorer | [[explorer-bradbury.genlayer.com/address/0xB149fd61faE9Ea03D01a142f9e938d9D2c5B4064](https://explorer-bradbury.genlayer.com/address/0xB149fd61faE9Ea03D01a142f9e938d9D2c5B4064)] |

Anyone can independently verify the deployment and re-run
`create_fact` / `submit_verification` against this address through the
explorer or GenLayer Studio pointed at Bradbury.

## Why this is a distinct primitive

The other two primitives in this submission set (Temporal Drift
Oracle, Graduated Confidence Consensus) reach validator consensus on a
judgement derived from one source. Cross-Source Fact Quorum builds the
source-quorum requirement **into the non-deterministic block itself**:
every validator independently fetches all configured source URLs,
extracts the answer from each one, and only accepts a value if a
majority of sources agree with each other internally. GenLayer
validator consensus is then reached on top of that already
source-checked result.

This gives two independent layers of agreement:
1. **Source-level quorum** (inside a single validator's run): does a
   majority of the configured sources agree? If not, the contract
   reports `no_quorum` explicitly instead of guessing from whichever
   source happened to load.
2. **Validator-level consensus** (across GenLayer validators, via the
   equivalence principle): do independently-run source-quorum checks
   converge on the same final value?

A single stale, mis-scraped, or unreachable page can no longer
single-handedly determine on-chain state, and no single validator's
network conditions can either. This is the property that makes it a
genuine oracle primitive rather than a "fetch one page, ask an LLM"
demo: price/availability checks across multiple listing sites, event
outcome confirmation across multiple news outlets, service status
checks across multiple status pages, and similar real-world facts
where any one source can be wrong.

## Consensus design

`submit_verification()` runs a non-deterministic block that, for every
configured source URL:
1. Fetches the page text.
2. Asks the LLM to extract the answer to the fact's question from that
   source alone, returning `{"value": ..., "found": true/false}`.

It then tallies the extracted values across all reachable sources and
only accepts a result if it has a strict majority (`> half` of
reachable sources). Otherwise the block returns `no_quorum`.

`gl.eq_principle.prompt_comparative` is used with a principle that
requires the final `value` field (including `no_quorum` as a valid
outcome) to match exactly across validators, while `sources_agreeing`
may differ by 1 (to tolerate transient source reachability) and
`notes` may vary freely. This is a genuinely custom equivalence rule
built around a two-layer agreement structure, not a default
strict-match or a single-source "AI decides X" wrapper.

## Public interface

| Method | Type | Description |
|---|---|---|
| `create_fact(fact_id, question, source_urls)` | write | Registers a fact with at least 3 independent source URLs. |
| `submit_verification(fact_id)` | write | Runs the source-quorum check, reaches validator consensus, appends to history. |
| `get_current_value(fact_id)` | view | Latest agreed value and verification count. |
| `get_history(fact_id)` | view | Full append-only verification history, including source agreement ratio. |
| `get_sources(fact_id)` | view | The configured source URLs for a fact. |
| `list_fact_ids()` | view | All tracked fact ids. |

`create_fact` requires at least 3 `source_urls` — with only 2 sources,
"majority" can only ever be a tie or full agreement, which defeats the
purpose of a quorum.

## Example use cases

- **Price/availability oracles** that check a product or service
  across several independent listing pages instead of one, resistant
  to any single site being outdated or wrong.
- **Event outcome confirmation** (e.g. "did event X happen by date Y?")
  checked against multiple independent news sources before an
  on-chain payout or settlement triggers.
- **Service status oracles** for insurance or SLA contracts, checking
  multiple independent status pages instead of trusting one.

## Implementation notes (from real Bradbury testing)

This contract reuses SDK patterns confirmed while building the
companion Temporal Drift Oracle and Graduated Confidence Consensus
primitives on Bradbury:
- `DynArray` fields inside a nested `@allow_storage` dataclass must be
  allocated via `gl.storage.inmem_allocate(DynArray[...], [])`, not
  instantiated directly — this applies to both `source_urls: DynArray[str]`
  and `history: DynArray[VerificationEntry]` here.
- Web content fetching uses `gl.nondet.web.render(url, mode="text")`.
- LLM calls go through `gl.nondet.exec_prompt(...)`.
- Per-source fetch/parse failures are caught individually inside the
  non-deterministic block so one broken URL doesn't fail the whole
  verification — it's simply excluded from the quorum tally.

## Testing notes

Suggested manual walkthrough in GenLayer Studio:
1. Deploy with no constructor args.
2. `create_fact("fact1", "<a factual yes/no or short-answer question>", ["<url1>", "<url2>", "<url3>"])`
   using at least 3 stable, publicly reachable pages that plausibly
   contain the same answer.
3. `submit_verification("fact1")` — inspect `get_history("fact1")` to
   confirm `value`, `sources_agreeing`, and `sources_total` are
   consistent with the configured sources.
4. Repeat with a question where sources are likely to disagree, to
   confirm the contract correctly returns `no_quorum` rather than
   forcing a guess.

Edge cases worth testing explicitly:
- A `source_urls` list where one URL is intentionally broken/unreachable
  — confirm the verification still completes using the remaining
  sources rather than failing outright.
- A question where exactly half of sources agree (no strict majority)
  — confirm the result is `no_quorum`.
