---
name: project-readme
description: Discuss, write, review, or revise the root project README.md for the constrained work-agent environment. Use when the task concerns the repository's primary README as the project's front door and routing page.
---

# Project README

Use this skill when discussing, writing, reviewing, or revising the root project `README.md`.

The root README is the project's front door. Its primary job is to orient the reader and route them to the right place, not to duplicate every detailed procedure in the repository.

## Reader Questions

A new engineer landing on the README should be able to determine, without reading the whole repository:

- what this project is;
- whether the repository is relevant to what they are trying to do;
- what the major supported tasks or workflows are;
- where to go next for each of those tasks.

Do not satisfy these questions with vague assertions. Give enough concrete information for the reader to make the decision themselves.

## Scope

Open with enough information for the reader to answer:

> Am I in the right repository for what I am trying to do?

Keep this concise. The README should orient before it explains details.

## Routing

Treat routing as a core README responsibility.

For each major task or supported path, point to the authoritative next document, section, command, or entry point. The reader should not need to hunt through the repository or already know its structure.

Links and referenced paths must identify real destinations.

If multiple environments or variants exist, make the distinction clear enough that the reader can select the correct path without guessing.

## Keep Detailed Procedures Out of the Front Door

Prefer routing to authoritative documentation over copying long installation, configuration, troubleshooting, or operational procedures into the README.

Include enough information to help the reader choose the correct path, then hand off to the document that owns the procedure.

Do not remove detail that is genuinely required for basic project orientation or the immediate first step.

## Review Standard

When reviewing the README, evaluate whether a new engineer can orient and route successfully.

Do not approve it merely because it contains common README sections such as overview, setup, usage, or links.

Look for failures such as:

- the project purpose is unclear;
- the reader cannot tell whether the repository applies to their goal;
- major workflows are missing or difficult to discover;
- two links or sections appear to own the same path without a clear authority;
- detailed procedures bury the routing information;
- referenced destinations are missing, misleading, or unclear.

If the README already performs its front-door role well, do not expand it merely to make it more comprehensive.

## Writing and Revision

Prefer a short, useful README over an exhaustive one.

Preserve existing information that helps orientation and routing. Move or summarize detail only when doing so makes the front door clearer and the authoritative detailed documentation remains available.

Do not impose a fixed README template unless the project requires one. Organize the page around what a new engineer needs to understand and where they need to go next.
