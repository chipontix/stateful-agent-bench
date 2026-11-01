"""Mutants of a task's reference fix, to check that its verifier would notice
a fix that is almost right.

Only the lines the fix touches are mutated:
  revert-hunk  one hunk of fix.patch undone, the rest kept (a partial fix)
  delete-line  one added line removed
  flip         one operator or literal on an added line inverted
               (== !=, < <=, > >=, && ||, and or, true false, True False)
A mutant that does not build or compile is "stillborn" and left out of the
score: the compiler caught it, not the verifier.
"""

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import workspace

FLIPS = [("==", "!="), ("!=", "=="), ("<=", "<"), (">=", ">"), (" < ", " <= "), (" > ", " >= "),
         ("&&", "||"), ("||", "&&"), (" and ", " or "), (" or ", " and "), ("true", "false"), ("false", "true"),
         ("True", "False"), ("False", "True")]
SKIP = re.compile(r"^\s*($|//|#|--|[{}()\[\],;]+$|\"\"\"|\w+:\s*$)")
MAX = 12


@dataclass
class Mutant:
    id: str
    op: str
    path: str
    line: int  # 1-based, in the reference file
    before: str
    after: str
    text: str  # the whole mutated file

    def describe(self) -> str:
        if self.op == "revert-hunk":
            return f"{self.path}:{self.line} hunk reverted to the buggy version"
        if self.op == "delete-line":
            return f"{self.path}:{self.line} deleted `{self.before.strip()}`"
        return f"{self.path}:{self.line} `{self.before.strip()}` -> `{self.after.strip()}`"


def hunks(patch: str):
    """Yields (path, new_start, old_lines, new_lines, [(new_lineno, text) of added lines]).
    Hunk bodies are read by their line counts (a removed "-- comment" line
    looks like a file header otherwise)."""
    path, cur, left = None, None, (0, 0)
    for line in patch.splitlines():
        if left != (0, 0):
            line = line or " "
            if line[0] in " -":
                cur[2].append(line[1:])
            if line[0] in " +":
                cur[3].append(line[1:])
                if line[0] == "+":
                    cur[4].append((cur[5], line[1:]))
                cur[5] += 1
            left = (left[0] - (line[0] in " -"), left[1] - (line[0] in " +"))
            if left == (0, 0):
                yield tuple(cur[:5])
        elif line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            new_start = int(m.group(2))
            cur = [path, new_start, [], [], [], new_start]
            left = (int(m.group(1) or 1), int(m.group(3) or 1))


def prose(path: Path) -> set[int]:
    """1-based numbers of the lines that are comments or docstrings."""
    text = path.read_text()
    lines = text.split("\n")
    out = {i for i, l in enumerate(lines, 1) if re.match(r"\s*(//|#|--)", l)}
    if path.suffix == ".py":
        for node in ast.walk(ast.parse(text)):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                out |= set(range(body[0].lineno, body[0].end_lineno + 1))
    return out


def generate(task: workspace.Task, ref: Path) -> list[Mutant]:
    """ref: a checkout of the bug snapshot with fix.patch applied. Comments and
    docstrings are never mutated: such mutants are equivalent by construction."""
    fix = task.patch("fix").read_text()
    by_kind: dict[str, list[Mutant]] = {"revert-hunk": [], "delete-line": [], "flip": []}
    for path, start, old, new, added in hunks(fix):
        lines = (ref / path).read_text().split("\n")
        skip = prose(ref / path)
        i = start - 1
        assert lines[i:i + len(new)] == new, f"{path}:{start} fix hunk not found in the reference"
        changed = [n for n, _ in added]
        removed = [x for x in old if x not in new]
        code_removed = [x for x in removed if not re.match(r"\s*(//|#|--)", x) and x.strip()]
        if any(n not in skip for n in changed) or (code_removed and not path.endswith(".py")) or \
                (path.endswith(".py") and code_removed and any(n not in skip for n in range(start, start + len(new)))):
            m = lines[:i] + old + lines[i + len(new):]
            by_kind["revert-hunk"].append(Mutant("", "revert-hunk", path, start, "\n".join(new), "\n".join(old),
                                                 "\n".join(m)))
        for n, text in added:
            if n in skip or SKIP.match(text):
                continue
            m = lines[:n - 1] + lines[n:]
            by_kind["delete-line"].append(Mutant("", "delete-line", path, n, text, "", "\n".join(m)))
            for a, b in FLIPS:
                if a in text:
                    t = text.replace(a, b, 1)
                    m = lines[:n - 1] + [t] + lines[n:]
                    by_kind["flip"].append(Mutant("", "flip", path, n, text, t, "\n".join(m)))
                    break
    # Every hunk revert, then alternate the line-level kinds up to MAX.
    out = by_kind["revert-hunk"][:MAX]
    rest = [x for pair in zip(by_kind["flip"], by_kind["delete-line"]) for x in pair]
    rest += by_kind["flip"][len(by_kind["delete-line"]):] + by_kind["delete-line"][len(by_kind["flip"]):]
    seen = {m.text for m in out}
    for m in rest:
        if len(out) >= MAX:
            break
        if m.text not in seen:
            seen.add(m.text)
            out.append(m)
    for k, m in enumerate(out, 1):
        m.id = f"m{k:02d}"
    return out


def stillborn(repo: Path, m: Mutant) -> str | None:
    """Why the mutant cannot run at all, if it cannot."""
    if m.path.endswith(".py"):
        p = subprocess.run(["python3", "-m", "py_compile", str(repo / m.path)], capture_output=True, text=True)
        return p.stderr.strip()[-300:] or None
    if m.path.endswith(".go"):
        p = subprocess.run(["go", "build", "-o", "/dev/null", "./" + str(Path(m.path).parent)], cwd=repo,
                           capture_output=True, text=True)
        return p.stderr.strip()[-300:] if p.returncode else None
    return None
