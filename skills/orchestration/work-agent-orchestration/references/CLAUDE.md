## Execution workflow

Investigation and proposal are autonomous. Execution is not.

Before making any change to project files or executing any command that can modify project, repository, filesystem, or environment state:

- Investigate the task using read-only operations.
- Determine the requested outcome and relevant constraints.
- Present a concrete implementation plan to the user.
- Stop and wait for explicit user approval of that plan.

Do not interpret discussion of a plan, clarification of requirements, or approval of an objective as approval to execute. Execution begins only after the user explicitly approves the proposed implementation plan.

After approval:

- Execute only the approved plan.
- Preserve existing files and state unless their modification or removal is explicitly part of the approved plan.
- Do not add destructive or materially different steps during execution.
- If new information requires a material change to the approved plan, stop, explain the required change, propose the revised plan, and wait for approval before continuing.
- Never delete or discard existing work merely because replacement content has been created or copied elsewhere.

Commands and operations that are read-only may be used autonomously during investigation.
