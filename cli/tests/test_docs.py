"""The documentation keeps its promises: every relative link and anchor points at something that exists, every
`scripts/jobsearch <command>` and `scripts/<name>.sh` the docs mention is real, and the files an AI assistant is told
to read are there. These run on the markdown files in the repository (no network)."""
import re
from pathlib import Path

import pytest

import jobsearch_cli as C

REPO = Path(__file__).resolve().parents[2]
FILES = sorted([REPO / "README.md", REPO / "AGENTS.md", REPO / "CLAUDE.md", REPO / "workflows" / "README.md",
                *(REPO / "docs").glob("*.md"), *(REPO / ".claude" / "commands").glob("*.md")])
FENCE = re.compile(r"^\s*(```|~~~)")
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def prose_lines(path: Path):
    """(line number, text) of the lines outside code fences, with inline code removed."""
    in_fence = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield n, re.sub(r"`[^`]*`", "", line)


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces become hyphens."""
    text = re.sub(r"`", "", heading.strip().lower())
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)                  # a link inside a heading keeps its text
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s", "-", text)


def anchors(path: Path) -> set:
    seen: dict = {}
    out = set()
    for n, line in prose_lines_with_code_headings(path):
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            base = slug(m.group(1))
            count = seen.get(base, 0)
            out.add(base if count == 0 else f"{base}-{count}")
            seen[base] = count + 1
    return out


def prose_lines_with_code_headings(path: Path):
    in_fence = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield n, line


def links():
    for path in FILES:
        for n, line in prose_lines(path):
            for target in LINK.findall(line):
                if not re.match(r"^(https?:|mailto:|tel:)", target):
                    yield path, n, target


@pytest.mark.parametrize("path, n, target", list(links()), ids=lambda v: str(v) if not isinstance(v, Path) else v.name)
def test_every_relative_link_and_anchor_exists(path, n, target):
    file_part, _, anchor = target.partition("#")
    dest = (path.parent / file_part).resolve() if file_part else path
    where = f"{path.relative_to(REPO)}:{n} -> {target}"
    assert dest.exists(), f"{where}: no such file"
    if anchor and dest.suffix == ".md":
        assert anchor in anchors(dest), f"{where}: no heading with that anchor in {dest.name}"


def test_every_jobsearch_command_the_docs_mention_exists():
    parser = C.build_parser()
    commands = set(next(a for a in parser._actions if getattr(a, "choices", None) and "doctor" in a.choices).choices)
    problems = []
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"scripts/jobsearch\s+([a-z][a-z-]*)", text):
            word = m.group(1)
            if word not in commands and word not in {"s"}:
                problems.append(f"{path.relative_to(REPO)}: `scripts/jobsearch {word}` is not a command")
    assert not problems, "\n".join(sorted(set(problems)))


def test_every_script_the_docs_mention_exists():
    problems = []
    for path in FILES:
        for m in re.finditer(r"scripts/([A-Za-z0-9_-]+\.sh)", path.read_text(encoding="utf-8")):
            if not (REPO / "scripts" / m.group(1)).is_file():
                problems.append(f"{path.relative_to(REPO)}: scripts/{m.group(1)} does not exist")
    assert not problems, "\n".join(sorted(set(problems)))


def test_the_files_an_assistant_is_sent_to_exist_and_agree():
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert "docs/setup-with-ai.md" in agents and (REPO / "docs" / "setup-with-ai.md").is_file()
    assert "@AGENTS.md" in (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "scripts/check-secrets.sh" in agents
    playbook = (REPO / "docs" / "setup-with-ai.md").read_text(encoding="utf-8")
    for needed in ("Ask before you build", "never pasted to you", "scripts/check-secrets.sh", "push-config", "vm-oracle.md"):
        assert needed.lower() in playbook.lower(), needed


def test_every_slash_command_has_a_file():
    names = {p.stem for p in (REPO / ".claude" / "commands").glob("*.md")}
    claude = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\| `/([a-z]+)`", claude, re.M))
    assert listed and listed <= names, f"AGENTS.md lists commands with no file: {listed - names}"
