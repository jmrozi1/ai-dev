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

Keep a **per-active-minor** ten-shade *checkpoint-dwell* progression, and use it for the effort's accepted-fill color:

- **First report observing a particular current minor:** shade 0, blue. **Each subsequent requested portfolio report with that same named minor still current and unaccepted:** advance one shade, capped at shade 9 (bright red), **regardless of meaningful progress, tests, repaired defects, or how much of the minor's internal work has advanced**. Moving inches within the same mile is progress, but does not reset the pace warning.
- **Reset only when the tracked minor is formally accepted and the effort advances beyond it** (or an authorized scope/roadmap change truly replaces or retires it). A new branch revision, successful verification, changing the wording of the same obligation, or accepting a different minor **does not reset** the persistent minor's shade.
- **Parallel tracks:** track dwell separately for each active or blocking minor. The portfolio bar uses the warmest shade among the currently active/blocking minors, and names the responsible minor/track in expanded detail. Acceptance on one track adds green for its counted minor but must not conceal another track's long-stuck minor. Do not warm a minor that is merely future planned work, with no active or held obligation.
- **Evidence limits:** if the prior report did not establish the minor identity, use the earliest reliable baseline rather than inventing previous repeats. If current minor identity or continuing status is genuinely unknown, mark dwell unknown and keep the prior supported shade without advancing it. Consecutive means **requested reports**, not elapsed hours, controller passes, or ticket comments.
- Use ten discernible shades progressing blue → yellow → orange → bright red, never maroon/dark red. **Warm color warns about repeated reports without crossing that minor's acceptance boundary; it does not assert no work occurred, blame an agent, or prove a technical stall.** Explain substantive work and legitimate waits separately, even when the bar is orange or red.

If a **minor checkpoint remains open through multiple requested reports**, and the work has expanded beyond its original expected slice, show a **separate, explicitly approximate percentage within that minor**. Assess accepted sub-obligations, newly discovered feature/dependency work, and plausibly remaining undiscovered work. Give a range when uncertainty is material. Always present the overall release percentage first, then the within-minor estimate (e.g. `23.0% overall → current minor ~65–75%`). Don't add the estimate to the accepted numerator, and don't break down every ordinary minor. Re-estimate when evidence changes; don't automatically increase it because time passed.

## Default Three-Tier Presentation

Render a compact, dark-background-friendly, **interactive** report in ChatGPT when supported. Prefer the platform's native text/layout/interaction components; this skill specifies behavior and appearance, not a separate app or permanent dashboard.

1. **Portfolio — visible by default:** one row per tracked effort. Name, accepted percentage, quiet full-width progress bar, numerator/denominator with units, and compact new-acceptance delta. Rows/bars are clickable to expand. Put the most useful executive observation and genuine human need close to the chart, not a log dump.
2. **Release — expanded on click:** major checkpoint groups with their own smaller bars and accepted counts, current objective and consequential dependencies. Group long lists when necessary but keep a truthful accounting of the whole denominator.
3. **Minor — expanded from a relevant major:** show a long-running minor's original objective, what was completed, newly introduced work, approximate internal percentage/range, unresolved obligations, and the reason progress is slow. Keep evidence and detailed caveats here, not in the portfolio view.

**Progress-bar treatment:**

- Match the understated earlier progress bars: **unfilled track in dark charcoal gray** against the dark page, with rounded ends; no white/light track, card backdrop, decorative border or outline.
- Previously accepted portion uses the current **blue-to-bright-red current-minor dwell shade**, not a color reset by meaningful work within the minor. **Newly accepted portion since the previous requested report is green** and remains distinct from old progress. Preserve at least a one-pixel visible green mark for a genuine positive delta too small to draw at ordinary width. If a different parallel minor was accepted while an older one remains open, show both green new credit **and** the continuing warm shade for the stuck minor.
- Keep green within the actual current percentage, not overlaid beyond the total. Compare accepted counts against the *current* denominator when the roadmap is comparable; explain any denominator change separately. When a prior numerator exceeds current due to a legitimate rollback or reclassification, show the regression honestly rather than a negative green segment.
- The dwell shade colors **only the already-completed portion**, not the empty track. Use subdued plain text rather than a fake bar color when zero checkpoints have been accepted. Don't add a preview slider, color-key exercise, heat-border, or other tuning control to routine reports.
- An expanded row and its nested detail should be collapsible again. If click-to-expand cannot be provided in the current client, fall back to a concise static hierarchy and say that expand/collapse is unavailable; don't imply inert text is interactive.

## Executive Narrative

After or next to the portfolio, give a short synthesis: strongest accepted movement, meaningful work still in flight, the one or two significant risks, and **human action now** (or **none**). Note useful future preparation only when a specific known trigger warrants it. Distinguish active execution, legitimately waiting, and unconfirmed execution; do not equate a healthy service, open ticket, or silent Claude session with forward movement.

Stay at the executive level. Avoid per-rail transcript retellings, repeated validation inventories, tickets to create, and copy-paste instructions for Claude unless the user explicitly asks for tactical help. If a suspicious stall needs investigation, identify the likely owner or existing remediation briefly and keep that investigation within the owning ticket workflow.

## ChatGPT Interaction and Boundary Checks

Begin a substantive invocation with **Skill: release-progress-review**, or announce a responsibility-ordered skill chain when another skill genuinely owns distinct requested work. Treat each requested portfolio report as a new comparison observation.

Before finalizing, check these representative failure cases against the reported data:

- **Same named minor across three requested reports, with real fixes and proof in every interval:** shade 0 → 1 → 2, **not blue reset**; report meaningful progress separately and keep the minor unaccepted.
- **Two newly accepted minors, with tracked minors completed:** old accepted fill plus a visible green delta; reset those accepted minors' dwell anchors as the effort advances.
- **Parallel track accepts a minor but another track remains blocked on the same minor:** show green new acceptance while advancing the old minor's warm shade and identifying that track.
- **Repeated state rewrites with no retired obligations:** the unchanged current minor warms once per requested report, not once per controller pass or retry; absence of substantive progress is an additional concern, not the criterion for warmth.
- **Denominator reduced without new acceptance:** percentage may increase, but no green; retain dwell for substantively unchanged unfinished minors and state the plan change.
- **Prior report missing, ambiguous current-minor identity, or unavailable live evidence:** disclose unknowns; don't invent previous exposures, acceptance or a shade increase.

These checks are reasoning-level dogfood, not a required additional runtime or test framework.
