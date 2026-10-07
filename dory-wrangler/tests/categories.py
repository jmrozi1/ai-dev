"""Which category every test in the suite belongs to, and why.

Every test is in exactly one of three categories, so a run on a host that is
not this one -- the internal Rocky 9 / Python 3.9 host above all -- can tell a
failure that may be environmental from one that cannot be.

**portable** -- asserts v0.1 behaviour that must hold wherever the product runs:
the contract and its validator, the store and its invariants, the chat loop
through launchers that are part of the portable surface (`scripted-stub`, and
launchers a test defines in-process), classification, rendering, the notice,
the served boundary over HTTP, and diagnostics retrieval. It needs of the host
only a POSIX filesystem, the Python standard library, a loopback socket, and
Python programs started with the running interpreter (the product's own
`run_shell.py`, `diagnostics.py` and `validate_store.py`, and the suite's own
helpers). **A portable failure is a product failure on that host.**

**launch-boundary** -- asserts the launch-boundary contract (contract 6 and
`launch-boundary.md`) as a launcher must honour it and as the harness must use
it: the seam's closed packet and result shapes, the handle as the only address,
a launcher remembering nothing between calls, the declared capabilities and the
two continuation modes, the five failure categories, and the internal-shape
Codex JSONL model (`internal_bridge.py` over `model_launch_agent.py`, a Python
program started per call) hosted unchanged. Same host needs as portable. **A
launch-boundary failure is a boundary failure on that host.** What it would take
to run these against a launcher #90 registers is in the README.

**development-environment** -- the outcome depends on something this
development host supplies and another host could legitimately differ on: the
`dev-local` launcher (its agent program, its child processes and pipes, its
output framing, the locale its pipe encodes the instruction in -- L4); POSIX
process mechanics asserted as facts (a process alive or gone, a process tree
read from `/proc` as evidence that nothing runs, a signal delivered to a process
group or at a chosen moment); `flock` or exclusive creation
settling a race between separate store objects or processes; file permission
bits (which root ignores); wall-clock deadlines, sleeps and host load; or the
interpreter version. **A development-environment failure may be the host, not
the product**: find which of those it rests on, confirm the host differs there,
and only then call it environmental.

Three things are plumbing, not dependencies, and do not by themselves make a
test development-environment: stopping a process the test started (even by
SIGKILL, to stand for a crash) between two steps whose results are then read
only from the store or over HTTP; a generous deadline that exists only so a
regression fails instead of hanging the suite; and the store lock taken by the
one process that serves a store, uncontended, which every store open does.

The table below assigns a category to a test module, a class, or a single
test, and the most specific entry wins. `run_tests.py`'s phase 0 fails the run
when any test the suite discovers resolves to no category, or when an entry
names nothing that exists -- so a new module, or a renamed class that loses its
entry, cannot go uncategorised.
"""

PORTABLE = "portable"
LAUNCH_BOUNDARY = "launch-boundary"
DEVELOPMENT_ENVIRONMENT = "development-environment"

CATEGORIES = (PORTABLE, LAUNCH_BOUNDARY, DEVELOPMENT_ENVIRONMENT)

P, LB, DEV = PORTABLE, LAUNCH_BOUNDARY, DEVELOPMENT_ENVIRONMENT

ASSIGNMENTS = {
    # -- modules: the category of every test the entries below do not place --
    "test_addressing": LB,
    "test_adversarial": P,
    "test_atomicity": P,
    "test_binding": P,
    "test_categories": P,
    "test_carried_gates": P,
    "test_chat_loop": P,
    "test_classification": P,
    "test_convergence": P,
    "test_diagnostic_access": P,
    "test_end_to_end": DEV,
    "test_failure_classification": LB,
    "test_intake": P,
    "test_internal_bridge": LB,
    "test_launch_adversarial": LB,
    "test_mode_invariance": LB,
    "test_rendering": P,
    "test_restart": P,
    "test_seam": LB,
    "test_shell": P,
    "test_store": P,
    "test_swappability": LB,
    "test_tools": P,
    "test_turn_floor_regression": P,
    "test_unsupported_and_malformed": P,

    # -- launch-boundary entries more specific than a module --
    # Contract 6.1 / 4.3: a launcher remembers nothing between calls.
    "test_adversarial.TestTheLaunchSeamCarriesTheHandle": LB,
    # Contract 6.1 consequence 3: no harness behaviour rests on launcher state.
    "test_intake.TheHarnessRestsOnNoLauncherDurableState": LB,
    # The one test of that class that uses no dev-local configuration (measured:
    # it passes with dev-local unavailable); it drives only scripted-stub.
    "test_mode_invariance.TheExperimentIsWhatItIsAndNotMore.test_response_shape_is_exercised_where_it_actually_matters": LB,

    # -- development-environment: the `dev-local` launcher --
    # Each of these fails when `dev-local` is unavailable (measured: its
    # registry entry removed and its agent program replaced by one that exits).
    "test_binding.SequentialRebindingIsPermitted.test_a_chat_binds_many_agents_one_after_another": DEV,
    "test_carried_gates.ALineOfAnyLength.test_an_answer_over_a_mebibyte_is_preserved_and_rendered_whole": DEV,
    "test_carried_gates.ExactTextTheGateReads.test_the_development_transport": DEV,
    "test_chat_loop.DiagnosticsStayOutOfTheChat.test_the_same_holds_through_a_real_process": DEV,
    "test_chat_loop.DurableRecordsAreCanonical.test_a_chat_reopens_after_a_restart_with_no_live_anything": DEV,
    "test_chat_loop.DurableRecordsAreCanonical.test_a_live_session_survives_a_restart_and_the_user_is_never_stuck": DEV,
    "test_chat_loop.FullLoopAgainstEveryLauncher.test_every_dev_local_configuration_records_the_capabilities_it_declared": DEV,
    "test_chat_loop.FullLoopAgainstEveryLauncher.test_three_turns_under_every_dev_local_configuration": DEV,
    "test_classification.ClassificationIsAFunctionOfThePreservedBytes.test_dev_local_in_both_profiles": DEV,
    # Includes L4: the instruction written in the pipe's locale encoding.
    "test_classification.DevLocalFramesTheWireBytesTheSameInBothProfiles": DEV,
    "test_classification.UnrecognizedAndMalformedAreNeverChat.test_at_the_store_through_dev_local": DEV,
    "test_classification.UnrecognizedAndMalformedAreNeverChat.test_over_http_with_a_shipped_launcher_chosen_by_configuration": DEV,
    "test_diagnostic_access.TheMinimumFromTheToolAlone.test_the_development_transport_both_profiles": DEV,
    "test_failure_classification.AgentFailureIsNotLaunchFailure.test_a_real_process_exiting_non_zero_is_failed": DEV,
    "test_failure_classification.EveryFailureCategoryIsDurablyRecorded.test_a_process_that_cannot_start_is_unavailable": DEV,
    "test_failure_classification.EveryFailureCategoryIsDurablyRecorded.test_a_real_process_that_says_nothing_is_no_acknowledgement": DEV,
    "test_failure_classification.UnknownIsAStateNotAFailure.test_a_real_process_whose_stream_ends_while_it_lives": DEV,
    "test_launch_adversarial.AOneShotLauncherCannotObserveAStreamEnding.test_probe_a_real_one_shot_agent_cannot_smuggle_a_stream_end": DEV,
    "test_launch_adversarial.NoPayloadBoundIsAsserted.test_every_launcher_in_this_package_declares_no_measured_bound": DEV,
    "test_mode_invariance.TheExperimentIsWhatItIsAndNotMore": DEV,
    "test_mode_invariance.TheTranscriptIsIdenticalUnderEveryCombination.test_every_dev_local_configuration_produces_the_same_three_turn_transcript": DEV,
    "test_mode_invariance.TheTranscriptIsIdenticalUnderEveryCombination.test_reopen_behaviour_is_identical_under_every_dev_local_combination": DEV,
    "test_mode_invariance.TheTranscriptIsIdenticalUnderEveryCombination.test_the_chat_and_message_records_are_the_same_shape": DEV,
    "test_rendering.TheFourAcceptanceCases": DEV,
    "test_rendering.ZeroOrSeveralTextEventsPerTurn.test_an_event_with_empty_text_is_preserved_and_renders_nothing": DEV,
    "test_rendering.ZeroOrSeveralTextEventsPerTurn.test_a_turn_with_no_text_in_flight_still_refuses_the_next_in_process": DEV,
    "test_rendering.ZeroOrSeveralTextEventsPerTurn.test_the_development_transport_in_process": DEV,
    "test_rendering.ZeroOrSeveralTextEventsPerTurn.test_the_development_transport_over_http_through_run_shell": DEV,
    "test_swappability.SelectionIsConfigurationOnly.test_the_registry_builds_each_configured_launcher": DEV,
    "test_unsupported_and_malformed.TheConversationSurvivesWhatCannotBeShown.test_the_development_transport_in_process": DEV,
    "test_unsupported_and_malformed.TheConversationSurvivesWhatCannotBeShown.test_the_development_transport_over_http_through_run_shell": DEV,
    "test_unsupported_and_malformed.TheNoticeIsFixedHarnessWording.test_it_is_served_and_shown_as_a_system_message": DEV,
    "test_unsupported_and_malformed.TheNoticeIsFixedHarnessWording.test_nothing_from_the_integration_reaches_it": DEV,
    "test_unsupported_and_malformed.WhenTheNoticeIsNotWritten.test_not_for_a_turn_that_never_ends_even_across_a_restart": DEV,
    "test_unsupported_and_malformed.WhenTheNoticeIsNotWritten.test_not_for_a_turn_with_an_answer_and_unrenderable_events": DEV,

    # -- development-environment: flock, or exclusive creation, settling a race
    # between separate store objects or processes (decisions/0001 risk R3: over
    # NFS neither is safe in general, and a POSIX-lock emulation of flock does
    # not conflict between descriptors of one process) --
    "test_adversarial.TestOneAgentPerChat.test_e3_six_writers_opening_a_session_at_once": DEV,
    "test_adversarial.TestOneAgentPerChat.test_e3b_a_second_process_cannot_open_a_session_on_a_served_store": DEV,
    "test_atomicity.TestConcurrentSenders.test_a_second_writing_process_is_refused_and_writes_nothing": DEV,
    "test_atomicity.TestConcurrentSenders.test_threads_racing_for_a_sequence_lose_nothing": DEV,
    "test_convergence.OneServingProcessPerStore": DEV,
    # Both also race a second serving process for the store; one also uses dev-local.
    "test_convergence.ASecondShellAgainstALiveOneChangesNothing": DEV,
    "test_convergence.ATurnInFlightRefusesEveryOtherUserAction.test_every_action_in_every_non_terminal_state_is_refused_while_held": DEV,
    "test_convergence.ATurnInFlightRefusesEveryOtherUserAction.test_a_chat_with_no_session_refuses_a_send_while_held": DEV,
    "test_convergence.TheTurnLockGivesBackWhatItTook.test_a_hold_refused_because_another_descriptor_holds_it_takes_nothing": DEV,
    "test_convergence.TheTurnLockGivesBackWhatItTook.test_a_re_entrant_exit_keeps_the_file_lock_for_the_outer_hold": DEV,
    "test_convergence.TheStoreLockHasNoGaps.test_a_read_only_store_writes_nothing_and_takes_no_lock": DEV,
    "test_convergence.TheStoreLockHasNoGaps.test_a_refused_process_creates_no_turn_lock_file": DEV,
    "test_convergence.TheStoreLockHasNoGaps.test_a_forked_child_neither_joins_nor_gives_back_its_parents_hold": DEV,
    "test_convergence.TheStoreLockHasNoGaps.test_a_refused_acquisition_leaks_no_descriptor": DEV,
    "test_convergence.TheStoreLockHasNoGaps.test_the_lock_is_released_only_by_the_last_holder_in_the_process": DEV,
    "test_convergence.TheStoreLockHasNoGaps.test_closing_the_served_application_gives_the_store_back": DEV,
    "test_convergence.TheStoreLockHasNoGaps.test_a_shell_refused_its_port_re_attaches_nothing_and_holds_nothing": DEV,
    "test_convergence.TheStoreLockHasNoGaps.test_a_shell_that_fails_to_start_gives_back_what_it_took_even_while_its_error_lives": DEV,
    # Its child decides "held" by taking `flock` on a second descriptor of the
    # same process (measured: under a `lockf` emulation of flock it reads "b" as
    # free, and it is the only portable or launch-boundary test that fails so).
    "test_convergence.TheStoreLockHasNoGaps.test_a_store_collected_while_the_guard_is_held_does_not_deadlock": DEV,
    "test_store.TestGuardedCodes.test_duplicate_sequence": DEV,
    "test_store.TestTheDeliveryAcknowledgementIsWrittenOnce.test_two_concurrent_answers_to_one_delivery_record_exactly_one": DEV,

    # -- development-environment: process mechanics asserted as facts, or a
    # signal delivered at a moment --
    "test_convergence.EveryNonTerminalStateHasTheOneActionAsItsExit.test_a_holder_killed_after_start_up_is_no_longer_a_stranding": DEV,
    "test_convergence.EveryNonTerminalStateHasTheOneActionAsItsExit.test_a_turn_that_never_returns_is_exited_by_process_exit_restart_and_the_action": DEV,
    "test_restart.TestRestartRecovery.test_a_chat_reopens_with_its_complete_user_visible_history": DEV,
    "test_restart.TestRestartRecovery.test_the_history_survives_the_store_being_moved_between_restarts": DEV,
    "test_restart.TestRestartWithNoShellAtAll.test_history_is_readable_with_nothing_running": DEV,
    "test_shell.TestShellBoundaries.test_the_shell_starts_no_process": DEV,

    # -- development-environment: the locale's encoding --
    # A command-line argument a Latin-1 locale cannot encode (measured: under
    # LC_ALL=en_US.iso88591 `subprocess` refuses it before the tool runs).
    "test_diagnostic_access.TheCommandLineRetrieval.test_a_non_ascii_digit_int_reads_is_refused_as_a_limit": DEV,

    # -- development-environment: file permission bits --
    "test_adversarial.TestReopenFidelity.test_d2_the_transcript_does_not_read_the_diagnostics_tree": DEV,
    "test_diagnostic_access.TheCommandLineRetrieval.test_an_unreadable_directory_is_refused_without_its_reason": DEV,

    # -- test_end_to_end is development-environment (dev-local, process groups,
    # deadlines) but for its fault matrix's static self-check --
    "test_end_to_end.EachStepFailsAtItsStep.test_every_step_has_a_fault_and_every_fault_a_test": P,
}


def test_key(test):
    """The dotted name a test is assigned by: `module.Class.test_method`.

    An error in a class or module fixture arrives as an `_ErrorHolder` whose
    description names the class or module; its key is that name, so it resolves
    to the category of the tests it stopped.
    """
    description = getattr(test, "description", None)
    if description is not None and type(test).__name__ == "_ErrorHolder":
        if "(" in description and description.endswith(")"):
            return description[description.index("(") + 1:-1]
        return description
    return test.id()


def category_of(key):
    """The category of a test key, or None when nothing in the table places it."""
    parts = key.split(".")
    for end in range(len(parts), 0, -1):
        found = ASSIGNMENTS.get(".".join(parts[:end]))
        if found is not None:
            return found
    return None


def tests_of(suite):
    """Every test case in a suite, flattened, in the loader's order."""
    for item in suite:
        if hasattr(item, "__iter__"):
            for test in tests_of(item):
                yield test
        else:
            yield item


def check(suite):
    """{"counts": {category: n}, "problems": [text]} for the whole suite.

    A problem is a test that could not be loaded, a test no entry places, an
    entry whose value is not a category, or an entry that names no test.
    """
    counts = dict((name, 0) for name in CATEGORIES)
    problems = []
    used = set()
    for test in tests_of(suite):
        key = test_key(test)
        if type(test).__name__ == "_FailedTest":
            problems.append("could not load: %s" % key)
            continue
        found = category_of(key)
        if found is None:
            problems.append("unassigned: %s" % key)
            continue
        if found not in CATEGORIES:
            problems.append("not a category: %s -> %r" % (key, found))
            continue
        counts[found] += 1
        parts = key.split(".")
        used.update(".".join(parts[:end]) for end in range(1, len(parts) + 1))
    for entry in sorted(ASSIGNMENTS):
        if entry not in used:
            problems.append("names no test: %s" % entry)
    return {"counts": counts, "problems": problems}


def select(suite, wanted):
    """A flat suite of only the tests whose category is in `wanted`, in order."""
    import unittest
    return unittest.TestSuite(
        test for test in tests_of(suite) if category_of(test_key(test)) in wanted)
