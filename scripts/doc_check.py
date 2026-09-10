#!/usr/bin/env python3
"""Documentation staleness check.

Re-reads the reference documentation (AGENTS.md, README.md, llms.txt, docs/)
and verifies that the concrete references it cites still exist in the
repository: local links and anchors, file paths, directory trees, environment
variables, Docker Compose services and container names, npm/make targets.
It catches broken references (names that no longer exist), not descriptions
that have become false: those are still on whoever changes the code.

Usage:
    python3 scripts/doc_check.py        # exit code 1 if a reference is broken
    python3 scripts/doc_check.py -v     # also list the documents checked

Markers (HTML comments, invisible once rendered):
    <!-- doc-check:ignore -->           skip the line it is on
    <!-- doc-check:ignore-start -->     skip everything up to the matching
    <!-- doc-check:ignore-end -->
    <!-- doc-check:ignore-file -->      skip the whole document

Standard library only (python3 >= 3.8): nothing to install.
"""

import argparse
import bisect
import fnmatch
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

# --- Project configuration ---------------------------------------------------

# Reference documentation (globs relative to the repo root). Entries without
# wildcards are mandatory: a missing one is an error.
DOCS = ["AGENTS.md", "README.md"]
# Dated snapshots and archive: kept for history, never checked.
DOCS_EXCLUDE = ["docs/archive/**"]
# Directories a relative path cited in the docs may start from.
SEARCH_ROOTS = ["."]
# Absolute paths where this repository is deployed: /opt/docker/x/... cited
# in the docs is checked as a path inside the repo.
DEPLOY_PATHS = []
# Containers of other stacks the docs legitimately mention.
EXTERNAL_CONTAINERS = []
# UPPER_CASE names cited in the docs but defined outside this repository
# (e.g. variables read by an upstream image).
EXTERNAL_NAMES = []
# Exact references that look like paths or names but are not in this repo
# (MQTT topics sharing a name with a directory, files inside an image, ...).
IGNORE_REFS = []
# Every reference document must be linked from llms.txt (when it exists).
LLMS_MUST_LIST_DOCS = True

# -----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent

# Markers count only as real HTML comments: quoted in `inline code` or inside
# a fenced block (e.g. to document them) they are not markers.
MARK_RE = re.compile(r"<!--\s*doc-check:(ignore(?:-start|-end|-file)?)\s*-->")

FILE_EXTS = (
    "py sh bash yml yaml md json js mjs ts php conf cfg toml txt items things "
    "rules sitemap persist map csv xml ini sql example env html css lock db"
).split()
SPECIAL_FILES = {"Dockerfile", "Makefile", "LICENSE", "artisan", ".gitignore",
                 ".dockerignore", ".env.example"}
BARE_FILE_RE = re.compile(r"^\.?[\w.-]+\.(?:%s)$" % "|".join(FILE_EXTS))
ENV_NAME = r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
ENV_TOKEN_RE = re.compile(r"^\$?\{?(%s)\}?$" % ENV_NAME)
ENV_ASSIGN_RE = re.compile(r"^(%s)=" % ENV_NAME)
ENV_REF_RE = re.compile(r"\$\{?(%s)\b" % ENV_NAME)
PLACEHOLDER_CHARS = ("<", ">", "...", "…", "$", "{", "}", "%", "@", "|")
URL_SCHEMES = ("http:", "https:", "mailto:", "ftp:", "tel:", "ws:", "wss:",
               "mqtt:", "rtsp:", "file:", "data:", "vscode:")
LINK_RE = re.compile(r"!?\[(?:[^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^)]*)?\)")
REFLINK_RE = re.compile(r"^\s*\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
CODE_SPAN_RE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)")
TREE_RE = re.compile(r"^(?P<indent>(?:[│|][  ]{2,3}|[  ]{4})*)"
                     r"(?:[├└]──|\|--|`--)\s*(?P<name>\S+)")
SHELL_OPS = {"|", "||", "&&", ";", "&", "(", ")", ";;"}
REDIRECTS = {">", ">>", "<", "2>", "2>>", "&>", ">&", "<<", "<<<"}
INTERPRETERS = {"python", "python3", "bash", "sh", "node", "php", "source", "."}
# docker compose: global options taking a value.
COMPOSE_GLOBAL_OPTS = {"-f", "--file", "-p", "--project-name", "--profile",
                       "--env-file", "--project-directory", "--ansi",
                       "--progress", "--parallel"}
# docker compose subcommand -> options taking a value.
COMPOSE_SUBCMDS = {
    "up": {"--scale", "-t", "--timeout", "--exit-code-from", "--pull",
           "--wait-timeout", "--attach", "--no-attach"},
    "down": {"-t", "--timeout", "--rmi"},
    "logs": {"--tail", "-n", "--since", "--until"},
    "restart": {"-t", "--timeout"}, "stop": {"-t", "--timeout"},
    "start": set(), "kill": {"-s", "--signal"}, "rm": set(), "pause": set(),
    "unpause": set(), "top": set(), "create": {"--scale", "--pull"},
    "build": {"--build-arg", "--progress", "--ssh", "-m", "--memory"},
    "pull": set(), "push": set(), "ps": {"--format", "--filter", "--status"},
    "images": {"--format"}, "events": set(), "port": {"--index", "--protocol"},
    "exec": {"-e", "--env", "-u", "--user", "-w", "--workdir", "--index"},
    "run": {"-e", "--env", "-u", "--user", "-w", "--workdir", "-v",
            "--volume", "--name", "--entrypoint", "-p", "--publish", "-l",
            "--label", "--cap-add", "--cap-drop", "--pull"},
}
# docker <subcommand> <container>: options taking a value.
DOCKER_CONTAINER_SUBCMDS = {
    "logs": {"--tail", "-n", "--since", "--until"},
    "exec": {"-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file"},
    "restart": {"-t", "--time", "-s", "--signal"},
    "stop": {"-t", "--time", "-s", "--signal"}, "start": set(),
    "kill": {"-s", "--signal"}, "inspect": {"-f", "--format", "--type"},
    "top": set(), "stats": {"--format"}, "port": set(), "attach": set(),
    "pause": set(), "unpause": set(), "wait": set(), "rm": set(),
    "update": {"--cpus", "-m", "--memory", "--restart"},
}


class Checker:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.errors = []
        self.checked = 0
        self._ignored = {}
        self._tracked = None
        self._basenames = None
        self._identifiers = None
        self._sorted_ids = None
        self._compose = None
        self._headings = {}

    # --- repository index ----------------------------------------------------

    def git(self, *args):
        try:
            res = subprocess.run(["git", "-C", str(ROOT), *args],
                                 capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return None
        return res

    def tracked(self):
        """Tracked plus untracked-but-not-ignored files, repo-relative."""
        if self._tracked is None:
            res = self.git("ls-files", "-co", "--exclude-standard", "-z")
            if res is not None and res.returncode == 0:
                self._tracked = [p for p in res.stdout.split("\0") if p]
            else:
                self._tracked = [str(p.relative_to(ROOT)).replace("\\", "/")
                                 for p in ROOT.rglob("*")
                                 if p.is_file() and ".git" not in p.parts]
        return self._tracked

    def basenames(self):
        if self._basenames is None:
            self._basenames = {Path(p).name for p in self.tracked()}
        return self._basenames

    def is_ignored(self, rel):
        """True if git ignores the path: it may legitimately be missing."""
        rel = rel.strip("/")
        if rel not in self._ignored:
            ignored = False
            for cand in (rel, rel + "/"):
                res = self.git("check-ignore", "-q", "--no-index", cand)
                if res is not None and res.returncode == 0:
                    ignored = True
                    break
            self._ignored[rel] = ignored
        return self._ignored[rel]

    def identifiers(self):
        """Every identifier appearing in the project, docs excluded."""
        if self._identifiers is None:
            ids = set(EXTERNAL_NAMES)
            word = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
            for rel in self.tracked():
                if rel.endswith(".md") or rel == "llms.txt":
                    continue
                path = ROOT / rel
                try:
                    if path.stat().st_size > 2_000_000:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                if b"\0" in data[:8192]:
                    continue
                ids.update(word.findall(data.decode("utf-8", "replace")))
            self._identifiers = ids
            self._sorted_ids = sorted(ids)
        return self._identifiers

    def known_name(self, name):
        """Defined somewhere, or prefix of a longer name (NAME_...)."""
        if name in self.identifiers():
            return True
        i = bisect.bisect_left(self._sorted_ids, name + "_")
        return i < len(self._sorted_ids) and self._sorted_ids[i].startswith(name + "_")

    def compose(self):
        """(services, container names) declared in the compose files."""
        if self._compose is None:
            services, containers = set(), set(EXTERNAL_CONTAINERS)
            projects = []
            for root in SEARCH_ROOTS:
                base = (ROOT / root).resolve()
                files = []
                for pat in ("docker-compose*.yml", "docker-compose*.yaml",
                            "compose*.yml", "compose*.yaml"):
                    files.extend(base.glob(pat))
                for f in files:
                    projects.append(f.parent.name.lower())
                    self._parse_compose(f, services, containers)
            self._compose = (services, containers, projects)
        return self._compose

    @staticmethod
    def _parse_compose(path, services, containers):
        in_services, indent = False, None
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            lead = len(line) - len(line.lstrip())
            if lead == 0:
                in_services = line.startswith("services:")
                indent = None
                continue
            m = re.match(r"\s*container_name:\s*[\"']?([\w.-]+)", line)
            if m:
                containers.add(m.group(1))
            if in_services:
                if indent is None:
                    indent = lead
                if lead == indent:
                    m = re.match(r"\s*[\"']?([\w.-]+)[\"']?\s*:", line)
                    if m:
                        services.add(m.group(1))

    def headings(self, path):
        """GitHub-style anchors of a Markdown document."""
        if path not in self._headings:
            slugs, seen, fence = set(), {}, None
            text = path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                m = FENCE_RE.match(line)
                if m:
                    if fence is None:
                        fence = m.group(1)[0]
                    elif m.group(1)[0] == fence:
                        fence = None
                    continue
                if fence:
                    continue
                for a in re.findall(r"<a\s+(?:name|id)=[\"']([^\"']+)", line):
                    slugs.add(a)
                h = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
                if h:
                    slug = self.slugify(h.group(1))
                    n = seen.get(slug, 0)
                    seen[slug] = n + 1
                    slugs.add(slug if n == 0 else "%s-%d" % (slug, n))
            self._headings[path] = slugs
        return self._headings[path]

    @staticmethod
    def slugify(text):
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"[^\w\- ]", "", text.strip().lower())
        return text.replace(" ", "-")

    # --- reporting ---------------------------------------------------------

    def error(self, doc, lineno, msg):
        self.errors.append("%s:%d: %s" % (doc, lineno, msg))

    def exists_or_ignored(self, rel):
        return (ROOT / rel).exists() or self.is_ignored(rel)

    # --- individual checks ---------------------------------------------------

    def check_link(self, doc, lineno, target):
        if target.lower().startswith(URL_SCHEMES) or "://" in target:
            return
        if target in IGNORE_REFS:
            return
        self.checked += 1
        path_part, _, anchor = target.partition("#")
        path_part = path_part.split("?", 1)[0]
        if not path_part:
            dest = ROOT / doc
        elif path_part.startswith("/"):
            dest = ROOT / path_part.lstrip("/")
        else:
            dest = (ROOT / doc).parent / path_part
        try:
            rel = str(dest.resolve().relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            return  # points outside the repository: not checkable here
        if not dest.exists():
            if not self.is_ignored(rel):
                self.error(doc, lineno, "broken link: %s" % target)
            return
        if anchor and dest.is_file() and dest.suffix == ".md":
            if anchor not in self.headings(dest.resolve()):
                self.error(doc, lineno, "missing anchor: #%s in %s" % (anchor, rel))

    def path_candidate(self, tok):
        """Repo-relative path a token refers to, or None if it is not one."""
        tok = tok.strip("\"'`").lstrip("(").rstrip(".,;)")
        if not tok or any(c in tok for c in PLACEHOLDER_CHARS):
            return None
        if "://" in tok or tok.startswith("~") or tok.startswith("-"):
            return None
        if ":" in tok:
            left = tok.split(":", 1)[0]
            if "/" not in left and not left.startswith("."):
                return None  # image:tag, host:port, service:path
            tok = left
        if tok.startswith("/"):
            for dp in DEPLOY_PATHS:
                dp = dp.rstrip("/")
                if tok == dp or tok.startswith(dp + "/"):
                    return tok[len(dp):].lstrip("/") or "."
            return None  # host or container path outside the repository
        if tok.startswith("../"):
            return None
        if tok.startswith("./"):
            return tok[2:] or "."
        if "/" in tok:
            return tok
        return None

    def check_path(self, doc, lineno, tok):
        if tok.strip("\"'`").rstrip(".,;:)") in IGNORE_REFS:
            return
        rel = self.path_candidate(tok)
        if rel is None or rel == ".":
            return
        first = rel.split("/", 1)[0]
        roots = [r for r in SEARCH_ROOTS
                 if (ROOT / r / first).exists() or first in (".", "..")]
        if not roots and not tok.lstrip("\"'`(").startswith(("./", "/")):
            return  # first segment unknown: MQTT topic, container path, ...
        roots = roots or ["."]
        self.checked += 1
        for r in roots:
            base = ROOT / r
            target = rel.rstrip("/")
            if any(c in target for c in "*?["):
                if list(base.glob(target)):
                    return
            elif (base / target).exists() or self.is_ignored(
                    str(Path(r) / target).replace("\\", "/")):
                return
        self.error(doc, lineno, "path not found: %s" % rel)

    def check_bare_file(self, doc, lineno, tok):
        tok = tok.strip("\"'`").rstrip(".,;:)")
        if tok in IGNORE_REFS or not (BARE_FILE_RE.match(tok) or tok in SPECIAL_FILES):
            return
        self.checked += 1
        if tok in self.basenames() or (ROOT / tok).exists() or self.is_ignored(tok):
            return
        self.error(doc, lineno, "file not found in the repository: %s" % tok)

    def check_env(self, doc, lineno, name):
        if name in IGNORE_REFS:
            return
        self.checked += 1
        if not self.known_name(name):
            self.error(doc, lineno, "name not defined in the project: %s" % name)

    def check_service(self, doc, lineno, name):
        services, containers, _ = self.compose()
        if name in IGNORE_REFS:
            return
        self.checked += 1
        if name not in services and name not in containers:
            self.error(doc, lineno, "unknown compose service: %s" % name)

    def check_container(self, doc, lineno, name):
        services, containers, projects = self.compose()
        if name in IGNORE_REFS:
            return
        self.checked += 1
        if name in containers or name in services:
            return
        for svc in services:
            for proj in projects:
                if re.fullmatch(r"%s[-_]%s[-_]\d+" % (re.escape(proj), re.escape(svc)), name):
                    return
        self.error(doc, lineno, "unknown container: %s" % name)

    def check_npm(self, doc, lineno, script):
        for r in SEARCH_ROOTS:
            pkg = ROOT / r / "package.json"
            if pkg.exists():
                self.checked += 1
                try:
                    scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
                except ValueError:
                    return
                if script not in scripts:
                    self.error(doc, lineno, "npm script not found: %s" % script)
                return

    def check_make(self, doc, lineno, target):
        mk = ROOT / "Makefile"
        if mk.exists():
            self.checked += 1
            text = mk.read_text(encoding="utf-8", errors="replace")
            if not re.search(r"^%s\s*:" % re.escape(target), text, re.M):
                self.error(doc, lineno, "make target not found: %s" % target)

    # --- command lines -----------------------------------------------------

    @staticmethod
    def tokenize(line):
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        try:
            return list(lex)
        except ValueError:
            return line.split()

    def check_command_line(self, doc, lineno, line, fenced):
        tokens = self.tokenize(line)
        segments, cur, skip_next = [], [], False
        for t in tokens:
            if skip_next:
                skip_next = False
                continue
            if t in SHELL_OPS:
                segments.append(cur)
                cur = []
            elif t in REDIRECTS or re.fullmatch(r"\d?[<>]{1,2}&?\d?", t):
                skip_next = True
            else:
                cur.append(t)
        segments.append(cur)
        foreign = False
        for seg in segments:
            foreign = self.check_segment(doc, lineno, seg, foreign, fenced)

    def check_segment(self, doc, lineno, seg, foreign, fenced):
        i = 0
        while i < len(seg) and (seg[i] in ("sudo", "time", "exec", "nohup", "env")
                                or ENV_ASSIGN_RE.match(seg[i])):
            m = ENV_ASSIGN_RE.match(seg[i])
            if m and i + 1 < len(seg):  # VAR=x command: an env var of the command
                self.check_env(doc, lineno, m.group(1))
            i += 1
        seg = seg[i:]
        if not seg:
            return foreign
        cmd = seg[0]
        if cmd == "cd" and len(seg) > 1:
            target = seg[1]
            if target.startswith("/"):
                return not any(target.rstrip("/") == dp.rstrip("/")
                               or target.startswith(dp.rstrip("/") + "/")
                               for dp in DEPLOY_PATHS)
            if target.startswith(".."):
                return True
            return foreign
        for prev, t in zip(seg, seg[1:]):
            m = ENV_ASSIGN_RE.match(t)
            if m and prev in ("-e", "--env", "--build-arg"):
                self.check_env(doc, lineno, m.group(1))
        if cmd == "docker" and len(seg) > 1 and seg[1] == "compose":
            self.check_compose(doc, lineno, seg[2:], foreign)
        elif cmd == "docker-compose":
            self.check_compose(doc, lineno, seg[1:], foreign)
        elif cmd == "docker" and len(seg) > 1 and seg[1] in DOCKER_CONTAINER_SUBCMDS:
            args = self.positional(seg[2:], DOCKER_CONTAINER_SUBCMDS[seg[1]])
            if args:
                self.check_container(doc, lineno, args[0])
        elif cmd == "docker" and len(seg) > 1 and seg[1] == "cp":
            for a in self.positional(seg[2:], set()):
                m = re.match(r"^([\w.-]+):/", a)
                if m:
                    self.check_container(doc, lineno, m.group(1))
        elif cmd in INTERPRETERS and len(seg) > 1 and not foreign:
            script = next((a for a in seg[1:] if not a.startswith("-")), None)
            if script and "/" not in script:
                self.check_bare_file(doc, lineno, script)
        elif cmd == "npm" and len(seg) > 2 and seg[1] in ("run", "run-script"):
            self.check_npm(doc, lineno, seg[2])
        elif cmd == "make" and len(seg) > 1 and not seg[1].startswith("-"):
            self.check_make(doc, lineno, seg[1])
        if not foreign:
            for t in seg:
                self.check_path(doc, lineno, t)
        return foreign

    @staticmethod
    def positional(args, value_opts):
        out, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a.startswith("-"):
                skip = a in value_opts
            else:
                out.append(a)
        return out

    def check_compose(self, doc, lineno, args, foreign):
        i = 0
        while i < len(args) and args[i].startswith("-"):
            opt = args[i]
            if opt in ("-f", "--file", "--project-directory") and i + 1 < len(args):
                val = args[i + 1]
                if val.startswith(("/", "../")) and self.path_candidate(val) is None:
                    foreign = True
            i += 2 if opt in COMPOSE_GLOBAL_OPTS else 1
        if foreign or i >= len(args) or args[i] not in COMPOSE_SUBCMDS:
            return
        sub = args[i]
        names = self.positional(args[i + 1:], COMPOSE_SUBCMDS[sub])
        if sub in ("exec", "run", "port"):
            names = names[:1]
        for n in names:
            if not any(c in n for c in PLACEHOLDER_CHARS):
                self.check_service(doc, lineno, n)

    # --- a whole document ----------------------------------------------------

    def check_tree_line(self, doc, lineno, line, state):
        m = TREE_RE.match(line)
        if not m:
            return
        depth = len(m.group("indent")) // 4
        name = m.group("name").rstrip("/")
        stack = state["stack"][:depth]
        state["stack"] = stack + [name]
        if any(c in name for c in PLACEHOLDER_CHARS + ("*", "[", "(")):
            return
        if not re.search(r"\w", name):
            return  # a box-drawing diagram stroke (└────┘), not an entry
        rel = "/".join([state["prefix"]] + stack + [name]).strip("/")
        self.checked += 1
        if not self.exists_or_ignored(rel):
            self.error(doc, lineno, "directory tree entry not found: %s" % rel)

    def tree_prefix(self, first_line):
        name = first_line.strip().split()[0].rstrip("/") if first_line.strip() else ""
        if name and (ROOT / name).is_dir() and name != ROOT.name:
            return name
        return ""

    @staticmethod
    def markers(line):
        return set(MARK_RE.findall(CODE_SPAN_RE.sub("", line)))

    def check_doc(self, rel):
        path = ROOT / rel
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        parsed, fence, fence_lang, block, ignoring = [], None, "", [], False
        for lineno, line in enumerate(lines, 1):
            m = FENCE_RE.match(line)
            if fence is None and m:
                fence, fence_lang, block = m.group(1), m.group(2).lower(), []
                continue
            if fence is not None:
                closing = line.strip()
                if closing and set(closing) == {fence[0]} and len(closing) >= len(fence):
                    if not ignoring:
                        parsed.append(("block", block, fence_lang))
                    fence = None
                else:
                    block.append((lineno, line, ignoring or "ignore" in self.markers(line)))
                continue
            marks = self.markers(line)
            if "ignore-file" in marks:
                return
            if "ignore-start" in marks and "ignore-end" not in marks:
                ignoring = True
                continue
            if "ignore-end" in marks and "ignore-start" not in marks:
                ignoring = False
                continue
            if ignoring or marks:
                continue
            parsed.append(("prose", lineno, line))
        if self.verbose:
            print("  %s" % rel)
        for item in parsed:
            if item[0] == "block":
                self.check_block(rel, item[1], item[2])
            else:
                self.check_prose_line(rel, item[1], item[2])

    def check_prose_line(self, doc, lineno, line):
        for m in LINK_RE.finditer(re.sub(CODE_SPAN_RE, "", line)):
            self.check_link(doc, lineno, m.group(1))
        m = REFLINK_RE.match(line)
        if m:
            self.check_link(doc, lineno, m.group(1))
        for m in CODE_SPAN_RE.finditer(line):
            span = m.group(2).strip()
            if not span:
                continue
            if re.search(r"\s", span):
                self.check_command_line(doc, lineno, span, fenced=False)
                continue
            env = ENV_TOKEN_RE.match(span) or ENV_ASSIGN_RE.match(span)
            if env:
                self.check_env(doc, lineno, env.group(1))
            elif "/" in span:
                self.check_path(doc, lineno, span)
            else:
                self.check_bare_file(doc, lineno, span)

    def check_block(self, doc, block, lang):
        if any(TREE_RE.match(l) for _, l, _ in block):
            first = next((l for _, l, ign in block if l.strip()), "")
            state = {"stack": [], "prefix": self.tree_prefix(first)}
            for lineno, line, ign in block:
                if not ign:
                    self.check_tree_line(doc, lineno, line, state)
                elif TREE_RE.match(line):
                    m = TREE_RE.match(line)
                    depth = len(m.group("indent")) // 4
                    state["stack"] = state["stack"][:depth] + [m.group("name").rstrip("/")]
            return
        env_block = lang in ("", "env", "dotenv", "ini", "properties", "text", "conf")
        shell_block = lang in ("", "bash", "sh", "shell", "console", "zsh")
        local_vars = set()
        for lineno, line, ign in block:
            if ign:
                continue
            stripped = line.strip()
            if lang in ("console",) and stripped.startswith("$ "):
                stripped = stripped[2:]
            m = re.match(r"^(?:export\s+)?(%s)=" % ENV_NAME, stripped)
            if m:
                if env_block and not shell_block:
                    self.check_env(doc, lineno, m.group(1))
                elif lang == "" and " " not in stripped.split("=", 1)[1].strip():
                    self.check_env(doc, lineno, m.group(1))
                local_vars.add(m.group(1))
            for ref in ENV_REF_RE.findall(stripped):
                if ref not in local_vars:
                    self.check_env(doc, lineno, ref)
            if shell_block and stripped and not stripped.startswith("#"):
                self.check_command_line(doc, lineno, stripped, fenced=True)

    def check_llms(self, docs):
        llms = ROOT / "llms.txt"
        if not (LLMS_MUST_LIST_DOCS and llms.exists()):
            return
        text = llms.read_text(encoding="utf-8", errors="replace")
        linked = set()
        for m in LINK_RE.finditer(text):
            target = m.group(1).split("#", 1)[0]
            linked.add(target[2:] if target.startswith("./") else target)
        for rel in docs:
            if rel.startswith("docs/") and rel not in linked:
                self.error("llms.txt", 1, "reference document not listed: %s" % rel)

    def run(self):
        docs = []
        for pat in DOCS:
            if not any(c in pat for c in "*?["):
                if not (ROOT / pat).exists():
                    self.error(pat, 0, "document missing")
                    continue
                matches = [pat]
            else:
                matches = sorted(str(p.relative_to(ROOT)).replace("\\", "/")
                                 for p in ROOT.glob(pat) if p.is_file())
            for rel in matches:
                if rel in docs or any(fnmatch.fnmatch(rel, ex) for ex in DOCS_EXCLUDE):
                    continue
                docs.append(rel)
        if self.verbose:
            print("doc-check: documents checked")
        for rel in docs:
            self.check_doc(rel)
        self.check_llms(docs)
        for e in self.errors:
            print(e)
        if self.errors:
            print("doc-check: %d broken reference(s) - fix the document, do not "
                  "silence the check" % len(self.errors))
            return 1
        print("doc-check: OK (%d documents, %d references checked)" % (len(docs), self.checked))
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    return Checker(verbose=args.verbose).run()


if __name__ == "__main__":
    sys.exit(main())
