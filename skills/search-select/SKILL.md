---
name: search-select
description: Design, implement, or review a browse-first searchable single-select control when users should be able to browse available options immediately or type to filter them in place. Use for ordinary entity selection and select-to-add interactions.
---

# Search Select

Use this skill for a single-selection control that combines the browseability of
a traditional select with immediate typed filtering.

Keep v1 deliberately small. Add behavior only when real product friction earns it.

## Core Interaction

The control should support both browsing and known-item retrieval without
requiring separate modes.

- Present options alphabetically.
- Clicking or tapping the control opens the result list immediately.
- Do not require the user to type before options become visible.
- Typing opens the result list when necessary and filters the complete option
  collection immediately.
- Keep the search field and selected value in the same control rather than
  introducing a separate search box.

## Result Limits

Treat visible height and searchable collection size as separate concerns.

- Default to roughly 10 visible result rows before internal scrolling.
- Default to at most 50 rendered results available through scrolling.
- Both limits are configurable.
- Search always evaluates the complete option collection, not merely the
  currently rendered results.
- When more matches exist than the rendered-result limit, communicate that
  additional results exist and that further typing can narrow the set.
- Do not add pagination or infinite scrolling in v1.

## Filtering

Keep v1 filtering deterministic and simple.

1. Compare case-insensitively.
2. Split the query on whitespace.
3. Require every non-empty query term to occur as a substring of the displayed
   option text.
4. Keep matching results alphabetically ordered.

Example:

`ben pre` matches `Bench Dumbbell Press` because both `ben` and `pre` occur in
the displayed text.

V1 does not rank results and does not provide fuzzy matching, aliases, synonyms,
or hidden searchable fields.

## Keyboard Behavior

Preserve familiar select behavior.

When the result list is closed:

- Up and Down change the committed selection directly, like a traditional
  single-select control.

When the result list is open:

- Up and Down navigate the available results.
- Keep the active keyboard result visible while navigating a scrollable list.
- Enter selects the active result.
- Escape closes the list without changing the previously committed selection.

Pointer/touch and keyboard interaction are both first-class paths.

## Selection Modes

Support two v1 modes.

### Ordinary selection

Selecting an option:

- commits the selected value;
- closes the result list;
- leaves that value selected.

### Select to add

Selecting an option:

- immediately performs the caller-defined add action;
- closes the result list;
- resets the control so another item can be added.

Do not prohibit duplicate selections at the component level. Whether duplicates
are valid belongs to the caller.

## Empty Search Result

When the query matches no options, show a simple `No matches` state.

Do not invent application-specific empty-library, creation, or recovery behavior
inside this skill.

## Accessibility

Use proper accessible combobox/list semantics and accessible naming.

Equivalent selection and navigation must be available from the keyboard. The
visible placeholder is not a substitute for an accessible control name.

Keep accessibility outcomes required while leaving the exact DOM, framework,
component library, and ARIA implementation to the project.

## Out Of Scope For V1

Do not add these without concrete product need:

- fuzzy matching;
- relevance ranking;
- aliases, synonyms, or hidden search fields;
- multi-select;
- create-new-item-from-search behavior;
- async or remote-search architecture;
- pagination or infinite scrolling;
- specialized mobile-only interaction models;
- generalized search infrastructure.

## Validation

Use evidence appropriate to the implementation. At minimum verify:

- options can be browsed before typing;
- alphabetical ordering;
- visible-height scrolling and rendered-result limits;
- filtering searches the complete collection;
- case-insensitive ANDed substring filtering;
- the additional-results indication when the cap is exceeded;
- closed Up/Down selection;
- open Up/Down, Enter, and Escape behavior;
- ordinary selection;
- select-to-add and reset;
- `No matches`;
- accessible naming and equivalent keyboard operation.

Do not require a particular framework or implementation architecture to satisfy
the contract.
