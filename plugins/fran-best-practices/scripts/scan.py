#!/usr/bin/env python3
"""Deterministic detector for the findings this ruleset blocks pull requests over.

Parses Python with ``ast`` and ``tokenize`` rather than grepping, so a finding points at a
real construct instead of a substring. Every check maps to a rule in
``skills/fran-best-practices/references/rules.md``; the semantic checks that need judgement
(R2 layering, R10 comment quality, R11 spec examples) are left to the model and only
partially pre-filtered here.

Usage:
    scan.py [PATH ...]                 # scan files or directories
    scan.py --diff origin/develop      # scan only files changed against a ref
    scan.py --json                     # machine-readable output
    scan.py --rules R1,R6              # restrict to some rules
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

BLOCK, ASK = "BLOCK", "ASK"

# Spanish words that effectively never appear in an English code comment. A comment needs
# MIN_SPANISH_HITS distinct matches to be reported, which keeps loanwords, acronyms and
# domain terms borrowed from a spec from tripping the check on their own.
# Grammatical words only, deliberately: function words are what reliably signal the language,
# while domain nouns would tie the detector to one project's vocabulary and misfire on the
# loanwords every integration picks up.
SPANISH_WORDS = frozenset("""
que para con del los las una por este esta estos estas cuando porque segun según asi así
pero mas más tambien también hay esta está estan están desde hasta sobre entre aunque
mientras siempre nunca cada todos todas otro otra otros otras mismo misma donde cuales
muy bien aqui aquí ahora luego antes despues después ademas además tiene tienen hace hacen
puede pueden debe deben son fue fueron sera será seria sería envia envía devuelve aporta
cualquier ningun ningún alguna algun algún vez veces segundo primera nuestro nuestra
sino solo sólo tras hacia mediante durante segun cuyo cuya cuyos cuyas ello ellos ellas
""".split())
MIN_SPANISH_HITS = 2

COMPAT_PATTERNS = re.compile(
    r"backward[- ]?compat|backwards[- ]?compat|retrocompat|for compatibility|"
    r"compatibilidad|legacy support|deprecated|kept for compat|for the old |old format|"
    r"para compatibilidad",
    re.IGNORECASE,
)

# Layer conventions. These defaults match a conventional FastAPI service layout; any repo can
# override them by dropping a `.fran-scan.json` at its root:
#
#     {"layers": {"schemas": ["src/schemas"], "services": ["src/services"],
#                 "routes": ["src/api"], "clients": ["src/clients"], "domain": ["src/domain"]}}
#
# Rules that are layer-independent (R6 language, R9 backward compat, R3 exceptions) apply
# everywhere regardless, so the plugin is useful in a new repo before anyone configures it.
DEFAULT_LAYERS = {
    "domain": ["app/domain"],
    "services": ["app/services"],
    "schemas": ["app/api/schemas"],
    "routes": ["app/api/routes"],
    "clients": ["app/clients"],
}
LAYER_CONFIG_FILE = ".fran-scan.json"

# Populated by load_layers(); MODEL_REQUIRED_DIRS is every layer whose return types must be
# modelled (R1). Raw upstream bodies are only allowed as an *input* type, which is why we look
# at return annotations and field annotations.
MODEL_REQUIRED_DIRS: tuple[str, ...] = ()
SCHEMA_DIRS: tuple[str, ...] = ()
SERVICE_DIRS: tuple[str, ...] = ()
ROUTE_DIRS: tuple[str, ...] = ()
CLIENT_DIRS: tuple[str, ...] = ()


def load_layers(root: Path) -> str:
    """Apply the repo's layer conventions. Returns a one-line description of what was used."""
    global MODEL_REQUIRED_DIRS, SCHEMA_DIRS, SERVICE_DIRS, ROUTE_DIRS, CLIENT_DIRS
    layers = dict(DEFAULT_LAYERS)
    origin = "built-in defaults"
    config_path = root / LAYER_CONFIG_FILE
    if config_path.is_file():
        try:
            configured = (json.loads(config_path.read_text(encoding="utf-8")) or {}).get("layers")
            if isinstance(configured, dict):
                for key, value in configured.items():
                    if key in layers and isinstance(value, list):
                        layers[key] = [str(v) for v in value]
                origin = LAYER_CONFIG_FILE
        except (json.JSONDecodeError, OSError) as exc:
            print(f"scan.py: ignoring {config_path}: {exc}", file=sys.stderr)
    SCHEMA_DIRS = tuple(layers["schemas"])
    SERVICE_DIRS = tuple(layers["services"])
    ROUTE_DIRS = tuple(layers["routes"])
    CLIENT_DIRS = tuple(layers["clients"])
    MODEL_REQUIRED_DIRS = tuple(layers["domain"] + layers["services"] + layers["schemas"] + layers["clients"])
    return origin

PYDANTIC_BASES = {"BaseModel", "APIResponseModel", "WorkflowResponse", "ToolRequest", "RootModel"}

# Built-in exceptions a service may legitimately raise: they signal a programming error, not an
# upstream failure to be translated into a result code. R4's strict lookup raises ValueError on
# purpose, so flagging these would contradict the ruleset.
BUILTIN_ERRORS = frozenset({
    "ValueError", "TypeError", "KeyError", "IndexError", "AttributeError", "RuntimeError",
    "NotImplementedError", "AssertionError", "StopIteration", "ArithmeticError",
    "ZeroDivisionError", "OverflowError", "LookupError", "OSError", "MemoryError",
    "RecursionError", "UnicodeDecodeError", "UnicodeEncodeError",
})

SKIP_DIR_NAMES = {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", "node_modules"}

# Rules that do not apply to test code. Repeated assertion keys and throwaway fixtures are
# not magic values, and a test helper returning a dict is not a contract. Language (R6) and
# backward-compat debt (R9) still apply — he reads those everywhere.
TEST_PATH_MARKERS = ("tests/", "test_", "_test.py", "conftest.py")
RULES_SKIPPED_IN_TESTS = frozenset({"R1", "R4", "R5", "R8", "R11", "R12"})


def is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(marker in normalized for marker in TEST_PATH_MARKERS)

# Literals too common to be worth naming.
TRIVIAL_LITERALS = frozenset({"", " ", "-", "/", ".", ",", ":", "|", "0", "1", "OK", "true", "false",
                              "GET", "POST", "utf-8", "application/json", "%s", "\n"})
MIN_LITERAL_REPEATS = 3
MIN_LITERAL_LEN = 3

# Numeric literals that are never "magic".
BENIGN_NUMBERS = frozenset({0, 1, 2, -1, 100, 200})

# Keyword arguments whose numeric value documents itself (validation constraints, HTTP codes
# handled by R2, formatting knobs). Flagging these is noise.
CONSTRAINT_KWARGS = frozenset({
    "max_length", "min_length", "max_digits", "decimal_places", "multiple_of",
    "ge", "le", "gt", "lt", "min_items", "max_items", "indent", "maxsplit",
    "status_code", "version", "port", "width", "precision",
})


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    message: str
    snippet: str = ""
    evidence: str = ""


@dataclass
class FileReport:
    path: str
    findings: list[Finding] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def in_dirs(path: str, dirs: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return any(d in normalized for d in dirs)


def annotation_text(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return ""


def is_loose_dict(text: str) -> bool:
    """True for annotations that pass raw JSON around instead of a model."""
    compact = text.replace(" ", "")
    return (
        "dict[str,Any]" in compact
        or "dict[str,object]" in compact
        or compact in {"dict", "Dict", "Any", "list[dict]", "list[Any]"}
        or re.search(r"list\[dict\[", compact) is not None
    )


def spanish_hits(text: str) -> list[str]:
    words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", text.lower())
    return sorted({w for w in words if w in SPANISH_WORDS})


def class_bases(node: ast.ClassDef) -> set[str]:
    names = set()
    for base in node.bases:
        text = annotation_text(base)
        names.add(text.split("[")[0].split(".")[-1])
    return names


def looks_pydantic(node: ast.ClassDef) -> bool:
    bases = class_bases(node)
    if bases & PYDANTIC_BASES:
        return True
    # Heuristic: a class whose body is only annotated assignments is a model in these repos.
    annotated = [s for s in node.body if isinstance(s, ast.AnnAssign)]
    body = [s for s in node.body if not isinstance(s, (ast.Expr, ast.Pass))]
    return bool(annotated) and len(annotated) == len(body) and len(annotated) >= 2


def model_fields(node: ast.ClassDef) -> list[tuple[str, str, bool, int]]:
    """Return (name, annotation, is_optional_with_default, lineno) for each field."""
    out = []
    for stmt in node.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        name = stmt.target.id
        if name.startswith("_") or name == "model_config":
            continue
        ann = annotation_text(stmt.annotation)
        optional = "None" in ann and stmt.value is not None
        out.append((name, ann, optional, stmt.lineno))
    return out


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


def check_r1_models(tree: ast.Module, path: str, lines: list[str], out: list[Finding]) -> None:
    if not in_dirs(path, MODEL_REQUIRED_DIRS):
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            returns = annotation_text(node.returns)
            if returns and is_loose_dict(returns) and not in_dirs(path, CLIENT_DIRS):
                out.append(Finding(
                    "R1", BLOCK, path, node.lineno,
                    f"`{node.name}()` returns `{returns}` instead of a Pydantic model.",
                    snippet=lines[node.lineno - 1].strip(),
                    evidence='"create a pydantic object for this" / "use a pydantic object"',
                ))
        if isinstance(node, ast.ClassDef) and in_dirs(path, SCHEMA_DIRS) and looks_pydantic(node):
            for name, ann, _optional, lineno in model_fields(node):
                if is_loose_dict(ann):
                    out.append(Finding(
                        "R1", BLOCK, path, lineno,
                        f"`{node.name}.{name}` is typed `{ann}` — a contract field must be a model.",
                        snippet=lines[lineno - 1].strip(),
                        evidence='"the json should not be pass through ... well defined tool bodies"',
                    ))
    # Dict literal handed to a `payload=` keyword: the mock/service smell he flagged twice.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "payload":
                continue
            value = kw.value
            if isinstance(value, ast.Dict) and len(value.keys) >= 2:
                out.append(Finding(
                    "R1", BLOCK, path, value.lineno,
                    f"`payload=` receives a raw dict literal ({len(value.keys)} keys). "
                    "Build the model and pass `model.model_dump()`.",
                    snippet=lines[value.lineno - 1].strip(),
                    evidence='"make the payload a pydantic object" (flagged twice, in mocks)',
                ))


def check_r2_error_flow(tree: ast.Module, path: str, lines: list[str], out: list[Finding]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        raised = annotation_text(node.exc).split("(")[0].split(".")[-1]
        if raised == "HTTPException" and "main.py" not in path:
            out.append(Finding(
                "R2", BLOCK, path, node.lineno,
                "`raise HTTPException` — tools must answer 200 with the outcome in the body.",
                snippet=lines[node.lineno - 1].strip(),
                evidence='"for the upstream (the workflow) everything is a 200 response"',
            ))
        if in_dirs(path, SERVICE_DIRS) and raised.endswith("Error") and raised not in BUILTIN_ERRORS:
            out.append(Finding(
                "R2", BLOCK, path, node.lineno,
                f"A service raises `{raised}`. Catch it here and return a FlowCode instead — "
                "exceptions must not cross the service boundary.",
                snippet=lines[node.lineno - 1].strip(),
                evidence='"The errors should be handled in the business logic and return the '
                         'corresponding next steps"',
            ))
    if in_dirs(path, ROUTE_DIRS):
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "status_code" and isinstance(kw.value, ast.Constant):
                        code = kw.value.value
                        if isinstance(code, int) and code != 200:
                            out.append(Finding(
                                "R2", BLOCK, path, kw.value.lineno,
                                f"Route sets `status_code={code}`. The workflow only understands 200.",
                                snippet=lines[kw.value.lineno - 1].strip(),
                                evidence='"everything is a 200 response with different return body"',
                            ))


def check_r3_exceptions(tree: ast.Module, path: str, lines: list[str], out: list[Finding]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            out.append(Finding(
                "R3", BLOCK, path, node.lineno,
                "Bare `except:` swallows everything. Catch the typed error you expect.",
                snippet=lines[node.lineno - 1].strip(),
                evidence="exception hierarchy is per-root-cause",
            ))
        body = [s for s in node.body if not isinstance(s, ast.Expr)]
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            out.append(Finding(
                "R3", BLOCK, path, node.lineno,
                "`except ...: pass` hides a failure. Translate it to a FlowCode or re-raise.",
                snippet=lines[node.lineno - 1].strip(),
                evidence='"Raises rather than returning a sentinel"',
            ))
        if len(body) == 1 and isinstance(body[0], ast.Return):
            returned = body[0].value
            is_sentinel = returned is None or (
                isinstance(returned, ast.Constant) and returned.value in (None, "", 0, False)
            ) or (isinstance(returned, (ast.List, ast.Dict)) and not getattr(returned, "elts", getattr(returned, "keys", [])))
            if is_sentinel and not in_dirs(path, SERVICE_DIRS):
                out.append(Finding(
                    "R3", ASK, path, node.lineno,
                    "Handler returns an empty sentinel. A refusal must not look like an empty result.",
                    snippet=lines[node.lineno - 1].strip(),
                    evidence='"so a caller cannot accidentally treat a refusal as an empty result"',
                ))


def check_r4_enums(tree: ast.Module, path: str, lines: list[str], out: list[Finding]) -> None:
    """A module-level CONSTANT dict of literal keys that is read by subscript."""
    dict_consts: dict[str, tuple[int, int]] = {}
    for stmt in tree.body:
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            targets, value = [stmt.target], stmt.value
        else:
            continue
        # Unwrap MappingProxyType({...}) / frozendict({...})
        if isinstance(value, ast.Call) and value.args:
            value = value.args[0]
        if not isinstance(value, ast.Dict):
            continue
        keys = [k for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if len(keys) < 4 or len(keys) != len(value.keys):
            continue
        for target in targets:
            if target.id.isupper():
                dict_consts[target.id] = (target.lineno, len(keys))
    if not dict_consts:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Name):
            continue
        name = node.value.id
        if name in dict_consts:
            lineno, size = dict_consts[name]
            out.append(Finding(
                "R4", BLOCK, path, node.lineno,
                f"`{name}[...]` on a closed set of {size} literal keys (defined line {lineno}) "
                "can raise a bare KeyError. Model it as an Enum with a tolerant "
                "`for_prefix()`-style lookup and a strict one that raises ValueError.",
                snippet=lines[node.lineno - 1].strip(),
                evidence='"this could throw a KeyError no? why not instead of a dict we create an Enum?"',
            ))


def check_r5_magic(tree: ast.Module, path: str, lines: list[str], out: list[Finding]) -> None:
    literals: Counter[str] = Counter()
    literal_lines: dict[str, list[int]] = defaultdict(list)
    # Strings used as dict keys or keyword names are structural field names, not magic values.
    structural: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant):
                    structural.add(id(key))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in structural:
            value = node.value
            if value in TRIVIAL_LITERALS or len(value) < MIN_LITERAL_LEN or "\n" in value:
                continue
            literals[value] += 1
            literal_lines[value].append(node.lineno)
    for value, count in literals.items():
        if count >= MIN_LITERAL_REPEATS:
            first = min(literal_lines[value])
            out.append(Finding(
                "R5", BLOCK, path, first,
                f'String {value!r} repeated {count}x (lines {sorted(set(literal_lines[value]))}). '
                "Name it as a module constant.",
                snippet=lines[first - 1].strip(),
                evidence='"lets use a setting to not hardcode strings here"',
            ))
    # A numeric literal as a keyword argument: limit=2000, readingTypeId=13301.
    # Pydantic/Field constraints (max_length=128) are self-documenting, so schemas are skipped.
    if not in_dirs(path, SCHEMA_DIRS):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if not isinstance(kw.value, ast.Constant):
                    continue
                value = kw.value.value
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
                if value in BENIGN_NUMBERS or kw.arg in CONSTRAINT_KWARGS:
                    continue
                out.append(Finding(
                    "R5", ASK, path, kw.value.lineno,
                    f"`{kw.arg}={value}` is an unnamed number. Give it a named constant or a setting.",
                    snippet=lines[kw.value.lineno - 1].strip(),
                    evidence='"create a constant for this"',
                ))
    # Dict values that are literal catalog codes, e.g. {"category": "A1234"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(value, ast.Constant)):
                continue
            if not isinstance(value.value, str):
                continue
            if re.fullmatch(r"[0-9]{3,}[A-Z][0-9]{2,}|[A-Z]{2,}[0-9]{3,}", value.value):
                out.append(Finding(
                    "R5", BLOCK, path, value.lineno,
                    f'Catalog code {value.value!r} inlined under key {key.value!r}. '
                    "Extract it to a named constant with a provenance comment.",
                    snippet=lines[value.lineno - 1].strip(),
                    evidence='"create a constant for this" (one flagged code -> all six extracted)',
                ))


def check_r6_spanish(source: str, tree: ast.Module, path: str, lines: list[str], out: list[Finding]) -> None:
    # Comments, via tokenize.
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            text = tok.string.lstrip("#").strip()
            if text.startswith(("type:", "noqa", "pragma", "ruff:", "mypy:", "pylint:")):
                continue
            hits = spanish_hits(text)
            if len(hits) >= MIN_SPANISH_HITS:
                out.append(Finding(
                    "R6", BLOCK, path, tok.start[0],
                    f"Spanish comment (matched: {', '.join(hits[:6])}). Comments go in English; "
                    "only business field names and customer-facing strings stay in Spanish.",
                    snippet=text[:120],
                    evidence='"no spanish in code!"',
                ))
    except tokenize.TokenError:
        pass
    # Docstrings, via ast.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node)
        if not doc:
            continue
        hits = spanish_hits(doc)
        if len(hits) >= MIN_SPANISH_HITS:
            lineno = getattr(node, "lineno", 1)
            out.append(Finding(
                "R6", BLOCK, path, lineno,
                f"Spanish docstring (matched: {', '.join(hits[:6])}). Translate to English.",
                snippet=doc.strip().splitlines()[0][:120],
                evidence='"no spanish in code!"',
            ))


def check_r8_fields(tree: ast.Module, path: str, lines: list[str], out: list[Finding]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not looks_pydantic(node):
            continue
        fields = model_fields(node)
        is_envelope = bool(class_bases(node) & {"WorkflowResponse", "WorkflowKOResponse"})
        if len(fields) >= 3 and not is_envelope and all(optional for _n, _a, optional, _l in fields):
            out.append(Finding(
                "R8", ASK, path, node.lineno,
                f"`{node.name}`: all {len(fields)} fields are optional with defaults. "
                "A model where nothing is required was not designed — make the fields that "
                "every path fills required, or split it per outcome. (Most relevant for models "
                "this branch introduces; run with --diff to see only those.)",
                snippet=lines[node.lineno - 1].strip(),
                evidence='"whats this?" (the all-optional model was deleted outright)',
            ))
        names = [n for n, _a, _o, _l in fields]
        by_line = {n: l for n, _a, _o, l in fields}
        by_ann = {n: a for n, a, _o, _l in fields}
        for name in names:
            for suffix in ("_texto", "_text", "_str", "_legible", "_display", "_raw"):
                if name.endswith(suffix) and name[: -len(suffix)] in names:
                    out.append(Finding(
                        "R8", BLOCK, path, by_line[name],
                        f"`{node.name}` carries both `{name[:-len(suffix)]}` and `{name}` — the same "
                        "information in two formats. Keep the structured one.",
                        snippet=lines[by_line[name] - 1].strip(),
                        evidence='"why two separate fields?" / "why the texto field"',
                    ))
            if name.endswith("s") and name[:-1] in names:
                singular = name[:-1]
                # Only a real duplicate when the plural is a list of what the singular holds;
                # `next_step` (an object) next to `next_steps` (a message) is not one.
                plural_ann = by_ann[name]
                singular_ann = by_ann[singular].split("|")[0].strip()
                if f"list[{singular_ann}]" not in plural_ann.replace(" ", ""):
                    continue
                out.append(Finding(
                    "R8", BLOCK, path, by_line[singular],
                    f"`{node.name}` exposes both `{singular}` and `{name}`. Keep the collection; "
                    "callers index it.",
                    snippet=lines[by_line[singular] - 1].strip(),
                    evidence='"why having `items` and `item`?"',
                ))


def check_r9_compat(source: str, path: str, lines: list[str], out: list[Finding]) -> None:
    for index, line in enumerate(lines, start=1):
        match = COMPAT_PATTERNS.search(line)
        if match:
            out.append(Finding(
                "R9", BLOCK, path, index,
                f"Backward-compatibility marker ({match.group(0)!r}). This code is pre-prod: "
                "delete the shim, the field and the old tests.",
                snippet=line.strip(),
                evidence='"fuck backward compatibility. we are not in prod lol"',
            ))


def check_r11_spec_examples(tree: ast.Module, path: str, lines: list[str], out: list[Finding]) -> None:
    suspicious = re.compile(r"BLOCKLIST|BLACKLIST|WHITELIST|ALLOWLIST|DENYLIST|EXAMPLES|"
                            r"PLACEHOLDER|SAMPLE|KNOWN_|DUMMY|FAKE|TEST_VALUES", re.IGNORECASE)
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        targets = [t.id for t in stmt.targets if isinstance(t, ast.Name) and t.id.isupper()]
        if not targets:
            continue
        value = stmt.value
        if isinstance(value, ast.Call):
            fname = annotation_text(value.func).split(".")[-1]
            if fname not in {"frozenset", "set", "tuple", "list"} or not value.args:
                continue
            value = value.args[0]
        if not isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            continue
        literals = [e for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if len(literals) < 2 or len(literals) != len(value.elts):
            continue
        for name in targets:
            if suspicious.search(name):
                out.append(Finding(
                    "R11", ASK, path, stmt.lineno,
                    f"`{name}` hardcodes {len(literals)} literal sample values. Confirm the spec "
                    'said "the following values" and not "e.g." — if they were examples, '
                    "implement the underlying rule and drop the list.",
                    snippet=lines[stmt.lineno - 1].strip(),
                    evidence='"i dont think is needed, i would say they were just examples"',
                ))


def check_r12_boundary(tree: ast.Module, path: str, lines: list[str], out: list[Finding]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.startswith("_"):
                out.append(Finding(
                    "R12", ASK, path, key.lineno,
                    f"Transport key {key.value!r} mixed into a payload dict. Pydantic reserves "
                    "underscore-prefixed names — keep it outside the model and say why.",
                    snippet=lines[key.lineno - 1].strip(),
                    evidence='"careful here. is this intended?" (a "_MOCK" transport key)',
                ))


CHECKS_SOURCE = (check_r6_spanish,)
CHECKS_TREE = (
    check_r1_models, check_r2_error_flow, check_r3_exceptions, check_r4_enums,
    check_r5_magic, check_r8_fields, check_r11_spec_examples, check_r12_boundary,
)
CHECKS_LINES = (check_r9_compat,)


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------


def scan_file(path: Path, repo_root: Path) -> FileReport:
    rel = str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else str(path)
    report = FileReport(path=rel)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.findings.append(Finding("--", ASK, rel, 1, f"could not read: {exc}"))
        return report
    lines = source.splitlines() or [""]
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        report.findings.append(Finding("--", BLOCK, rel, exc.lineno or 1, f"syntax error: {exc.msg}"))
        return report
    for check in CHECKS_TREE:
        check(tree, rel, lines, report.findings)
    for check in CHECKS_SOURCE:
        check(source, tree, rel, lines, report.findings)
    for check in CHECKS_LINES:
        check(source, rel, lines, report.findings)
    if is_test_path(rel):
        report.findings = [f for f in report.findings if f.rule not in RULES_SKIPPED_IN_TESTS]
    return report


def collect_paths(targets: list[str]) -> list[Path]:
    out: list[Path] = []
    for target in targets:
        p = Path(target)
        if p.is_file() and p.suffix == ".py":
            out.append(p)
        elif p.is_dir():
            for child in sorted(p.rglob("*.py")):
                if not any(part in SKIP_DIR_NAMES for part in child.parts):
                    out.append(child)
    return out


def changed_files(ref: str) -> list[Path]:
    try:
        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", ref], capture_output=True, text=True, check=True
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", merge_base, "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
        untracked = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        print(f"scan.py: git failed ({exc}); falling back to the whole tree", file=sys.stderr)
        return collect_paths(["app"])
    names = {n for n in (diff + untracked).splitlines() if n.endswith(".py")}
    return [Path(n) for n in sorted(names) if Path(n).is_file()]


def repo_root() -> Path:
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(top)
    except subprocess.CalledProcessError:
        return Path.cwd()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", default=[], help="files or directories to scan")
    parser.add_argument("--diff", metavar="REF", help="scan only files changed against REF")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--rules", help="comma-separated rule ids to keep, e.g. R1,R6")
    args = parser.parse_args()

    root = repo_root()
    layer_origin = load_layers(root)
    if args.diff:
        files = changed_files(args.diff)
    elif args.paths:
        files = collect_paths(args.paths)
    else:
        files = collect_paths(["app"]) or collect_paths(["."])

    wanted = {r.strip().upper() for r in args.rules.split(",")} if args.rules else None
    findings: list[Finding] = []
    for path in files:
        for finding in scan_file(path, root).findings:
            if wanted is None or finding.rule in wanted:
                findings.append(finding)

    findings.sort(key=lambda f: (f.severity != BLOCK, f.rule, f.path, f.line))

    if args.json:
        print(json.dumps({
            "scanned_files": len(files),
            "layers": layer_origin,
            "counts": dict(Counter(f.rule for f in findings)),
            "blocking": sum(1 for f in findings if f.severity == BLOCK),
            "findings": [asdict(f) for f in findings],
        }, indent=2, ensure_ascii=False))
        return 1 if any(f.severity == BLOCK for f in findings) else 0

    if not findings:
        print(f"fran-scan: {len(files)} file(s) scanned, nothing to flag. [layers: {layer_origin}]")
        return 0

    by_rule: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_rule[finding.rule].append(finding)
    print(f"fran-scan: {len(files)} file(s) scanned, {len(findings)} finding(s) "
          f"({sum(1 for f in findings if f.severity == BLOCK)} blocking) "
          f"[layers: {layer_origin}]\n")
    for rule in sorted(by_rule):
        group = by_rule[rule]
        print(f"── {rule} · {len(group)} finding(s) · {group[0].evidence}")
        for f in group:
            print(f"   [{f.severity}] {f.path}:{f.line}  {f.message}")
            if f.snippet:
                print(f"           | {f.snippet}")
        print()
    return 1 if any(f.severity == BLOCK for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
