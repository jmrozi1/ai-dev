"""A model of the shipped page, so a test can assert what a browser would show.

The shell renders its transcript in the browser, so a test in this repository
cannot run the renderer. The temptation is then to grep the page source for a
string that is *supposed* to imply the rendering, and that is exactly what probe
A7 did: it asserted `".turn.system .bubble" in page`. Independent review emptied
those CSS rules and relabelled `system` to `AGENT` while leaving both greped
substrings in the file, so a system turn rendered identically to an agent turn
and all 106 tests stayed green. The check tested the label on the claim rather
than the fact it stood for.

This module derives the rendering instead. Nothing here is a second copy of the
page's rules:

* the stylesheet is parsed out of `PAGE`,
* the label map is read out of `PAGE`'s `LABEL` object literal,
* the element structure is extracted from `PAGE`'s own `renderChat`.

Every extraction fails closed. If the page stops having a shape this model can
read, `PageModelError` is raised rather than a stale answer returned, because a
model that quietly stops tracking the page is the same defect one level up.

`render_turn` deliberately excludes the turn's class names from its result. A
class attribute with no CSS behind it is invisible to a reader, so comparing
class names would be another label check; what is compared is the label text and
the declarations that actually apply.
"""

import re


class PageModelError(Exception):
    """The page no longer has a shape this model can read."""


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)

# A compound selector this model understands: an optional element or `*`,
# followed by any number of #ids, .classes, [attributes] and :pseudos.
_COMPOUND_RE = re.compile(
    r"""^(?P<tag>[A-Za-z][\w-]*|\*)?
         (?P<rest>(?:\#[\w-]+|\.[\w-]+|\[[^\]]*\]|::?[\w-]+(?:\([^)]*\))?)*)$""",
    re.X,
)
_PART_RE = re.compile(r"\#[\w-]+|\.[\w-]+|\[[^\]]*\]|::?[\w-]+(?:\([^)]*\))?")
_ATTR_RE = re.compile(r"^\[\s*([\w-]+)\s*(?:([~|^$*]?=)\s*(.*?)\s*)?\]$")


def style_source(page):
    """The contents of the page's one `<style>` element."""
    blocks = re.findall(r"<style>(.*?)</style>", page, re.S)
    if len(blocks) != 1:
        raise PageModelError(
            "expected exactly one <style> block in the page, found %d" % len(blocks)
        )
    return blocks[0]


def _split_rules(css):
    """(selector_text, declaration_text) pairs in document order.

    At-rules with a block (`@media`) are descended into, so the rules they hold
    are returned inline; their conditions are not evaluated, which is safe here
    because a conditional rule that applies to one author applies to the other.
    """
    rules = []
    index = 0
    length = len(css)
    while index < length:
        brace = css.find("{", index)
        if brace == -1:
            break
        prelude = css[index:brace].strip()
        depth = 1
        cursor = brace + 1
        while cursor < length and depth:
            if css[cursor] == "{":
                depth += 1
            elif css[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth:
            raise PageModelError("unbalanced braces in the page stylesheet")
        body = css[brace + 1:cursor - 1]
        if prelude.startswith("@"):
            if "{" in body:
                rules.extend(_split_rules(body))
        else:
            rules.append((prelude, body))
        index = cursor
    return rules


def _declarations(text):
    out = {}
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise PageModelError("unreadable declaration %r" % chunk)
        name, _sep, value = chunk.partition(":")
        out[name.strip()] = " ".join(value.split())
    return out


def stylesheet(page):
    """The page's rules as (selector, declarations, order) in document order."""
    css = _COMMENT_RE.sub("", style_source(page))
    rules = []
    for order, (selector, body) in enumerate(_split_rules(css)):
        decls = _declarations(body)
        for one in selector.split(","):
            one = " ".join(one.split())
            if one:
                rules.append((one, decls, order))
    return rules


def _parse_compound(text):
    match = _COMPOUND_RE.match(text)
    if not match:
        raise PageModelError("selector fragment %r is beyond this model" % text)
    compound = {"tag": match.group("tag"), "ids": [], "classes": [],
                "attrs": [], "pseudos": []}
    for part in _PART_RE.findall(match.group("rest") or ""):
        if part.startswith("#"):
            compound["ids"].append(part[1:])
        elif part.startswith("."):
            compound["classes"].append(part[1:])
        elif part.startswith("["):
            attr = _ATTR_RE.match(part)
            if not attr:
                raise PageModelError("attribute selector %r is beyond this model" % part)
            compound["attrs"].append(attr.groups())
        else:
            compound["pseudos"].append(part)
    return compound


def _matches_compound(compound, element):
    if compound["pseudos"]:
        # `:hover`, `:disabled`, `:root` and friends describe a state or a node
        # this model does not represent. Such a rule does not contribute to the
        # resting rendering of a transcript turn.
        return False
    tag = compound["tag"]
    if tag and tag != "*" and tag != element["tag"]:
        return False
    if compound["ids"] and element.get("id") not in compound["ids"]:
        return False
    for name in compound["classes"]:
        if name not in element["classes"]:
            return False
    for name, operator, value in compound["attrs"]:
        present = element.get("attrs", {})
        if name not in present:
            return False
        if operator == "=" and present[name] != value.strip("'\""):
            return False
    return True


def _specificity(compounds):
    ids = sum(len(c["ids"]) for c in compounds)
    classes = sum(len(c["classes"]) + len(c["attrs"]) + len(c["pseudos"]) for c in compounds)
    tags = sum(1 for c in compounds if c["tag"] and c["tag"] != "*")
    return (ids, classes, tags)


def _matches(selector, path):
    """Does `selector` match the last element of `path` (a list of ancestors)?"""
    if re.search(r"[>+~]", selector):
        raise PageModelError(
            "selector %r uses a combinator this model does not implement; the "
            "model must be taught it rather than silently not matching" % selector
        )
    compounds = [_parse_compound(part) for part in selector.split()]
    if not compounds:
        return None
    if not _matches_compound(compounds[-1], path[-1]):
        return None
    remaining = list(compounds[:-1])
    for element in reversed(path[:-1]):
        if remaining and _matches_compound(remaining[-1], element):
            remaining.pop()
    if remaining:
        return None
    return _specificity(compounds)


def computed_style(rules, path):
    """The declarations that apply to `path[-1]`, cascaded."""
    matched = []
    for selector, decls, order in rules:
        specificity = _matches(selector, path)
        if specificity is not None:
            matched.append((specificity, order, decls))
    matched.sort(key=lambda item: (item[0], item[1]))
    computed = {}
    for _spec, _order, decls in matched:
        computed.update(decls)
    return computed


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(r"var\s+LABEL\s*=\s*\{(?P<body>[^{}]*)\}\s*;")
_LABEL_ENTRY_RE = re.compile(r"""(?P<key>[\w-]+|"[^"]*")\s*:\s*"(?P<value>[^"]*)"\s*""")

# The transcript renderer, as this model reads it. It is matched rather than
# assumed: if `renderChat` stops building a turn this way the extraction fails,
# instead of the model reporting a rendering the page no longer produces.
_RENDER_RE = re.compile(
    r"""chat\.messages\.forEach\(function\s*\(message\)\s*\{\s*
        var\s+turn\s*=\s*element\(\s*"div"\s*,\s*"(?P<turn_class>[\w-]+)\s"\s*\+\s*message\.author\s*\)\s*;\s*
        turn\.appendChild\(\s*element\(\s*"div"\s*,\s*"(?P<who_class>[\w-]+)"\s*,\s*
            LABEL\[message\.author\]\s*\|\|\s*message\.author\s*\)\s*\)\s*;\s*
        turn\.appendChild\(\s*element\(\s*"div"\s*,\s*"(?P<bubble_class>[\w-]+)"\s*,\s*
            message\.text\s*\)\s*\)\s*;""",
    re.X,
)


def label_map(page):
    match = _LABEL_RE.search(page)
    if not match:
        raise PageModelError("the page has no readable LABEL map")
    labels = {}
    for entry in match.group("body").split(","):
        entry = entry.strip()
        if not entry:
            continue
        pair = _LABEL_ENTRY_RE.match(entry)
        if not pair:
            raise PageModelError("unreadable LABEL entry %r" % entry)
        labels[pair.group("key").strip('"')] = pair.group("value")
    return labels


def turn_structure(page):
    """The class names `renderChat` gives a turn, its avatar and its bubble."""
    match = _RENDER_RE.search(page)
    if not match:
        raise PageModelError(
            "renderChat no longer builds a turn in a shape this model can read; "
            "teach the model the new shape rather than trusting this assertion"
        )
    return (match.group("turn_class"), match.group("who_class"),
            match.group("bubble_class"))


def render_turn(page, author, text):
    """What a reader would see for one transcript turn by `author`.

    Class names are deliberately absent from the result: an author-specific
    class with no CSS behind it changes nothing a reader can see, so including
    it would let a rendering that looks identical compare as different.
    """
    turn_class, who_class, bubble_class = turn_structure(page)
    rules = stylesheet(page)
    labels = label_map(page)
    turn = {"tag": "div", "classes": [turn_class, author], "attrs": {}}
    who = {"tag": "div", "classes": [who_class], "attrs": {}}
    bubble = {"tag": "div", "classes": [bubble_class], "attrs": {}}
    return {
        "avatar_text": labels.get(author, author),
        "body_text": text,
        "turn_style": computed_style(rules, [turn]),
        "avatar_style": computed_style(rules, [turn, who]),
        "bubble_style": computed_style(rules, [turn, bubble]),
    }


def rendering_differences(first, second):
    """The facets in which two `render_turn` results visibly differ."""
    return sorted(key for key in set(first) | set(second)
                  if first.get(key) != second.get(key))
