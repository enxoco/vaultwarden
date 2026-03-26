#!/usr/bin/env python3
"""
Detects the appropriate linter for a repository and updates
.github/workflows/ci.yaml to:
  - Replace the Ruff linting steps with the appropriate linter.
  - Wrap OpenGrep and Gitleaks installs with actions/cache (idempotent).
  - Consolidate all tool PATH additions into a single step.

Detection priority:
  1. Explicit linter config files (.eslintrc, .golangci.yml, Cargo.toml, etc.)
  2. pyproject.toml with [tool.*] sections
  3. package.json with an eslint devDependency
  4. go.mod presence
  5. Source file extensions (*.py, *.go, *.sh)
     JS/TS requires an explicit config or dep — presence of .ts files alone is
     not enough to conclude ESLint is set up.
  If nothing is found the Ruff steps are removed entirely.

Usage:
    python3 .github/scripts/detect-linter.py [--dry-run] [repo-root]

    --dry-run   Print a unified diff instead of writing the file.
    repo-root   Path to the repository root (defaults to the nearest .git parent).
"""

import difflib
import json
import re
import sys
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".tox"}

# Standard step indentation used in this CI template
_ITEM = "      "    # 6 spaces  — "- name:" list-item prefix
_CONT = "        "  # 8 spaces  — step body (uses/run/with)
_WITH = "          " # 10 spaces — inside "with:"

# Pinned versions for static (always-present) tools
OPENGREP_VERSION  = "v1.15.1"
GITLEAKS_VERSION  = "v8.30.0"
GOLANGCI_VERSION  = "v2.1.6"

LINTER_DISPLAY = {
    "ruff":          "Ruff",
    "eslint":        "ESLint",
    "golangci-lint": "golangci-lint",
    "clippy":        "Clippy (Rust)",
    "shellcheck":    "ShellCheck",
    "pylint":        "Pylint",
    "flake8":        "Flake8",
}

# ── File helpers ───────────────────────────────────────────────────────────────

def find_files(root: Path, *patterns: str) -> list[Path]:
    """Glob for source files, skipping vendor/hidden/build directories."""
    results: list[Path] = []
    for pattern in patterns:
        for p in root.rglob(pattern):
            rel_parts = p.relative_to(root).parts
            # Skip files inside hidden directories (e.g. .github/scripts) or vendor dirs
            if any(
                part in SKIP_DIRS or part.startswith(".")
                for part in rel_parts[:-1]
            ):
                continue
            results.append(p)
    return results


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

# ── Detection ─────────────────────────────────────────────────────────────────

def _eslint_ctx(root: Path) -> dict:
    """Return {'work_dir': relative_path_or_None} for the nearest package.json."""
    for pkg in (root / "package.json", root / "tests" / "package.json"):
        if pkg.exists():
            rel = str(pkg.parent.relative_to(root))
            return {"work_dir": None if rel == "." else rel}
    return {}


def detect_linter(root: Path) -> tuple[str | None, dict]:
    """
    Returns (linter_id, context_dict).
    linter_id is one of the LINTER_DISPLAY keys, or None if no linter found.
    context_dict carries linter-specific data (e.g. work_dir, files).
    """

    # 1. Explicit linter config files — strongest signal
    if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        return "ruff", {}

    eslint_configs = (
        ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
        ".eslintrc.yaml", ".eslintrc.yml",
        "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
    )
    if any((root / name).exists() for name in eslint_configs):
        return "eslint", _eslint_ctx(root)

    golangci_configs = (".golangci.yml", ".golangci.yaml", ".golangci.json", ".golangci.toml")
    if any((root / name).exists() for name in golangci_configs):
        return "golangci-lint", {}

    if (root / "Cargo.toml").exists():
        return "clippy", {}

    # 2. pyproject.toml tool sections
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text()
        for section, linter in (
            ("[tool.ruff]",   "ruff"),
            ("[tool.pylint",  "pylint"),
            ("[tool.flake8]", "flake8"),
        ):
            if section in text:
                return linter, {}

    # 3. package.json with an explicit eslint dependency
    for pkg_path in (root / "package.json", root / "tests" / "package.json"):
        if pkg_path.exists():
            pkg = read_json(pkg_path)
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "eslint" in all_deps:
                return "eslint", _eslint_ctx(root)

    # 4. Presence of a go.mod
    if (root / "go.mod").exists():
        return "golangci-lint", {}

    # 5. Source file extensions — only for ecosystems where a linter is effectively
    #    universal (Python/Go/shell). JS/TS requires an explicit signal above.
    if find_files(root, "*.py"):
        return "ruff", {}

    if find_files(root, "*.go"):
        return "golangci-lint", {}

    sh_files = find_files(root, "*.sh")
    if sh_files:
        return "shellcheck", {"files": [str(f.relative_to(root)) for f in sh_files]}

    return None, {}

# ── Step builders ─────────────────────────────────────────────────────────────

def _cache_step(name: str, cache_id: str, path: str, key: str) -> str:
    """Render an actions/cache restore step."""
    return "\n".join([
        f"{_ITEM}- name: {name}",
        f"{_CONT}id: {cache_id}",
        f"{_CONT}uses: actions/cache@v4",
        f"{_CONT}with:",
        f"{_WITH}path: {path}",
        f"{_WITH}key: {key}",
    ]) + "\n"


def _witness_step(
    name: str,
    step_id: str,
    command: str,
    outfile: str,
    witness_action: str,
) -> str:
    """Render a witness-run-action step block."""
    return "\n".join([
        f"{_ITEM}- name: {name}",
        f"{_CONT}uses: {witness_action}",
        f"{_CONT}with:",
        f"{_WITH}step: {step_id}",
        f"{_WITH}enable-archivista: false",
        f"{_WITH}enable-sigstore: true",
        f"{_WITH}command: {command}",
        f"{_WITH}outfile: {outfile}",
    ]) + "\n"


def build_steps(
    linter: str | None, ctx: dict, witness_action: str
) -> tuple[str, str, str | None]:
    """
    Returns (install_yaml, witness_yaml, outfile_name).
    install_yaml may span multiple steps (e.g. cache + conditional install).
    All three are empty strings / None when linter is None (steps removed).
    """
    if linter is None:
        return "", "", None

    work_dir = ctx.get("work_dir")

    if linter == "ruff":
        install = f"{_ITEM}- name: Install Ruff\n{_CONT}run: pip install ruff\n"
        witness = _witness_step(
            "Witness Run Ruff", "ruff", "ruff check .", "ruff-witness.json", witness_action
        )
        return install, witness, "ruff-witness.json"

    if linter in ("pylint", "flake8"):
        display = LINTER_DISPLAY[linter]
        install = f"{_ITEM}- name: Install {display}\n{_CONT}run: pip install {linter}\n"
        cmd = "flake8 ." if linter == "flake8" else "pylint $(git ls-files '*.py')"
        outfile = f"{linter}-witness.json"
        witness = _witness_step(f"Witness Run {display}", linter, cmd, outfile, witness_action)
        return install, witness, outfile

    if linter == "eslint":
        if work_dir:
            install = (
                f"{_ITEM}- name: Install ESLint\n"
                f"{_CONT}working-directory: {work_dir}\n"
                f"{_CONT}run: npm ci\n"
            )
            cmd = f"cd {work_dir} && npx eslint ."
        else:
            install = f"{_ITEM}- name: Install ESLint\n{_CONT}run: npm ci\n"
            cmd = "npx eslint ."
        witness = _witness_step(
            "Witness Run ESLint", "eslint", cmd, "eslint-witness.json", witness_action
        )
        return install, witness, "eslint-witness.json"

    if linter == "golangci-lint":
        # Install to ~/.local/bin so it lands in the shared cached directory
        install = (
            _cache_step(
                "Cache golangci-lint", "cache-golangci-lint",
                "~/.local/bin/golangci-lint",
                f"golangci-lint-${{{{ runner.os }}}}-{GOLANGCI_VERSION}",
            )
            + "\n"
            + f"{_ITEM}- name: Install golangci-lint\n"
            + f"{_CONT}if: steps.cache-golangci-lint.outputs.cache-hit != 'true'\n"
            + f"{_CONT}run: |\n"
            + f"{_CONT}  curl -sSfL https://raw.githubusercontent.com/golangci/golangci-lint/HEAD/install.sh"
            + f" | sh -s -- -b $HOME/.local/bin\n"
        )
        witness = _witness_step(
            "Witness Run golangci-lint", "golangci-lint", "golangci-lint run ./...",
            "golangci-lint-witness.json", witness_action,
        )
        return install, witness, "golangci-lint-witness.json"

    if linter == "clippy":
        install = (
            f"{_ITEM}- name: Install Rust toolchain\n"
            f"{_CONT}uses: dtolnay/rust-toolchain@stable\n"
        )
        witness = _witness_step(
            "Witness Run Clippy", "clippy", "cargo clippy -- -D warnings",
            "clippy-witness.json", witness_action,
        )
        return install, witness, "clippy-witness.json"

    if linter == "shellcheck":
        files = " ".join(ctx.get("files", ["*.sh"]))
        # Install via apt then copy to ~/.local/bin so the binary can be cached
        install = (
            _cache_step(
                "Cache ShellCheck", "cache-shellcheck",
                "~/.local/bin/shellcheck",
                "shellcheck-${{ runner.os }}-stable",
            )
            + "\n"
            + f"{_ITEM}- name: Install ShellCheck\n"
            + f"{_CONT}if: steps.cache-shellcheck.outputs.cache-hit != 'true'\n"
            + f"{_CONT}run: |\n"
            + f"{_CONT}  sudo apt-get install -y shellcheck\n"
            + f"{_CONT}  mkdir -p $HOME/.local/bin\n"
            + f"{_CONT}  cp $(which shellcheck) $HOME/.local/bin/shellcheck\n"
        )
        witness = _witness_step(
            "Witness Run ShellCheck", "shellcheck", f"shellcheck {files}",
            "shellcheck-witness.json", witness_action,
        )
        return install, witness, "shellcheck-witness.json"

    raise ValueError(f"Unknown linter: {linter!r}")


def build_opengrep_steps() -> str:
    """
    Cache + conditional install for OpenGrep.
    Includes the existing '# Only useful if source is available' comment so that
    replace_step (which swallows the preceding comment) ends up with the same text.
    """
    return (
        f"{_ITEM}# Only useful if source is available\n"
        + _cache_step(
            "Cache OpenGrep", "cache-opengrep",
            "~/.opengrep",
            f"opengrep-${{{{ runner.os }}}}-{OPENGREP_VERSION}",
        )
        + "\n"
        + f"{_ITEM}- name: Install OpenGrep\n"
        + f"{_CONT}if: steps.cache-opengrep.outputs.cache-hit != 'true'\n"
        + f"{_CONT}run: "
        + f"curl -sSL https://raw.githubusercontent.com/opengrep/opengrep/{OPENGREP_VERSION}/install.sh | bash\n"
    )


def build_gitleaks_steps() -> str:
    """
    Cache + conditional install for Gitleaks, followed by the PATH consolidation
    step for all cached tool directories.
    Includes the existing '# Should always run' comment for the same reason as above.
    """
    ver = GITLEAKS_VERSION.lstrip("v")
    return (
        f"{_ITEM}# Should always run\n"
        + _cache_step(
            "Cache Gitleaks", "cache-gitleaks",
            "~/.local/bin/gitleaks",
            f"gitleaks-${{{{ runner.os }}}}-{GITLEAKS_VERSION}",
        )
        + "\n"
        + f"{_ITEM}- name: Install Gitleaks\n"
        + f"{_CONT}if: steps.cache-gitleaks.outputs.cache-hit != 'true'\n"
        + f"{_CONT}run: |\n"
        + f"{_CONT}  curl -sSL https://github.com/gitleaks/gitleaks/releases/download/{GITLEAKS_VERSION}/gitleaks_{ver}_linux_x64.tar.gz -o gitleaks.tar.gz\n"
        + f"{_CONT}  tar -zxf gitleaks.tar.gz\n"
        + f"{_CONT}  mkdir -p $HOME/.local/bin\n"
        + f"{_CONT}  mv gitleaks $HOME/.local/bin\n"
        + f"{_CONT}  rm gitleaks.tar.gz\n"
        + "\n"
        + f"{_ITEM}- name: Add tool directories to PATH\n"
        + f"{_CONT}run: |\n"
        + f"{_CONT}  mkdir -p $HOME/.local/bin\n"
        + f'{_CONT}  echo "$HOME/.local/bin" >> "$GITHUB_PATH"\n'
        + f'{_CONT}  echo "$HOME/.opengrep/cli/latest" >> "$GITHUB_PATH"\n'
    )

# ── YAML text manipulation ────────────────────────────────────────────────────

def extract_witness_action(content: str) -> str:
    """Pull the pinned witness-run-action ref from the file (including inline comment)."""
    m = re.search(r"(testifysec/witness-run-action@\S+(?:\s+#[^\n]*)?)", content)
    return (
        m.group(1).rstrip()
        if m
        else "testifysec/witness-run-action@7aa15e327829f1f2a523365c564c948d5dde69dd # v0.3.3"
    )


def replace_step(content: str, step_name: str, new_yaml: str) -> tuple[str, bool]:
    """
    Find a step by name, include its immediately-preceding comment in the
    replacement region, and replace everything up to (not including) the
    trailing blank line with new_yaml.

    When new_yaml is empty the step and its comment are deleted.
    Returns (updated_content, was_changed).
    """
    pattern = re.compile(
        r"^([ \t]*)- name: " + re.escape(step_name) + r"[ \t]*$", re.MULTILINE
    )
    m = pattern.search(content)
    if not m:
        return content, False

    step_indent = m.group(1)
    start = m.start()

    # Walk back past any comment line at the same indent immediately before the step
    before = content[:start]
    stripped_before = before.rstrip("\n")
    last_nl = stripped_before.rfind("\n")
    preceding_line = stripped_before[last_nl + 1:] if last_nl >= 0 else stripped_before
    if preceding_line.startswith(step_indent) and preceding_line.lstrip().startswith("#"):
        start = (last_nl + 1) if last_nl >= 0 else 0

    # Advance past the step body: lines that are more-indented than step_indent.
    # m.end() lands on the \n that ends the "- name:" line (since $ in MULTILINE
    # matches before \n), so +1 to start scanning from the next line.
    pos = m.end() + 1
    while pos < len(content):
        nl = content.find("\n", pos)
        line = content[pos:nl] if nl != -1 else content[pos:]
        # Blank line or same/lesser-indent line marks the end of this step
        if not line or (
            not line.startswith(step_indent + " ")
            and not line.startswith(step_indent + "\t")
        ):
            break
        pos = (nl + 1) if nl != -1 else len(content)
    end = pos

    result = content[:start] + new_yaml + content[end:]

    # When removing a step (new_yaml == ""), collapse any run of 3+ newlines
    # to 2 (one blank line) to avoid leaving a gaping hole.
    if not new_yaml:
        result = re.sub(r"\n{3,}", "\n\n", result)

    return result, result != content


def ensure_linter_cached(
    content: str, linter: str | None, ctx: dict, witness_action: str
) -> tuple[str, bool]:
    """
    For linters that download a binary (shellcheck, golangci-lint), check whether
    the install step is already present but lacks a cache step, and wrap it if so.
    This handles repos where detect-linter.py was run before caching was added.
    """
    # Map linter → (cache step marker, existing install step name)
    cacheable = {
        "shellcheck":    ("Cache ShellCheck",    "Install ShellCheck"),
        "golangci-lint": ("Cache golangci-lint",  "Install golangci-lint"),
    }
    if linter not in cacheable:
        return content, False

    cache_marker, install_step = cacheable[linter]
    if cache_marker in content:
        return content, False  # Cache already present

    install_yaml, _, _ = build_steps(linter, ctx, witness_action)
    return replace_step(content, install_step, install_yaml)


def add_tool_caching(content: str) -> tuple[str, bool]:
    """
    Idempotently wrap OpenGrep and Gitleaks installs with actions/cache and
    consolidate PATH additions into a single step after Gitleaks.
    Skips each tool if a cache step for it already exists.
    """
    changed = False

    if "- name: Cache OpenGrep" not in content:
        content, c = replace_step(content, "Install OpenGrep", build_opengrep_steps())
        changed = changed or c

    if "- name: Cache Gitleaks" not in content:
        content, c = replace_step(content, "Install Gitleaks", build_gitleaks_steps())
        changed = changed or c

    return content, changed


def update_attestations(content: str, old_outfile: str, new_outfile: str | None) -> str:
    """Swap or remove the linter witness outfile in the --attestations flag."""
    if old_outfile == new_outfile:
        return content
    if new_outfile:
        return content.replace(old_outfile, new_outfile)
    # Remove the outfile token and its neighbouring comma
    content = re.sub(r",\s*" + re.escape(old_outfile), "", content)
    content = re.sub(re.escape(old_outfile) + r"\s*,\s*", "", content)
    return content


def update_overview_comment(content: str, display_name: str | None) -> str:
    if display_name:
        return re.sub(r"(# Lint:)\s+\S+", rf"\1 {display_name}", content)
    return re.sub(r"# Lint:[^\n]+\n", "", content)

# ── Main ──────────────────────────────────────────────────────────────────────

def find_repo_root(start: Path) -> Path:
    path = start.resolve()
    while path != path.parent:
        if (path / ".git").exists():
            return path
        path = path.parent
    return start.resolve()


def current_linter_outfile(content: str) -> str:
    """Guess the outfile currently used by the linter witness step."""
    m = re.search(r"--attestations\s+([^\\\n]+)", content)
    if m:
        for token in m.group(1).split(","):
            token = token.strip()
            if (
                token.endswith("-witness.json")
                and token not in ("zarf-create-witness.json", "gitleaks-witness.json")
            ):
                return token
    return "ruff-witness.json"


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    pos_args = [a for a in args if not a.startswith("-")]

    repo_root = Path(pos_args[0]).resolve() if pos_args else find_repo_root(Path.cwd())
    ci_yaml = repo_root / ".github" / "workflows" / "ci.yaml"

    if not ci_yaml.exists():
        print(f"ERROR: {ci_yaml} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {repo_root} ...")
    linter, ctx = detect_linter(repo_root)

    display = LINTER_DISPLAY.get(linter, linter) if linter else None
    if linter:
        print(f"Detected linter : {display}")
        if ctx:
            print(f"Context         : {ctx}")
    else:
        print("No linter detected — Ruff steps will be removed.")

    content = original = ci_yaml.read_text()
    witness_action = extract_witness_action(content)
    old_outfile = current_linter_outfile(content)

    install_yaml, witness_yaml, new_outfile = build_steps(linter, ctx, witness_action)

    # Replace linter-specific steps
    content, c1 = replace_step(content, "Install Ruff", install_yaml)
    content, c2 = replace_step(content, "Witness Run Ruff", witness_yaml)
    content = update_attestations(content, old_outfile, new_outfile)
    content = update_overview_comment(content, display)

    # If the linter was already installed from a prior run but without a cache step, add it
    content, c_linter_cache = ensure_linter_cached(content, linter, ctx, witness_action)

    # Add caching for static tools (OpenGrep, Gitleaks) and consolidate PATH
    content, c3 = add_tool_caching(content)
    c3 = c3 or c_linter_cache

    if content == original:
        print("No changes needed.")
        return

    if dry_run:
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=str(ci_yaml),
            tofile=str(ci_yaml) + " (updated)",
        )
        sys.stdout.writelines(diff)
        return

    ci_yaml.write_text(content)
    rel = ci_yaml.relative_to(repo_root)
    print(f"Updated {rel}")
    if c1:
        verb = "Replaced" if linter else "Removed"
        suffix = f" → {display} install" if linter else ""
        print(f"  ✓ {verb} 'Install Ruff' step{suffix}")
    if c2:
        verb = "Replaced" if linter else "Removed"
        suffix = f" → {display} witness step" if linter else ""
        print(f"  ✓ {verb} 'Witness Run Ruff' step{suffix}")
    if old_outfile != new_outfile:
        if new_outfile:
            print(f"  ✓ Updated attestations: {old_outfile} → {new_outfile}")
        else:
            print(f"  ✓ Removed {old_outfile} from attestations")
    if c3:
        print("  ✓ Added caching for OpenGrep and Gitleaks; consolidated PATH step")


if __name__ == "__main__":
    main()
