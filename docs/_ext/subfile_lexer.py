"""Pygments lexer for paperpush ``.sub`` files, used by the docs.

It mirrors the "Highlight" rules in ``.vscode/settings.json`` so code blocks
written with ``:language: subfile`` look the way the file does in VS Code:
comments grey, ``REQUIRED`` red, ``optional`` green, field names teal, and
answers blue. The colors themselves live in ``_static/subfile.css``.
"""

from pygments.lexer import RegexLexer, bygroups
from pygments.token import Comment, Keyword, Name, String, Text, Whitespace


class SubFileLexer(RegexLexer):
    name = "paperpush .sub"
    aliases = ["subfile", "sub"]
    filenames = ["*.sub", "*.sub.txt"]

    tokens = {
        "root": [
            (r"#", Comment, "comment"),
            # Field name (incl. "@venue"), the colon, then the answer.
            (
                r"^(@?[A-Za-z0-9_]+)(:)([ \t]*)(.*)$",
                bygroups(Name.Tag, Text, Whitespace, String),
            ),
            # Indented continuation line under "key: |".
            (r"^([ \t]{2,})(\S.*)$", bygroups(Whitespace, String)),
            (r"\n", Whitespace),
            (r".", Text),
        ],
        "comment": [
            (r"REQUIRED", Keyword),
            (r"optional", Name.Builtin),
            (r"[^\nRo]+", Comment),
            (r"[Ro]", Comment),
            (r"\n", Whitespace, "#pop"),
        ],
    }


def setup(app):
    app.add_lexer("subfile", SubFileLexer)
    app.add_css_file("subfile.css")
    return {"parallel_read_safe": True, "parallel_write_safe": True}
