"""``verify_codes``: what counts as the user agreeing to a ``P#-group/task`` code.

The live path (the scheduled summarizer, the persisted handoff, the gateway's provenance marker) is
covered in ``tests/gateway/test_scheduled_compression.py``; these are the lexical contracts.
"""
from agent.context_compressor_daily_threads import established_codes, verify_codes

SUMMARY = """## Key Decisions
- ship it

## Daily Threads
- P1-ops/backups — nightly snapshots off; next: re-enable the nightly snapshot job
- P2-ops/logs — rotation unset; next: rotate the logs weekly
"""


def _standing(messages, carried=()):
    return set(established_codes(verify_codes(SUMMARY, messages, carried)))


def test_an_approval_promised_for_later_is_an_intention_not_consent():
    """"I will approve P1-ops/backups tomorrow" names the code with an approving word and refuses
    nothing, yet agrees to nothing yet. A deferred decision errs towards proposed."""
    assert _standing([{"role": "user", "content": "I will approve P1-ops/backups tomorrow."}]) == set()
    assert _standing([{"role": "user", "content": "I'll use P1-ops/backups once the audit lands."}]) == set()
    # The same user, deciding now.
    assert _standing([{"role": "user", "content": "Agreed, track P1-ops/backups."}]) == {"P1-ops/backups"}


def test_a_bare_yes_approves_the_one_code_the_turn_before_it_proposed():
    """An unequivocal reply to a single explicit proposal is consent; the user should not have to
    retype the code. Ambiguity is not: two codes on the table, a code the turn only quoted, or a
    reply that asks, refuses or defers, all leave the code proposed."""
    proposal = {"role": "assistant", "content": "Proposing P2-ops/logs for the rotation work."}
    assert _standing([proposal, {"role": "user", "content": "Yes, use that."}]) == {"P2-ops/logs"}
    assert _standing([proposal, {"role": "user", "content": "Yes"}]) == {"P2-ops/logs"}

    for reply in ("Yes, use that?", "No, don't use that.", "Yes, use that once I have reviewed it."):
        assert _standing([proposal, {"role": "user", "content": reply}]) == set(), reply
    # Two codes on the table: "that" points at nothing in particular.
    ambiguous = {"role": "assistant",
                 "content": "Proposing P2-ops/logs and P1-ops/backups for tonight."}
    assert _standing([ambiguous, {"role": "user", "content": "Yes, use that."}]) == set()
    # Naming a code is not offering it: an example, a format note, a question about one already in
    # use, and a quoted code span all put nothing on the table for "use that" to point at.
    for mention in ("The format is `P2-ops/logs`, for example.",
                    "The format is P2-ops/logs, for example.",
                    "Is P2-ops/logs already tracked?",
                    "P2-ops/logs was the old template's wording.",
                    "Rotation is unset; the logs keep filling the disk."):
        assert _standing([{"role": "assistant", "content": mention},
                          {"role": "user", "content": "Yes, use that."}]) == set(), mention
    # The proposal is answered by the turn that answers it: a later approval cannot reach back.
    assert _standing([proposal, {"role": "user", "content": "Hold on, let me check the disk."},
                      {"role": "user", "content": "Yes, use that."}]) == set()


def test_an_ambiguous_yes_to_a_different_question_does_not_approve_the_code():
    """A turn that proposes a code AND closes on an unrelated question leaves the question, not the
    offer, as the thing a reply answers: "Yes, they are." is about the backups. An approval that
    names its own subject ("Keep backups running.") agrees to that subject, not to the code. Both
    leave the code proposed, because neither is consent to it."""
    with_question = {"role": "assistant",
                     "content": "Proposing P2-ops/logs for rotation. Are the backups healthy?"}
    for reply in ("Yes, they are.", "Keep backups running.", "Yes, keep the backups running."):
        assert _standing([with_question, {"role": "user", "content": reply}]) == set(), reply
    # Without any question to answer, an approval about its own subject still agrees to nothing.
    offer = {"role": "assistant", "content": "Proposing P2-ops/logs for rotation."}
    assert _standing([offer, {"role": "user", "content": "Keep backups running."}]) == set()
    # The proposal is still approvable by an answer that points at it rather than at a subject.
    assert _standing([offer, {"role": "user", "content": "Yes, use that."}]) == {"P2-ops/logs"}


def test_a_proposal_phrased_as_a_direct_question_can_be_approved_by_reference():
    """"Shall we use P2-ops/logs?" IS the offer, so "Yes, use that." is consent to it. A question
    mark alone never disqualifies a proposal; asking ABOUT a code still puts nothing on the table."""
    asked = {"role": "assistant", "content": "Shall we use P2-ops/logs for rotation?"}
    assert _standing([asked, {"role": "user", "content": "Yes, use that."}]) == {"P2-ops/logs"}
    assert _standing([{"role": "assistant", "content": "Should we go with P2-ops/logs?"},
                      {"role": "user", "content": "Yes"}]) == {"P2-ops/logs"}
    for asking_about in ("Is P2-ops/logs already tracked?",
                         "Was P2-ops/logs the old template's wording?",
                         "Which format do you want, `P2-ops/logs` for example?"):
        assert _standing([{"role": "assistant", "content": asking_about},
                          {"role": "user", "content": "Yes, use that."}]) == set(), asking_about


def test_a_pronoun_in_front_of_the_sentences_own_subject_is_not_a_reference():
    """A possessive modifies the noun after it, so "your" is not a pointer at the offer: "Keep your
    backups running." is about the backups. The same holds for a referent carrying a complement of
    its own ("Keep it running."). A referent followed by nothing, or by a prepositional adjunct, is
    still the thing being agreed to — and an explicit "your suggestion" still refers."""
    offer = {"role": "assistant", "content": "Proposing P2-ops/logs for rotation."}
    for reply in ("Keep your backups running.", "Yes, use your usual backup settings.",
                  "Keep it running.", "Yes, keep the log volume mounted."):
        assert _standing([offer, {"role": "user", "content": reply}]) == set(), reply
    for reply in ("Yes, use that.", "Yes, please use that for the rotation work.",
                  "ok go with it", "Yes, go with your suggestion."):
        assert _standing([offer, {"role": "user", "content": reply}]) == {"P2-ops/logs"}, reply


def test_a_compaction_handoff_cannot_supply_the_proposal_a_yes_approves():
    """A stored handoff is the summarizer's own text replayed as an assistant row. Reading a code
    line out of it as "the proposal on the table" would let a handoff the summarizer wrote become
    provenance for an approval the user never gave — the exact loop ``verify_codes`` exists to break.
    Provenance travels in the gateway's marker (``carried``), never in the transcript."""
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY, SUMMARY_PREFIX
    from agent.context_compressor import is_compaction_summary_message

    body = ("## Daily Threads\n"
            "- proposed P2-ops/logs — rotation unset; next: rotate the logs weekly\n")
    flagged = {"role": "assistant", "content": body, COMPRESSED_SUMMARY_METADATA_KEY: True}
    by_content = {"role": "assistant", "content": SUMMARY_PREFIX + body}
    for handoff in (flagged, by_content):
        # Both recognition paths matter: wire sanitizers strip the metadata key.
        assert is_compaction_summary_message(handoff)
        assert _standing([handoff, {"role": "user", "content": "Yes, use that."}]) == set()
    # A real assistant turn offering the same code is still approvable, and the marker still carries.
    assert _standing([{"role": "assistant", "content": "Proposing P2-ops/logs for rotation."},
                      {"role": "user", "content": "Yes, use that."}]) == {"P2-ops/logs"}
    assert _standing([handoff], carried=["p2-ops/logs"]) == {"P2-ops/logs"}
