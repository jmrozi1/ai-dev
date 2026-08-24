from __future__ import annotations

from pathlib import Path
import unittest


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


class ControlPlaneSkillContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.skills_root = self.repo_root / "skills"
        self.orchestrator = _normalized(self.skills_root / "chatgpt" / "orchestrator" / "SKILL.md")
        self.executor = _normalized(self.skills_root / "copilot" / "executor" / "SKILL.md")

    # Orchestrator: read fresh, reconcile, accept

    def test_orchestrator_proceed_reads_fresh_durable_state(self) -> None:
        self.assertIn("`proceed` and `continue` mean read fresh durable state before acting", self.orchestrator)
        self.assertIn("do not answer them from conversational memory", self.orchestrator)
        self.assertIn("do not assume the state you last saw is still current", self.orchestrator)

    def test_orchestrator_reconciles_four_inputs_with_provenance(self) -> None:
        self.assertIn("your own accepted state, including the rail index and the next decision", self.orchestrator)
        self.assertIn("the current executor handoff for each active rail", self.orchestrator)
        self.assertIn("any bounded provider-native evidence attached to a rail", self.orchestrator)
        self.assertIn("the provenance and source health of that evidence", self.orchestrator)

    def test_orchestrator_treats_executor_claims_as_proposed_until_accepted(self) -> None:
        self.assertIn("proposed evidence, not accepted fact, until you accept it", self.orchestrator)

    def test_orchestrator_keeps_provider_evidence_as_an_independent_channel(self) -> None:
        self.assertIn("independent observational channel", self.orchestrator)
        self.assertIn("does not automatically outrank the executor's account", self.orchestrator)
        self.assertIn("keep unavailable or partial evidence visibly unavailable or partial", self.orchestrator)

    def test_orchestrator_writes_only_what_it_owns(self) -> None:
        self.assertIn("write only what you own: accepted state and rail authorization", self.orchestrator)
        self.assertIn("never rewrite an executor's handoff or its evidence", self.orchestrator)

    def test_orchestrator_publication_is_fresh_conditional_and_fail_closed(self) -> None:
        self.assertIn("freshly resolved provider-native git state", self.orchestrator)
        self.assertIn("conditional writes keyed to the head you actually read", self.orchestrator)
        self.assertIn("must fail closed", self.orchestrator)
        self.assertIn("re-read, reconcile, and republish rather than forcing", self.orchestrator)

    # Orchestrator: executive summary and decision boundary

    def test_orchestrator_executive_summary_covers_required_content(self) -> None:
        for element in (
            "material progress",
            "the current checkpoint or goal",
            "important changes in knowledge or risk",
            "blockers",
            "currently authorized or ready work",
            "process signals",
            "any genuine human decision",
        ):
            with self.subTest(element=element):
                self.assertIn(element, self.orchestrator)

    def test_orchestrator_executive_summary_is_non_canonical_and_not_a_relay(self) -> None:
        self.assertIn("never canonical state and never a substitute for publishing", self.orchestrator)
        self.assertIn("do not relay full executor output through it", self.orchestrator)
        self.assertIn("do not ask the human to carry agent responses between chats", self.orchestrator)

    def test_orchestrator_asks_only_for_genuine_decisions(self) -> None:
        self.assertIn(
            "genuine product, scope, architecture, permission, evidence-strategy, safety, or concurrency decision",
            self.orchestrator,
        )
        self.assertIn("anything decidable from durable state and accepted evidence, decide", self.orchestrator)

    # Fallback

    def test_orchestrator_preserves_repository_local_rail_when_unconfigured(self) -> None:
        self.assertIn("most work has no control plane configured, and none is required", self.orchestrator)
        self.assertIn("keep using the repository-local `.ai-dev/tasking.md` rail", self.orchestrator)
        self.assertIn("do not stand up external coordination infrastructure for small or single-session work", self.orchestrator)

    def test_executor_preserves_repository_local_rail_when_unconfigured(self) -> None:
        self.assertIn("when no control plane is configured, `.ai-dev/tasking.md` remains the rail", self.executor)

    # Audience mechanics

    def test_orchestrator_uses_provider_native_reads_not_the_local_helper(self) -> None:
        self.assertIn("chatgpt performs these reads and conditional writes through its github integration", self.orchestrator)
        self.assertIn("is not something chatgpt invokes", self.orchestrator)
        self.assertNotIn("python -m ai_dev_flow.control_plane", self.orchestrator)

    def test_executor_uses_the_deterministic_local_helper(self) -> None:
        self.assertIn("python -m ai_dev_flow.control_plane", self.executor)
        self.assertIn("fails closed on stale or diverged publication", self.executor)
        self.assertIn("do not force, and do not route around the helper", self.executor)

    def test_both_audiences_share_one_contract(self) -> None:
        self.assertIn("identical for both audiences", self.orchestrator)

    # Executor: rail, cold start, ownership

    def test_executor_proceed_reads_the_fresh_authorized_rail(self) -> None:
        self.assertIn("`proceed` and `continue` mean read the fresh authorized rail before acting", self.executor)

    def test_executor_cold_starts_without_a_transcript(self) -> None:
        self.assertIn("without the previous chat transcript", self.executor)
        self.assertIn("`.ai-dev/tasking.md` or the configured authorized rail", self.executor)

    def test_executor_reads_only_its_own_rail(self) -> None:
        self.assertIn("do not read sibling rails you were not authorized for", self.executor)

    def test_executor_publishes_only_owned_surfaces(self) -> None:
        self.assertIn("publish only what you own", self.executor)
        self.assertIn("observations, exact evidence, unknowns, proposed facts, failures, and recommended next work", self.executor)

    def test_executor_cannot_promote_its_own_proposals(self) -> None:
        self.assertIn("you may not promote your own proposal into accepted state", self.executor)
        self.assertIn("may not materially rewrite your own authorization", self.executor)
        self.assertIn("propose the change and let the orchestrator decide", self.executor)

    def test_executor_handoff_is_bounded_current_state(self) -> None:
        self.assertIn("a published handoff is bounded current state", self.executor)
        self.assertIn("never append to it", self.executor)
        self.assertIn("never let it become a transcript, message log, or execution diary", self.executor)

    def test_executor_continues_independent_work_after_a_failure(self) -> None:
        self.assertIn("failure of one task does not stop an authorized rail", self.executor)
        self.assertIn("block only what actually depends on it", self.executor)

    # Process review

    def test_executor_process_observations_are_classifiable_for_review(self) -> None:
        for category in (
            "a communication failure durable state should have prevented",
            "durable information that was stale or contradictory",
            "a legitimate human decision",
            "a tooling or deterministic-helper failure",
            "a permission or provider limitation",
            "an isolated executor mistake",
            "a possible skill-guidance deficiency",
        ):
            with self.subTest(category=category):
                self.assertIn(category, self.executor)
        self.assertIn("do not decide or state that a skill is defective", self.executor)

    # Parallel rails

    def test_orchestrator_tracks_four_rail_status_classes(self) -> None:
        self.assertIn("`ready`, `running`, `blocked`, or `completed`", self.orchestrator)
        self.assertIn(
            "record only the dependencies and shared-resource constraints that materially affect the current recommendation",
            self.orchestrator,
        )

    def test_orchestrator_recommends_continue_launch_or_hold(self) -> None:
        self.assertIn("continue an existing executor", self.orchestrator)
        self.assertIn("launch a fresh executor", self.orchestrator)
        self.assertIn("hold or block the rail, with a concise reason", self.orchestrator)

    def test_orchestrator_leaves_dispatch_to_the_human(self) -> None:
        self.assertIn("the human is the dispatcher", self.orchestrator)
        self.assertIn("never spawn, poll, or manage agents", self.orchestrator)

    def test_orchestrator_optimizes_attention_not_agent_count(self) -> None:
        self.assertIn("optimize useful progress and human attention rather than agent count", self.orchestrator)
        self.assertIn("holding a runnable rail is often right", self.orchestrator)

    def test_orchestrator_serializes_only_singleton_resource_rails(self) -> None:
        self.assertIn("a known singleton resource serializes the rails that need it", self.orchestrator)
        self.assertIn("unrelated source-only work stays launchable", self.orchestrator)

    def test_orchestrator_keeps_judgment_out_of_the_helper(self) -> None:
        self.assertIn("those are facts", self.orchestrator)
        self.assertIn("deciding what to launch, continue, or hold is your judgment", self.orchestrator)
        self.assertIn("do not build a dependency graph, queue, or schedule", self.orchestrator)

    def test_executor_identity_is_disposable_and_rail_scoped(self) -> None:
        self.assertIn("executor identity is disposable; the rail is durable", self.executor)
        self.assertIn("do not assume you authored it", self.executor)
        self.assertIn("work only the rail you were given, even when other rails are running", self.executor)

    # Negative boundaries

    def test_no_separate_shared_control_plane_skill_was_created(self) -> None:
        self.assertFalse((self.skills_root / "control-plane").exists())
        catalog = _normalized(self.skills_root / "index.md")
        self.assertNotIn("`control-plane`", catalog)

    def test_neither_skill_introduces_forbidden_machinery(self) -> None:
        for forbidden in ("message bus", "inbox", "outbox", "polling loop", "worker queue", "heartbeat", "agent database", "worker registry"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.orchestrator)
                self.assertNotIn(forbidden, self.executor)


if __name__ == "__main__":
    unittest.main()
