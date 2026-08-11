# Skills Package

This repository uses a minimal, proven skill package layout.

Canonical layout:

- `skills/<skill-name>/SKILL.md`

Optional subdirectories are allowed only when real skill content requires them:

- `scripts/`
- `src/`
- `tests/`
- `references/`

Rules:

- Do not create speculative category hierarchy directories now.
- Introduce categories later only if repeated real skills demonstrate a clear need.
- If a compatibility catalog is needed for non-native hosts, keep it as a small maintained derivative file under `skills/index.md`.
- Canonical `skills/<skill-name>/SKILL.md` files remain authoritative over any derivative catalog.
- Do not add generated routing files, category hierarchies, or custom skill-loading frameworks.
- Keep each skill self-contained under its own directory.
- Add optional subdirectories only when they contain real, maintained content.
