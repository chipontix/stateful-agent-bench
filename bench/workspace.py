"""Tasks and the repositories they run in.

A task is tasks/<id>/: task.json, issue.md and patches against one pinned
commit of an upstream project. upstream/<project> is a plain clone (not part
of this repository) that `pristine` exports the pinned commit from.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = ROOT / "upstream"
GITHUB = "https://github.com/chipontix/"
AUTHOR = ["-c", "user.name=stateful-agent-bench", "-c", "user.email=bench@localhost"]


@dataclass
class Task:
    id: str
    dir: Path
    project: str
    commit: str
    verifier: dict
    protected: list[str]

    @property
    def issue(self) -> str:
        return (self.dir / "issue.md").read_text()

    def patch(self, kind: str) -> Path:
        return self.dir / f"{kind}.patch"


def load(tid: str) -> Task:
    d = ROOT / "tasks" / tid
    j = json.loads((d / "task.json").read_text())
    return Task(tid, d, j["project"], j["commit"], j["verifier"], j.get("protected", []))


def all_tasks() -> list[Task]:
    return [load(p.parent.name) for p in sorted((ROOT / "tasks").glob("*/task.json"))]


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *AUTHOR, *args], check=check, capture_output=True,
                          text=True).stdout


def upstream(task: Task) -> Path:
    """The upstream clone, fetched if missing or older than the pinned commit."""
    repo = UPSTREAM / task.project
    if not repo.exists():
        UPSTREAM.mkdir(exist_ok=True)
        subprocess.run(["git", "clone", "-q", GITHUB + task.project, str(repo)], check=True)
    if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", task.commit + "^{commit}"]).returncode:
        subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
    return repo


def pristine(task: Task, dest: Path, paths: list[str] | None = None) -> None:
    """Exports the pinned commit (or just `paths` of it) into dest."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(upstream(task)), "archive", task.commit, *(paths or [])],
                             check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)


def apply(repo: Path, patch: Path | str) -> None:
    """repo must be a git work tree of its own (see build)."""
    text = patch.read_text() if isinstance(patch, Path) else patch
    if text.strip():
        p = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=repo, input=text, text=True,
                           capture_output=True)
        if p.returncode:
            raise RuntimeError(f"patch does not apply in {repo}: {p.stderr.strip()}")


def build(task: Task, dest: Path, *extra: Path | str) -> Path:
    """The repository an agent starts from: upstream + bug.patch, as a fresh
    git repo with one commit, so neither the history nor the diff gives the
    bug away. Extra patches (a fix, a mutant) are applied on top, uncommitted."""
    if dest.exists():
        shutil.rmtree(dest)
    pristine(task, dest)
    git(dest, "init", "-q", "-b", "main")  # first: inside another work tree, git apply would resolve paths there
    apply(dest, task.patch("bug"))
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", f"{task.project} snapshot")
    for p in extra:
        apply(dest, p)
    return dest


def changes(repo: Path) -> str:
    """Everything changed since the snapshot commit, new files included."""
    git(repo, "add", "-A")
    return git(repo, "diff", "--cached", "HEAD")


def changed_paths(repo: Path) -> list[str]:
    git(repo, "add", "-A")
    return [p for p in git(repo, "diff", "--cached", "--name-only", "HEAD").splitlines() if p]


def is_protected(task: Task, path: str) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in task.protected)


def verification_copy(task: Task, repo: Path, dest: Path) -> list[str]:
    """Copies repo to dest and puts the protected paths (the harness, the
    reference, the checker) back to the pinned upstream version. Returns the
    protected paths the agent changed; the verifier never sees those changes."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(repo, dest, ignore=shutil.ignore_patterns(".git"), symlinks=True)
    tampered = [p for p in changed_paths(repo) if is_protected(task, p)]
    for p in task.protected:
        target = dest / p
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    upstream_paths = [p for p in task.protected
                      if subprocess.run(["git", "-C", str(upstream(task)), "cat-file", "-e", f"{task.commit}:{p}"],
                                        capture_output=True).returncode == 0]
    if upstream_paths:
        pristine(task, dest, upstream_paths)
    return tampered
