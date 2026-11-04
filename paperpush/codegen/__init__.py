"""``paperpush codegen``: record a venue's submission wizard as runner code.

An extension of Playwright's own recorder, built on its public API rather than
a fork: :mod:`.session` drives the browser and :mod:`.model` holds the script.
The toolbar adds what writing a venue runner needs: branches, ``step()`` labels,
``.sub`` variables, undo/redo, replay, and copying the page's HTML.
"""

from .model import TARGETS, Node, Script, render, suggest_variable

__all__ = ["TARGETS", "Node", "Script", "render", "suggest_variable", "CodegenSession"]


def __getattr__(name):
    # The session imports Playwright; keep `import paperpush.codegen` cheap.
    if name == "CodegenSession":
        from .session import CodegenSession

        return CodegenSession
    raise AttributeError(name)
