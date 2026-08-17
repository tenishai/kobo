"""API error copy stays plain: no em dashes in string literals that
reach users. Scans every string literal in main.py via its AST (not a
line-based regex keyed to "error"/"detail" substrings, which is blind to
any em-dash string that doesn't happen to share a line with one of those
tokens — e.g. `_validation_error_message`'s return values, which live on
their own lines two statements away from the `{"error": ...}` call site
that serves them). Docstrings (the first statement of the module, or of
any class/function body, when it's a bare string expression) are exempt,
same as comments — comments never reach the AST at all, so they need no
special-casing here."""
import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "kobo_api" / "main.py"


def _docstring_ids(tree: ast.AST) -> set[int]:
    """id() of every AST string-`Constant` node that is a docstring: the
    first statement of the module, or of a class/function body, when that
    statement is a bare `Expr` whose value is a string constant. Returning
    the node ids (rather than e.g. line numbers) lets the caller exclude
    exactly those `Constant` objects from a walk over the same tree, with
    no risk of an unrelated literal on the same line being exempted too.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def test_no_em_dashes_in_error_literals():
    tree = ast.parse(SOURCE.read_text())
    docstring_ids = _docstring_ids(tree)
    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "—" in node.value
        and id(node) not in docstring_ids
    ]
    assert offenders == [], offenders
