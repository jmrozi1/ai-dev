# Work AI Lessons

This file records generalized, non-sensitive conclusions established by
dogfooding the current work AI. It is not a copy of the work environment or its
configuration.

## Established

### Treat the work AI as a low-reasoning agent

The current work AI has demonstrated very poor reasoning reliability. Do not
design workflows that depend on it making a long sequence of good judgments,
inferring missing operational steps, or producing trustworthy original design.

Optimize for a reliable capability floor rather than autonomy.

### Reading is stronger than authorship

The work AI has demonstrated that it can read and consume substantial amounts of
instructions and documentation.

Do not infer from that success that it can author durable material to the same
standard. Reading and interpreting authoritative information is currently a
trusted use; creating maintainable source material is not.

### Prefer scripts over execution flows

When operational mechanics can reasonably be encoded in a deterministic script,
prefer the script over instructions that require the work AI to reconstruct and
execute a multi-step flow.

The preferred pattern is for the work AI to invoke a proven skill or helper,
interpret the result, and explain what happened. Keep deterministic mechanics in
the script rather than in model judgment.

### Bounded execution is acceptable

Running bounded commands for inspection, testing, experimentation, or
verification is acceptable when the result is reviewable and the command is not
being treated as the foundation of a reusable implementation.

Small support scripts are also acceptable when they can be independently
inspected and verified before being trusted.

### Do not build on work-AI-authored artifacts

Do not delegate artifacts that need to become a trustworthy foundation for later
work. This includes:

- durable or authoritative documentation;
- reusable skills;
- production or scalable code;
- scripts larger than roughly a couple hundred lines;
- architecture or refactoring that depends on sustained reasoning.

When one of these tasks comes up, recommend using the user, ChatGPT, or another
stronger development model instead of trying to compensate with a larger prompt.

### Prefer skill-relative paths

When a skill needs to reference files or scripts bundled with itself, prefer
`${CLAUDE_SKILL_DIR}` over hard-coded user paths or broad path wildcards.

This keeps the skill portable across users and installation locations while
keeping the execution surface narrow.

Prefer:

`${CLAUDE_SKILL_DIR}/scripts/install.sh`

over:

`/home/specific-user/.claude/skills/example/scripts/install.sh`

or:

`*/skills/example/scripts/install.sh`

## Under Evaluation

### `${CLAUDE_SKILL_DIR}` in `allowed-tools`

Verify that `${CLAUDE_SKILL_DIR}` expands as expected inside `allowed-tools`
permission patterns before treating that usage as established.

## Recording Rules

- Record generalized lessons rather than work-session transcripts.
- Do not store proprietary or sensitive work information in this public
  repository.
- Treat a new idea as under evaluation until work dogfooding provides enough
  evidence for the user to accept it as an established lesson.
- Change or remove an established lesson when later evidence shows that the
  capability boundary has materially changed.
