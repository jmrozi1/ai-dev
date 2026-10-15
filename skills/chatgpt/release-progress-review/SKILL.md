---
name: release-progress-review
description: Give recurring executive progress reports across multiple releases or standalone efforts, with accepted major/minor checkpoint percentages, changes since the last requested report, expandable progress bars, and deeper estimates for long-running minors.
---

# Release Progress Review

Use this ChatGPT skill when the user asks for a recurring, high-level progress report across releases, projects, or other tracked development efforts. Lead with how far each effort has advanced, what changed since the previous requested report, and which efforts are converging or slowing. This is a portfolio view, not a ticket-by-ticket Claude supervision or instruction-writing session.

Do not activate for a focused single-ticket progress/intervention review; `ticket-progress-review` owns that. A portfolio report may cite a particular ticket without triggering its full investigation. Use another skill only when a materially distinct task is actually requested. Do not create or change GitHub records, orchestrator state, or deployments as a side effect of reporting.

## Establish Comparable Progress

- Date the observation with a verified clock and time zone (America/New_York by default). Ground the current state in the smallest fresh authoritative evidence available: release roadmaps, owner-ticket checkboxes, accepted external handbacks, acceptance comments, and current named work. Distinguish source evidence from inferred progress.
- **Comparison window:** for each tracked effort, compare with the most recent **user-requested release progress report covering that effort**, not an arbitrary old checkpoint, activity log, or unrelated ticket-status review. Retrieve earlier conversation/report context when needed. If the previous request or its accepted-count snapshot cannot be established, show a baseline rather than a fabricated delta or stagnation streak. Newly discovered old achievements are baseline corrections, not new progress.
- Preserve separately the count of accepted **major outcomes**, accepted **minor checkpoints**, and the current roadmap denominator. Use accepted minors / planned minors as the default overall percentage when an accepted minor roadmap exists. If only major outcomes exist, report majors / majors clearly labeled; never silently mix units. A counted item has one owning roadmap position even when several tickets contribute. Exclude preliminary or prerequisite work from a release numerator unless its roadmap explicitly owns it.
- Treat implementation, reviewed handback, merged source, live deployment, formal checkpoint acceptance, and integrated release acceptance as distinct. An externally accepted minor may count once even when its parent checkbox is administratively stale, but disclose outstanding integration and any unresolved acceptance disagreement. Never convert an unverified claim or an unchecked provisional handoff into accepted credit merely to make the chart look accurate.
- Recount changed or superseded roadmaps; record changes in both numerator and denominator. Distinguish a higher completion percentage caused by fewer planned minors from genuine new acceptance. If the definition of a checkpoint changed materially, explain the comparison boundary or mark the delta incomparable. Don't double-count overlapping maintenance tickets or parent/child checkboxes.
- Display accepted completion to one decimal place with counts (`58/252 · 23.0%`). A percentage is a checkpoint-count measure, **not** a claim about elapsed time, effort remaining, or release readiness. Don't assign artificial weighting by hours unless the user explicitly requests it.

## Compare Movement and Health

For each effort establish **three separate observations**:

1. **Accepted checkpoint delta:** current accepted count minus the comparable count at the previous requested report. Only newly accepted work in that interval earns a green segment. A correction discovered now to earlier acceptance, or denominator shrinkage, does not.
2. **Meaningful forward progress:** whether concrete obligations were retired during that interval even if no checkbox changed: implemented/reviewed requirements, a resolved defect, valid new end-to-end proof, promoted or installed work, accepted decisions, or demonstrably narrower remaining uncertainty. Mere commands, elapsed time, token use, repeated tests without new evidence, or accumulating status/history are not progress. Use yes, no, or not established. **This is explanatory context, not the color-reset condition.**
3. **Current-minor dwell:** identify the actual active or blocking **named minor checkpoint** and whether that *same minor* remained unaccepted across consecutive **user-requested portfolio reports**. Track its identity and consecutive appearances separately from accepted counts and work done within it. A major, numeric Flow checkpoint, branch commit, or newly discovered defect is not a substitute for the minor's identity.

Keep a ten-shade **release-level minor-completion** progression for each effort's accepted-fill color. Track individual minor dwell separately as explanatory detail; it does not override the release color.

- **First comparable report:** shade 0, blue.
- **Reset on minor checkpoint completion:** if at least one minor is newly formally accepted since the previous requested report, reset the effort's previously accepted fill to shade 0, blue. This applies even when a different minor or parallel track remains open or blocked. Show the newly accepted portion in green.
- **No reset for activity within an unfinished minor:** implementation, commits, reviews, tests, repaired defects, deployment, accepted decisions and narrower uncertainty do not reset the color unless they establish formal completion of a counted minor checkpoint. Meaningful work is reported separately.
- **No newly completed minor:** advance one shade per subsequent requested portfolio report, capped at shade 9 (bright red), even when substantive work occurred. A changed current-minor label alone does not reset the color.
- **Parallel tracks:** a newly accepted minor on any track resets the release bar to blue. Still name any long-running or blocked sibling minor in expanded detail and preserve its dwell history; do not use its warmer dwell to override the release reset.
- **Evidence limits:** if the prior accepted snapshot is unavailable, establish a baseline rather than inventing a delta or streak. If new acceptance cannot be established, mark the comparison unknown and retain the prior supported shade without advancing it. Newly discovered old acceptance and denominator changes are baseline corrections, not new completions or color resets. An authorized material roadmap replacement may require a clearly labeled new baseline.
- Use ten discernible shades progressing blue → yellow → orange → bright red, never maroon/dark red. **Warm color warns about repeated requested reports without a newly completed minor checkpoint; it does not assert no work occurred, blame an agent, or prove a technical stall.** Count requested reports, not elapsed hours, controller passes or ticket comments.

**Concrete example:** WoW advances from 11/31 to 12/31 because M1.6 completes while M3.2 remains open: reset the old 11/31 fill to blue and show the new 1/31 in green. If M3.2 gains fixes and tests but no minor completes by the next report, advance one shade.

If a **minor checkpoint remains open through multiple requested reports**, and the work has expanded beyond its original expected slice, show a **separate, explicitly approximate percentage within that minor**. Assess accepted sub-obligations, newly discovered feature/dependency work, and plausibly remaining undiscovered work. Give a range when uncertainty is material. Always present the overall release percentage first, then the within-minor estimate (e.g. `23.0% overall → current minor ~65–75%`). Don't add the estimate to the accepted numerator, and don't break down every ordinary minor. Re-estimate when evidence changes; don't automatically increase it because time passed.

## Default Three-Tier Presentation

Render a compact, dark-background-friendly, **interactive** report in ChatGPT when supported. Prefer the platform's native text/layout/interaction components; this skill specifies behavior and appearance, not a separate app or permanent dashboard.

1. **Portfolio — visible by default:** one row per tracked effort. Name, accepted percentage, quiet full-width progress bar, numerator/denominator with units, and compact new-acceptance delta. Rows/bars are clickable to expand. Put the most useful executive observation and genuine human need close to the chart, not a log dump.
2. **Release — expanded on click:** major checkpoint groups with their own smaller bars and accepted counts, current objective and consequential dependencies. Group long lists when necessary but keep a truthful accounting of the whole denominator.
3. **Minor — expanded from a relevant major:** show a long-running minor's original objective, what was completed, newly introduced work, approximate internal percentage/range, unresolved obligations, and the reason progress is slow. Keep evidence and detailed caveats here, not in the portfolio view.

**Progress-bar treatment:**

- Match the understated earlier progress bars: **unfilled track in dark charcoal gray** against the dark page, with rounded ends; no white/light track, card backdrop, decorative border or outline.
- Previously accepted portion uses the effort's **blue-to-bright-red release-level minor-completion shade**: blue whenever a counted minor newly completes, otherwise warming only across comparable requested reports with no new minor completion. **Newly accepted portion since the previous requested report is green** and remains distinct from old progress. Preserve at least a one-pixel visible green mark for a genuine positive delta too small to draw at ordinary width. Acceptance on a parallel track resets the old fill to blue even if another minor remains open; describe that remaining dwell in expanded detail.
- Keep green within the actual current percentage, not overlaid beyond the total. Compare accepted counts against the *current* denominator when the roadmap is comparable; explain any denominator change separately. When a prior numerator exceeds current due to a legitimate rollback or reclassification, show the regression honestly rather than a negative green segment.
- The release-level shade colors **only the already-completed portion**, not the empty track. Use subdued plain text rather than a fake bar color when zero checkpoints have been accepted. Don't add a preview slider, color-key exercise, heat-border, or other tuning control to routine reports.
- An expanded row and its nested detail should be collapsible again. If click-to-expand cannot be provided in the current client, fall back to a concise static hierarchy and say that expand/collapse is unavailable; don't imply inert text is interactive.

## Executive Narrative

After or next to the portfolio, give a short synthesis: strongest accepted movement, meaningful work still in flight, the one or two significant risks, and **human action now** (or **none**). Note useful future preparation only when a specific known trigger warrants it. Distinguish active execution, legitimately waiting, and unconfirmed execution; do not equate a healthy service, open ticket, or silent Claude session with forward movement.

Stay at the executive level. Avoid per-rail transcript retellings, repeated validation inventories, tickets to create, and copy-paste instructions for Claude unless the user explicitly asks for tactical help. If a suspicious stall needs investigation, identify the likely owner or existing remediation briefly and keep that investigation within the owning ticket workflow.

## ChatGPT Interaction and Boundary Checks

Begin a substantive invocation with **Skill: release-progress-review**, or announce a responsibility-ordered skill chain when another skill genuinely owns distinct requested work. Treat each requested portfolio report as a new comparison observation.

Before finalizing, check these representative failure cases against the reported data:

- **Same named minor across three requested reports, with real fixes and proof but no newly completed minor in either interval:** shade 0 → 1 → 2, **not blue reset**; report meaningful progress separately.
- **One or more newly accepted minors:** reset the release's old accepted fill to blue and show a visible green delta; retire completed minors' dwell anchors.
- **Parallel track accepts a minor but another track remains blocked on the same minor:** reset the release's old fill to blue, show green new acceptance, and identify the blocked track separately. Its dwell does not override the reset.
- **Repeated state rewrites with no minor completion:** the release warms once per requested report, not once per controller pass or retry; absence of substantive progress is an additional concern, not the reset criterion.
- **Denominator reduced without new acceptance:** percentage may increase, but no green or completion-based color reset; state the plan change.
- **Prior report missing, ambiguous current-minor identity, or unavailable live evidence:** disclose unknowns; don't invent previous exposures, acceptance or a shade increase.

These checks are reasoning-level dogfood, not a required additional runtime or test framework.
