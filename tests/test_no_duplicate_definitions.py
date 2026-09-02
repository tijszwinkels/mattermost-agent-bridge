"""No name may be defined twice in the same module or class body.

WHY this test exists, and why it is repo-wide rather than a fix to one file:

Round F3's entire root cause was a method defined twice. `class Bridge` had two
`_backend_for_channel` definitions — one written to say "the backend" when it
didn't know, one written to guess `config.default_backend` for a Resume line.
Python keeps the LAST definition in a class body, so the honest one was dead
code and every error message inherited the guess. Two real incidents blamed the
`claude` backend for a `pi` session because of it.

Nothing in the language, the linters as configured, or code review caught that.
The method split in this round fixed the *instance*; this test is the fix for
the *class of bug*. It would have failed on `main` before the split, and it
failed on this branch too — the author of the split promptly introduced a
second `purpose.canonical_backend` next to the one that already existed.

Scope is deliberately DEFINITIONS (`def`, `async def`, `class`), not
assignments: rebinding a module-level name is ordinary Python, whereas a
redefined function or class is nearly always a mistake or a merge artifact.
Only DIRECT children of a module or class body are considered, so definitions
guarded by `if TYPE_CHECKING:` / `try: … except ImportError:` are naturally
exempt — those are deliberate alternatives, not collisions.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "mm_bridge"

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _is_exempt(node: ast.AST) -> bool:
    """Decorators that make a repeated name legitimate.

    `@property` + `@x.setter` / `@x.deleter` define one attribute across
    several defs, and `@overload` stubs precede the real implementation.
    """
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Name) and dec.id in {"property", "overload"}:
            return True
        if isinstance(dec, ast.Attribute) and dec.attr in {
            "setter", "getter", "deleter", "overload",
        }:
            return True
    return False


def duplicate_definitions(source: str, filename: str = "<src>") -> list[str]:
    """Names defined more than once in the same module or class body.

    Returns human-readable "file:line ..." strings, empty when clean.
    """
    tree = ast.parse(source, filename=filename)
    problems: list[str] = []

    def scan(body: list[ast.stmt], scope: str) -> None:
        seen: dict[str, int] = {}
        for node in body:
            if not isinstance(node, _DEF_NODES) or _is_exempt(node):
                continue
            if node.name in seen:
                problems.append(
                    f"{filename}: {scope}{node.name} defined twice "
                    f"(lines {seen[node.name]} and {node.lineno}) — "
                    f"Python keeps the last, so the first is dead code"
                )
            seen[node.name] = node.lineno

    scan(tree.body, "")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            scan(node.body, f"{node.name}.")

    return problems


class DetectorTests(unittest.TestCase):
    """The detector itself, so a green repo-wide run means something."""

    def test_catches_the_bug_this_round_was_about(self):
        """Verbatim shape of the `bridge.py:1662/4076` defect."""
        found = duplicate_definitions(
            "class Bridge:\n"
            "    def _backend_for_channel(self):\n"
            "        return None\n"
            "    def _other(self):\n"
            "        return 1\n"
            "    def _backend_for_channel(self):\n"
            "        return 'claude'\n"
        )
        self.assertEqual(len(found), 1, found)
        self.assertIn("Bridge._backend_for_channel", found[0])

    def test_catches_a_module_level_duplicate(self):
        """The shape introduced on this very branch, in purpose.py."""
        found = duplicate_definitions(
            "def canonical_backend(x):\n    return x\n"
            "def canonical_backend(x):\n    return x.lower()\n"
        )
        self.assertEqual(len(found), 1, found)

    def test_property_and_setter_are_not_a_duplicate(self):
        self.assertEqual(duplicate_definitions(
            "class C:\n"
            "    @property\n    def x(self): return 1\n"
            "    @x.setter\n    def x(self, v): pass\n"
        ), [])

    def test_overloads_are_not_a_duplicate(self):
        self.assertEqual(duplicate_definitions(
            "from typing import overload\n"
            "@overload\ndef f(x: int) -> int: ...\n"
            "def f(x): return x\n"
        ), [])

    def test_conditional_definitions_are_not_a_duplicate(self):
        """Deliberate alternatives, never both live at once."""
        self.assertEqual(duplicate_definitions(
            "import sys\n"
            "if sys.version_info >= (3, 12):\n    def f(): return 'new'\n"
            "else:\n    def f(): return 'old'\n"
        ), [])

    def test_same_name_in_different_classes_is_fine(self):
        self.assertEqual(duplicate_definitions(
            "class A:\n    def run(self): pass\n"
            "class B:\n    def run(self): pass\n"
        ), [])

    def test_clean_source_yields_nothing(self):
        self.assertEqual(duplicate_definitions("def a(): pass\ndef b(): pass\n"), [])


class RepoTests(unittest.TestCase):
    def test_no_module_defines_a_name_twice(self):
        problems: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            problems += duplicate_definitions(
                path.read_text(), filename=str(path.relative_to(SRC.parent.parent)),
            )
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
