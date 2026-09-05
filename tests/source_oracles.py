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
- `tests/test_claude_runtime.py` and `tests/test_role_invocation.py` asserted
  `"expected_skill=expected_skill, role=record.role" in source` to hold the *role*
  side of the plugin gate -- where the gate reads its role operand from, which the
  call merely existing does not answer. Both failure modes met in that one
  assertion: weakening the call to `role=expected_skill` while adding a comment
  quoting the asserted shape flipped both tests from failing to passing with the
  code broken, and reformatting the real call across four lines failed both on an
  edit that changes nothing. `keyword_bindings` answers it off the tree.

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

    The easiest evasion is neither of those, and is named here because omitting it
    overstated what this resolution buys. Resolution is syntactic and follows no
    binding, so a plain name rebinding -- `_gate = validate_plugin_surface` on one
    line and `_gate(...)` on the next -- resolves to `_gate`. A scan for
    `validate_plugin_surface` does not see that call at all, with no attribute and
    no subscript anywhere in it, and nothing in this suite catches it today. It is
    a disclosed residual of resolving names rather than resolving bindings.
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
        found.append((module, function, node))
    for child in ast.iter_child_nodes(node):
        _collect(child, function, name, module, found)


def _calls(name, modules):
    """Every call to `name`, as (module, enclosing function, the `ast.Call` node).

    The one traversal both public oracles below read: they differ only in what
    they keep off each node, and a second traversal would be a second answer.
    """
    found = []
    for path in _sources(modules):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _collect(tree, None, name, path.name, found)
    return found


def call_sites(name, *, modules=None):
    """Every call to `name`, as `(module, innermost enclosing function, line)`.

    `modules` scopes the scan to named files; the default scans the whole package,
    which is what the single-chokepoint property needs -- a second call in *any*
    module would defeat it, so the oracle must not be scoped to one file.

    A `def`, an import, a type annotation and a mention in prose are not calls and
    are not counted. That is the entire point.
    """
    return [(module, function, call.lineno) for module, function, call in _calls(name, modules)]


def call_locations(name, *, modules=None):
    """`call_sites` without line numbers, for assertions where the line is noise.

    Compared against a literal list, this asserts the count and the location in one
    reading: it cannot pass with an extra call, nor with the right number of calls
    in the wrong function.
    """
    return [(module, function) for module, function, _ in call_sites(name, modules=modules)]


def _binding_expression(value):
    """One argument's value as canonical source regenerated from the tree.

    `ast.unparse` re-renders the parsed node instead of quoting the file, so the
    string compared against is one the file's own formatting cannot reach: it
    carries no comment, no line break and no incidental spacing. A call spread
    across four lines and the same call on one line produce the same string, and a
    comment that merely quotes the call produces no string at all, because a
    comment is not a call.
    """
    return ast.unparse(value)


def _keyword_bindings(call):
    bound = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            # `f(**mapping)` binds names this oracle cannot read. Recording it
            # under a key no keyword argument can have keeps such a call visibly
            # different from one that binds nothing, rather than silently empty.
            bound["**"] = _binding_expression(keyword.value)
            continue
        bound[keyword.arg] = _binding_expression(keyword.value)
    return bound


def keyword_bindings(name, *, modules=None):
    """Every call to `name`, as `(module, enclosing function, {keyword: expression})`.

    `call_locations` says a call happens and where; this says what it was handed.
    That is the separate question a gate raises -- a gate that reads its operand
    from the caller's own argument instead of from durable state is still exactly
    one call in exactly the right function -- and it is the question a substring
    search over the source was being asked, and answering badly in both
    directions at once.

    Positional arguments are deliberately not reported. What is being pinned here
    are the keywords, and a value moved out of one shows up as a keyword that is
    no longer bound, which is a difference this oracle should report rather than
    absorb.
    """
    return [
        (module, function, _keyword_bindings(call))
        for module, function, call in _calls(name, modules)
    ]
