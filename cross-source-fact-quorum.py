# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
Cross-Source Fact Quorum
==========================

A reusable GenLayer Intelligent Contract primitive for verifying a
single fact against MULTIPLE independent web sources within one
consensus round, rather than trusting a single source per observation.

Why this is a different consensus shape
----------------------------------------
The two companion primitives in this submission set (Temporal Drift
Oracle, Graduated Confidence Consensus) both reach validator consensus
on a judgement derived from a single source. This primitive instead
builds the source-quorum INTO the non-deterministic block itself: each
validator independently fetches all configured source URLs, extracts
the core fact from each one, and only proceeds if a majority of those
sources agree with each other. The contract then asks GenLayer
validators to reach consensus on top of that already-aggregated,
source-quorum-checked value.

This produces two layers of agreement that matter independently:
  1. Source-level quorum (inside one validator's non-deterministic
     run): does a majority of the configured sources agree on the
     fact? If not, the run reports "no_quorum" rather than guessing
     from a single source.
  2. Validator-level consensus (across GenLayer validators, via the
     equivalence principle): do independent validators, each running
     their own source-quorum check, converge on the same final value?

A single mis-scraped or stale page can no longer single-handedly
determine the on-chain fact, and a single validator's network luck
(which mirror happened to load) can no longer single-handedly
determine consensus either. This makes the primitive genuinely useful
for anything that needs a fact pulled from the messy real web with
resilience to any one source being wrong, outdated, or unreachable:
price/availability checks across multiple listing sites, event outcome
confirmation across multiple news sources, status checks across
multiple status pages for the same service, and similar.

Consensus mechanism
--------------------
For every `submit_verification` call:
  1. A non-deterministic block fetches every configured source URL for
     the fact, asks the LLM to extract a structured value
     (category/boolean/short-string) from EACH source independently,
     then computes which value (if any) a majority of sources agree
     on. If no value reaches majority, the block returns "no_quorum"
     instead of forcing a guess.
  2. `gl.eq_principle.prompt_comparative` is used with a principle
     requiring validators to agree exactly on the final "value" (the
     source-quorum result, including "no_quorum" as a valid outcome),
     while tolerating differences in which exact sources were
     reachable, their wording, and the per-source breakdown notes.
  3. On consensus, the contract appends the agreed value plus the
     source agreement ratio (e.g. "3/4 sources agreed") to an
     append-only history for the fact.
"""

from genlayer import *
from dataclasses import dataclass
import json

NO_QUORUM = "no_quorum"


# ---------------------------------------------------------------------
# Storage schema
# ---------------------------------------------------------------------

@allow_storage
@dataclass
class VerificationEntry:
    epoch: u256
    value: str
    sources_agreeing: u256
    sources_total: u256
    notes: str


@allow_storage
@dataclass
class FactQuery:
    question: str
    source_urls: DynArray[str]
    owner: Address
    verification_count: u256
    last_value: str
    history: DynArray[VerificationEntry]


class CrossSourceFactQuorum(gl.Contract):
    facts: TreeMap[str, FactQuery]

    def __init__(self):
        self.facts = TreeMap()

    # -------------------------------------------------------------
    # Write methods
    # -------------------------------------------------------------

    @gl.public.write
    def create_fact(self, fact_id: str, question: str, source_urls: list) -> None:
        """Register a fact to be verified across multiple sources.
        `source_urls` should contain at least 3 independent URLs for a
        meaningful majority quorum; 2 sources can only ever tie or
        agree, never form a real majority."""
        if fact_id in self.facts:
            raise Exception("fact_id already exists")
        if len(question.strip()) == 0:
            raise Exception("question cannot be empty")
        if len(source_urls) < 3:
            raise Exception("provide at least 3 source_urls for a meaningful quorum")

        stored_urls = gl.storage.inmem_allocate(DynArray[str], [])
        for url in source_urls:
            stored_urls.append(url)

        self.facts[fact_id] = FactQuery(
            question=question,
            source_urls=stored_urls,
            owner=gl.message.sender_address,
            verification_count=u256(0),
            last_value=NO_QUORUM,
            history=gl.storage.inmem_allocate(DynArray[VerificationEntry], []),
        )

    @gl.public.write
    def submit_verification(self, fact_id: str) -> None:
        """Independently check the fact against every configured source,
        require a majority of sources to agree before accepting a
        value, then reach validator consensus on that aggregated
        result."""
        if fact_id not in self.facts:
            raise Exception("unknown fact_id")

        fact = self.facts[fact_id]
        question = fact.question
        urls = list(fact.source_urls)
        total_sources = len(urls)

        def check_quorum() -> str:
            per_source_values = []
            sources_reached = 0

            for url in urls:
                try:
                    page_text = gl.nondet.web.render(url, mode="text")
                except Exception:
                    continue

                prompt = f"""
Question: {question}

Source content (may be truncated):
{page_text[:4000]}

Extract the answer to the question from this source ONLY.
Respond with ONLY a compact JSON object, no markdown, no commentary:
{{"value": "<short answer, a single word or short phrase>", "found": <true or false>}}
If the source does not contain enough information to answer, set
"found" to false and "value" to "unknown".
"""
                try:
                    parsed = gl.nondet.exec_prompt(prompt, response_format="json")
                except Exception:
                    continue

                sources_reached += 1
                if parsed.get("found", False):
                    value = str(parsed.get("value", "unknown")).strip().lower()
                    per_source_values.append(value)

            # Tally votes among sources that produced a value
            tally: dict = {}
            for v in per_source_values:
                tally[v] = tally.get(v, 0) + 1

            best_value = NO_QUORUM
            best_count = 0
            for v, count in tally.items():
                if count > best_count:
                    best_value = v
                    best_count = count

            # Minimum reachability invariant: at least ceil(total/2) sources
            # must be reachable before we even attempt a quorum decision.
            # If too many sources are down, partial outage must produce
            # no_quorum rather than letting one reachable source decide.
            min_reachable = (total_sources // 2) + 1
            if sources_reached < min_reachable:
                final_value = NO_QUORUM
                agreeing = 0
                notes = (
                    f"only {sources_reached} of {total_sources} sources reachable "
                    f"(minimum {min_reachable} required)"
                )
                return json.dumps(
                    {
                        "notes": notes,
                        "sources_agreeing": agreeing,
                        "sources_total": total_sources,
                        "value": final_value,
                    },
                    sort_keys=True,
                )

            # Majority is computed against total_sources (configured count),
            # not sources_reached — so a partial outage cannot lower the bar.
            majority_threshold = (total_sources // 2) + 1
            if best_count == 0 or best_count < majority_threshold:
                final_value = NO_QUORUM
                agreeing = 0
            else:
                final_value = best_value
                agreeing = best_count

            notes = f"{sources_reached} of {total_sources} sources reachable, {agreeing} agreed"

            return json.dumps(
                {
                    "notes": notes,
                    "sources_agreeing": agreeing,
                    "sources_total": total_sources,
                    "value": final_value,
                },
                sort_keys=True,
            )

        principle = """
Two answers are EQUIVALENT only if the "value" field is exactly the
same string in both answers (treat "no_quorum" as a valid value
meaning no majority was reached). The "sources_agreeing" field may
differ by up to 1 due to transient source reachability and still be
considered equivalent. The "notes" field may differ freely. If the
"value" fields differ in any way, the answers are NOT equivalent.
"""

        consensus_result = gl.eq_principle.prompt_comparative(check_quorum, principle)
        parsed = json.loads(consensus_result)

        value = parsed["value"]
        sources_agreeing = u256(parsed["sources_agreeing"])
        sources_total = u256(parsed["sources_total"])
        notes = parsed["notes"]

        next_epoch = u256(int(fact.verification_count) + 1)

        fact.history.append(
            VerificationEntry(
                epoch=next_epoch,
                value=value,
                sources_agreeing=sources_agreeing,
                sources_total=sources_total,
                notes=notes,
            )
        )
        fact.verification_count = next_epoch
        fact.last_value = value

    # -------------------------------------------------------------
    # Read methods
    # -------------------------------------------------------------

    @gl.public.view
    def get_current_value(self, fact_id: str) -> dict:
        f = self.facts.get(fact_id, None)
        if f is None:
            raise Exception("unknown fact_id")
        return {
            "value": f.last_value,
            "verification_count": int(f.verification_count),
        }

    @gl.public.view
    def get_history(self, fact_id: str) -> list:
        f = self.facts.get(fact_id, None)
        if f is None:
            raise Exception("unknown fact_id")
        return [
            {
                "epoch": int(e.epoch),
                "value": e.value,
                "sources_agreeing": int(e.sources_agreeing),
                "sources_total": int(e.sources_total),
                "notes": e.notes,
            }
            for e in f.history
        ]

    @gl.public.view
    def get_sources(self, fact_id: str) -> list:
        f = self.facts.get(fact_id, None)
        if f is None:
            raise Exception("unknown fact_id")
        return list(f.source_urls)

    @gl.public.view
    def list_fact_ids(self) -> list:
        return list(self.facts.keys())
