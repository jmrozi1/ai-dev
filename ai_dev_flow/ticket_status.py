from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys

from .repository import RepositoryError, resolve_repo_root, workflow_state_file_for_repo_root
from .ticket_providers import TicketProviderError, resolve_ticket_provider_for_reference
from .workflow_state import WorkflowStateError, load_state


class TicketStatusError(Exception):
    """Raised when the active ticket cannot supply a project-progress status."""


@dataclass(frozen=True)
class TicketCheckpoint:
    name: str
    description: str
    completed: bool


_SECTION_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_CHECKPOINT_ITEM = re.compile(
    r"^-\s+\[([ xX])\]\s+(?:\*\*(.+?)\*\*|([^:]+?))(?:\s*:\s*(.*))?\s*$"
)


def _ticket_section(body: str, name: str) -> str:
    matches = list(_SECTION_HEADING.finditer(body))
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() != name.casefold():
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[match.end() : end].strip()
    raise TicketStatusError(f"Active ticket is missing its {name} section.")


def _parse_checkpoints(body: str) -> tuple[TicketCheckpoint, ...]:
    checkpoints_text = _ticket_section(body, "Checkpoints")
    checkpoints: list[TicketCheckpoint] = []
    current_name: str | None = None
    current_description: list[str] = []
    current_completed = False

    def finish_current() -> None:
        if current_name is None:
            return
        description = " ".join(part for part in current_description if part).strip()
        if not description:
            raise TicketStatusError(f"Ticket checkpoint '{current_name}' is missing a description.")
        checkpoints.append(
            TicketCheckpoint(
                name=current_name,
                description=description,
                completed=current_completed,
            )
        )

    for line in checkpoints_text.splitlines():
        match = _CHECKPOINT_ITEM.match(line)
        if match is not None:
            finish_current()
            current_completed = match.group(1).casefold() == "x"
            current_name = (match.group(2) or match.group(3) or "").strip()
            inline_description = (match.group(4) or "").strip()
            current_description = [inline_description] if inline_description else []
            continue
        if current_name is not None and line.strip():
            current_description.append(line.strip())

    finish_current()
    if not checkpoints:
        raise TicketStatusError("Active ticket has no named checkpoints.")
    return tuple(checkpoints)


def _render_checkpoints(checkpoints: tuple[TicketCheckpoint, ...]) -> str:
    return "\n".join(
        f"- [{'x' if checkpoint.completed else ' '}] {checkpoint.name}: {checkpoint.description}"
        for checkpoint in checkpoints
    )


def render_active_ticket_status(repo_root: Path, *, verbose: bool = False) -> str:
    """Render the active ticket's roadmap progress without repository diagnostics."""
    try:
        state = load_state(workflow_state_file_for_repo_root(repo_root))
    except WorkflowStateError as exc:
        raise TicketStatusError(str(exc)) from exc

    if state.active_issue_number is None or state.ticket_reference is None:
        raise TicketStatusError("No active ticket workflow is available for /status.")

    try:
        ticket = resolve_ticket_provider_for_reference(
            repo_root=repo_root,
            reference=state.ticket_reference,
        ).get(state.ticket_reference.ticket_id)
    except TicketProviderError as exc:
        raise TicketStatusError(str(exc)) from exc

    if ticket.body is None:
        raise TicketStatusError("Active ticket is missing its project-status content.")

    checkpoints = _parse_checkpoints(ticket.body)
    completed_count = sum(checkpoint.completed for checkpoint in checkpoints)
    current = next((checkpoint for checkpoint in checkpoints if not checkpoint.completed), None)
    current_name = current.name if current is not None else "Complete"

    lines = [
        f"Active ticket: #{ticket.reference.ticket_id} {ticket.title}",
        f"Checkpoints: {completed_count}/{len(checkpoints)} completed",
        f"Current checkpoint: {current_name}",
    ]
    if verbose:
        lines.extend(
            (
                "Full Description:",
                _ticket_section(ticket.body, "Full Description"),
                "Checkpoints:",
                _render_checkpoints(checkpoints),
            )
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in ([], ["verbose"]):
        raise TicketStatusError("Usage: /status [verbose]")
    print(render_active_ticket_status(resolve_repo_root(), verbose=arguments == ["verbose"]))
    return 0


def run() -> None:
    try:
        status = main()
    except (RepositoryError, TicketStatusError) as exc:
        print(f"/status: {exc}", file=sys.stderr)
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    run()
