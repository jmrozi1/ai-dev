"""Structural oracles over the package's own source, read as code and not as text.

Several assertions in this suite answer a question about the *call graph* -- "how
many times is this called, and from where" -- and used to answer it with
`source.count("name(")`. A substring count reads formatting, so it answers a
different question than the one asked:

- `tests/test_claude_runtime.py` asserted
  `module.count("validate_plugin_surface(" + chr(10)) == 1` to hold the
  single-chokepoint property. It counted calls *whose open paren was followed by a
  newline*. A genuine second call to the validator written on one line satisfied it,
  and no shipped test died -- so the guard on the property this boundary exists for
  could be walked past by reformatting.
- `text.count("drive_roles(")`, `source.count("controller.agent_count(")` and
  `entry.count("dispatch_role(")` are failed by a *comment* that mentions the call
  without making one, and the modules they read argue at length in comments about
  exactly these calls. An oracle that a comment can break pushes the reasoning out
  of the code.

Parsing removes both failure modes at once: comments, whitespace and line breaks
are not in the tree, and a call is a call however it is spelled across lines.
"""

import ast
from pathlib import Path


def package_root():
    """The `ai_dev_flow/` directory, located through the imported package."""
    import ai_dev_flow

    return Path(ai_dev_flow.__file__).parent


def _sources(modules):
    root = package_root()
    if modules is None:
        return [
            path for path in sorted(root.rglob("*.py"))
            if "__pycache__" not in path.parts
        ]
    return [root / name for name in modules]


def _resolve_callee(call):
    """The name a call is made through: `f(...)` and `obj.f(...)` both resolve to `f`.

    Resolving on the final segment means a call cannot escape the count by being
    reached through an attribute, which is how `dispatch_role` and `agent_count`
    are actually reached. A call through a subscript or another expression resolves
    to nothing and is not counted; that is a real limit, stated rather than hidden.
    """
    callee = call.func
    if isinstance(callee, ast.Attribute):
        return callee.attr
    if isinstance(callee, ast.Name):
        return callee.id
    return None


def _collect(node, function, name, module, found):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        function = node.name
    if isinstance(node, ast.Call) and _resolve_callee(node) == name:
        found.append((module, function, node.lineno))
    for child in ast.iter_child_nodes(node):
        _collect(child, function, name, module, found)


def call_sites(name, *, modules=None):
    """Every call to `name`, as `(module, innermost enclosing function, line)`.

    `modules` scopes the scan to named files; the default scans the whole package,
    which is what the single-chokepoint property needs -- a second call in *any*
    module would defeat it, so the oracle must not be scoped to one file.

    A `def`, an import, a type annotation and a mention in prose are not calls and
    are not counted. That is the entire point.
    """
    found = []
    for path in _sources(modules):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _collect(tree, None, name, path.name, found)
    return found


def call_locations(name, *, modules=None):
    """`call_sites` without line numbers, for assertions where the line is noise.

    Compared against a literal list, this asserts the count and the location in one
    reading: it cannot pass with an extra call, nor with the right number of calls
    in the wrong function.
    """
    return [(module, function) for module, function, _ in call_sites(name, modules=modules)]
