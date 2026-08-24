# The rules — full reference

Twelve rules distilled from 24 real review comments across 7 pull requests that were blocked
and then approved. Each carries: **evidence** (the reviewer's literal words), **severity**,
**detection** (what to look for), and the **canonical fix** as it was actually accepted.

Examples are written against a generic Python service — a FastAPI app with
`domain / services / clients / api.schemas / api.routes` layers calling one or more upstream
APIs. Adapt the names, not the shape.

Severity:
- **BLOCK** — changes were requested over this. Fix before pushing.
- **ASK** — it was questioned. Fix it, or be ready to justify it in the thread.

Rule index: R1 models · R2 error flow · R3 exceptions · R4 enums · R5 magic values ·
R6 language · R7 encapsulation · R8 redundant fields · R9 backward compat ·
R10 comments · R11 spec examples · R12 transport boundary

---

## R1 — Model everything. No `dict[str, Any]` pass-through. · BLOCK · 6 comments

The most frequent finding by a wide margin: **6 of 24**.

> *"create a pydantic object for this"*
> *"apply the schema here"*
> *"use a pydantic object"*
> *"make the payload a pydantic object"*
> *"make this a list of pydantic objects"*
> *"We need to create pydantic object per Tool Request parameters that we expected and their
> validations. the json should not be pass through. it should as deterministic as possible
> with well defined tool bodies"*

### Detection

- Any `-> dict[str, Any]` or `-> list[dict[...]]` return annotation in the domain or service layer
- Any `dict[str, Any]` field annotation in a request/response schema
- Dict literals passed as `payload=` — **especially in mocks and fixtures**
- A request field typed `dict[str, Any] | str` (raw JSON pass-through)
- Hand-rolled validation that Pydantic or the framework already does

### Scope — all four layers, not just the HTTP contract

| Layer | Wrong | Right |
|---|---|---|
| Request | `payload: dict[str, Any] \| str` | `payload: CreateThingRequest` |
| Response | `address: dict[str, Any] \| None` | `address: Address \| None` |
| Domain | `def build_address(...) -> dict[str, Any]` | `-> Address` |
| **Mocks** | `payload={"found": 1, "items": [...]}` | `ItemHistory(...).model_dump()` |

**Mocks are the layer people forget.** Two of the six comments were on a mock file.

### Canonical fix

```python
# BEFORE
def parse_items(response: dict[str, Any]) -> list[dict[str, str]]:
    rows = [{"code": text(...), "status": text(...)} for item in items(response, "records")]
    return [row for row in rows if row["code"]]

# AFTER — models live in the domain layer; the HTTP schema imports them
class ItemRecord(BaseModel):
    code: str | None = None
    category: str | None = None
    status: str | None = None
    created_at: str | None = None

class ItemHistory(BaseModel):
    found: int
    records: list[ItemRecord]

def parse_items(response: dict[str, Any]) -> list[ItemRecord]:
    rows = [ItemRecord(code=text(...), ...) for item in items(response, "records")]
    return [row for row in rows if row.code]
```

Tests change shape too: `assert row == ItemRecord(...)`, not a dict comparison.

`dict[str, Any]` is legitimate in exactly one place: **the raw upstream body, before parsing.**

### The advanced form — discriminated unions

When a body takes a different shape per tag, do not branch on the tag. Let Pydantic pick.

```python
# 1. Payload models EXTEND the real request models -> they cannot drift from the endpoint
class CreateThingPayload(CreateThingRequest):
    @field_validator("category")
    @classmethod
    def _category_allowed(cls, value: str) -> str:
        return _ensure_allowed(value, THING_CATEGORIES, "category")

# 2. One envelope per operation, tagged with Literal
class CreateThingValidation(BaseModel):
    endpoint: Literal["create-thing"]
    payload: CreateThingPayload

# 3. Discriminated union -> the framework selects the model by `endpoint` and validates natively
ToolValidation = Annotated[
    CreateThingValidation | UpdateThingValidation | ... | DeleteThingValidation,
    Field(discriminator="endpoint"),
]

class ValidatePayloadRequest(RootModel[ToolValidation]):
    """Body of the validation endpoint: a per-operation payload selected by its tag."""
```

That refactor removed **158 lines** of hand-rolled validation and **3** ad-hoc error models.
Closed-value validators reuse the **domain collections** that enforce the same values at
execution time — one source of truth, not a copy of the allowed set.

---

## R2 — Errors are business logic. The caller always gets 200. · BLOCK · 1 comment

Conceptually the most important thing in the whole set.

> *"we need to be careful in the error propagation of tools. for the upstream (the workflow)
> everything is a 200 response with diffferent reutrn body. The errors should be handled in
> the business logic and return the corresponding next steps to the upstream. See other
> endpoints for reference"*

Context: the caller is an automated consumer that cannot interpret HTTP status codes — it
reads the body and follows a next step. Any service whose client is a state machine rather
than a browser has the same constraint.

### The three-layer contract

```
CLIENT   raises the typed transport failure       (UpstreamError, ConfigurationError, ...)
SERVICE  catches it, translates to a result code  (LOOKUP_UNAVAILABLE, NOT_FOUND, AMBIGUOUS)
ROUTE    resolves the next step -> always 200, outcome in the body
```

### Detection

- A custom exception raised, or allowed to propagate, out of the service layer
- `raise HTTPException` anywhere outside the app-level handlers
- A route returning a non-200 `status_code`
- A new failure mode reusing a generic error instead of getting its own result code
- A failure path with no test

### Canonical fix

```python
async def resolve(self, *, query: str) -> ToolResult:
    try:
        candidates = await self._client.search(query=query, limit=_SEARCH_LIMIT)
    except UpstreamError:
        # This lookup is a best-effort external service: on failure ask the caller for the
        # value directly instead of escalating the whole interaction.
        return ToolResult(code=FlowCode.LOOKUP_UNAVAILABLE, payload={"found": False, "ambiguous": False})
    status, rows = match(candidates, query)
    if status == "not_found":
        return ToolResult(code=FlowCode.NOT_FOUND, payload={"found": False, "ambiguous": False})
    if status == "ambiguous":
        return ToolResult(code=FlowCode.AMBIGUOUS, payload={"found": True, "ambiguous": True, "candidates": rows})
    return ToolResult(code=FlowCode.RESOLVED, payload={"found": True, "ambiguous": False, **rows[0]})
```

### The fine print

1. **The client still raises.** This is not "stop using exceptions" — it is "don't let them
   cross the service boundary". The exception is internal transport; the result code is the
   external contract.
2. **One result code per failure mode**, each with its own actionable next step.
   `LOOKUP_UNAVAILABLE` ≠ `NOT_FOUND` ≠ `AMBIGUOUS`. No generic `error: true`.
3. **A best-effort dependency that falls over degrades, it does not escalate.** Lookup down →
   "ask the caller for the value", not → hand the whole interaction to a human.
4. ***"See other endpoints for reference"*** — consistency beats your own judgement. Read the
   neighbouring endpoint before inventing an error shape.
5. **The accepted fix shipped with a failure-path test.** It was not requested. It is assumed.

---

## R3 — Exception design · BLOCK (inferred) · 0 direct comments

No comment on this one, because the code already did it right — which makes it the de-facto
standard, and deviating from it is what would draw one.

```python
class ServiceError(Exception):
    """Base error with a message that is safe to return at the response boundary."""

    def __init__(self, public_message: str, *, endpoint: str,
                 upstream_code: str | None = None, status_code: int | None = None,
                 details: Any = None) -> None:
        ...

    @property
    def safe_log_message(self) -> str:
        """A fixed message suitable for structured logs."""


class ConfigurationError(ServiceError):
    """Cannot call the upstream because runtime configuration is incomplete."""


class UpstreamError(ServiceError):
    """The upstream returned an HTTP, transport, or invalid-payload failure."""
```

Where a single upstream has genuinely different failure causes, split further:
`UpstreamUnauthorizedError` / `UpstreamUnavailableError` / `UpstreamPayloadError`.

### Rules

- **One subclass per root cause**, never a single catch-all.
- **`public_message` ≠ `safe_log_message`.** What the caller may see is not what goes to the
  logs. No personal data in either.
- **Raise; never return a sentinel.** From the real docstring:
  *"Raises rather than returning a sentinel, so a caller cannot accidentally treat a refusal
  as an empty result."* A 403 collapsed into `[]` tells the caller "your query matched
  nothing" when the truth is "we were not allowed to ask". **Confusing "not permitted" with
  "not found" is the failure this design guards against.**
- **An unrecognisable response body is an error, not an empty list:** *"answering 'no members'
  would hide a contract change behind an empty result the caller cannot tell apart from a
  real one."*
- No bare `except:`, no `except X: pass`, no `return None` to signal failure.

---

## R4 — `Enum` instead of `dict` for a closed set · BLOCK · 1 comment

> *"this could throw a KeyError no? why not instead of a dict we create an Enum?"*

Two defects in one line: a `CODES[key]` subscript can raise a bare `KeyError`, and a dict lets
the set of valid keys and the values drift apart.

### Detection

A module-level dict constant over a **fixed, closed domain** — regions, statuses, categories,
document types, catalog codes — that is read with `[...]` subscript access.

### Canonical fix

```python
class Region(Enum):
    """A region, identified by the two digits its codes start with.

    An enum rather than a code-to-name dict, so a lookup with an unknown code fails with
    ``ValueError: '99' is not a valid Region`` instead of a bare ``KeyError``, and so the set
    of valid prefixes and the names cannot fall out of step: each member declares both.
    """

    NORTH = ("01", "NORTH")
    EAST = ("02", "EAST")
    # ... one member per code

    def __new__(cls, code: str, label: str) -> "Region":
        member = object.__new__(cls)
        member._value_ = code
        member.label = label
        return member

    @classmethod
    def for_prefix(cls, prefix: str) -> "Region | None":
        """Return the region a code prefix belongs to, or ``None`` if none does."""
        try:
            return cls(prefix)
        except ValueError:
            return None
```

### The subtle part — two lookups, two contracts

- `for_prefix()` → `None`: **tolerant**, for unvalidated input (user text, a transcription).
- `region_of()` → **raises** `ValueError` naming the code: **strict**, for input that already
  passed validation. If it fires, it is a bug and you want to see it.

```python
def normalize_code(value: str) -> str:
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) != CODE_LENGTH or Region.for_prefix(digits[:2]) is None:
        return ""                       # tolerant: bad input, ask again
    return digits

def region_of(code: str) -> Region:
    """Takes the output of normalize_code, which is what guarantees the prefix exists.
    Given anything else it raises ValueError naming the offending code, which is a failure
    worth seeing rather than a label invented for a code that has none."""
    return Region(code[:2])             # strict
```

Keep a `dict` only when the set is genuinely open or configuration-driven.

---

## R5 — No magic strings or numbers · BLOCK · 3 comments

> *"lets use a setting to not hardcode strings here"*
> *"same"*
> *"create a constant for this"*

| Before | After | Why |
|---|---|---|
| `self._client.get("primary", PATH)` | `self._clients.primary.get(PATH)` | the backend stops being a string at all |
| `"limit": 2000` | `MAX_RESULTS = 2000` | named, with a comment on the bound |
| `"category": "A1234"` | `CATEGORY_STANDARD = "A1234"` | catalog code, needs provenance |
| `FORBIDDEN = 403` | `httpx.codes.FORBIDDEN` | if the library already names it, use its name |

Constants carry a **provenance comment** — where the value came from, so the next reader can
check it against the source:

```python
# Catalog codes, from the integration spec shared by the provider.
CATEGORY_STANDARD = "A1234"
CATEGORY_URGENT = "A1235"
CATEGORY_INFORMATIONAL = "I1000"

# Upper bound for the search query: high enough to return every record for one subject
# while capping a pathological response.
MAX_RESULTS = 2000
```

### `"same"` means "and everywhere else in this file"

One comment flagged **one** inline code; the accepted fix extracted **all six** in that module.
Fixing only the flagged line is how you earn a second review round.

---

## R6 — Code is written in English · BLOCK · 3 comments

> *"no spanish in code!"*
> *"same!"*
> *"same"*

Generalised: **the codebase has one language, English.** The product's own language belongs
only where a human outside the team reads it. The boundary is precise — it is not
"everything in English":

| English | The product's language |
|---|---|
| Comments and docstrings | Request/response **field names** that model the business domain |
| Variables, functions, parameters, classes | Business catalog **values** taken verbatim from a spec |
| Log messages and log `extra` keys | Text returned to the end user or the calling agent |
| Constant names | |

```python
# BEFORE
# Estos campos son los que el sistema exige para poder dar de alta el registro.

# AFTER
# These are the fields the system requires in order to register the record.
nombre: str = Field(default="", max_length=128)          # field name stays in the domain language
primer_apellido: str = Field(default="", alias="primerApellido", max_length=128)
```

Only the comment changed. Internal parameters, though, go English:
`create_record(holder=..., phone=...)`, not `create_record(titular=...)`.

---

## R7 — Encapsulate by protocol. One client per upstream. · BLOCK · 2 comments

> *"lets create an auxiliar file called `bus` or similar to encapsulate all functions bus
> specific functions like the datetime and query encoding"*
> *"same! noticing a lot of replication here. could we unifiy by creating a mule specific
> httpx client? and another for the bus one"*

Two distinct moves, both in the same review.

### (a) An upstream's quirks live together, outside the domain

```python
# clients/legacy.py
"""Helpers for the quirks of the legacy backend.

These encapsulate the two things the rest of the code should not need to care about: how its
query string must be encoded, and how its timestamps must be formatted.
"""

def encode_filter_query(params) -> str: ...   # its separator must not be percent-encoded
def legacy_datetime(value) -> str: ...        # yyyyMMddHHmmss -> yyyy-MM-dd_HH:mm:ss
```

The date helper had been sitting in a domain module. **The domain has no business knowing how
a middleware formats timestamps.**

### (b) The backend is an identity, not a parameter

```python
# BEFORE — every call threads the string
class ApiClient:
    async def get(self, backend: Backend, path: str, ...): ...

await self._client.get("primary", WARNINGS_PATH)

# AFTER — fixed at construction
class ApiClient:
    """Call one backend without exposing credentials to route handlers."""

    def __init__(self, client: httpx.AsyncClient, settings: Settings, backend: Backend, *,
                 request_id: str | None = None, ...) -> None:
        self._backend = backend


@dataclass(frozen=True)
class ApiClients:
    """The per-backend clients, so callers pick a backend instead of passing a string."""

    primary: ApiClient
    legacy: ApiClient


def build_api_clients(client, settings, *, request_id=None, correlation_id=None) -> ApiClients:
    ids = {"request_id": request_id, "correlation_id": correlation_id}
    return ApiClients(primary=ApiClient(client, settings, "primary", **ids),
                      legacy=ApiClient(client, settings, "legacy", **ids))
```

Knock-on effect: `_base_url()`, `_headers()`, `_backend_headers()` all lose their `backend`
parameter. The string threading disappears from the entire class.

### (c) The corollary — response-envelope knowledge belongs to the client

```python
async def get_collection(self, path: str, *, params=None) -> list[Any]:
    """GET a collection and return its members.

    Knowing how this API wraps a collection belongs here rather than in a service or a domain
    module: it is a property of the API, it is the same for every collection it serves, and
    the domain has no business knowing its entries arrived over HTTP.
    """
```

The domain went from a function that unwrapped the HTTP envelope to one that only maps
already-extracted entries.

---

## R8 — No redundant fields. Every field justifies itself. · ASK→BLOCK · 4 comments

> *"why two separate fields?"*
> *"why the texto field"*
> *"why having `items` and `item`?"*
> *"whats this?"*

Two heuristics:
1. **The same information in two formats → one of them is dead.**
2. **A model where every field is optional is a model that was not designed.**

```python
# BEFORE                                   # AFTER
address: dict[str, Any] | None             address: Address | None
address_text: str | None                   # deleted — the caller composes the sentence
items: list[ItemResponse] | None           items: list[ItemResponse] | None
item: ItemResponse | None                  # deleted
region: str | None   # on ItemResponse     # deleted — already inside address
```

The `"whats this?"` landed on a model with eight fields, **all optional**, describing
validation diagnostics. The whole model was deleted and replaced with native framework
validation.

### Corollary — declare each field exactly once

```python
# BEFORE: the HTTP schema restates the domain model's fields
class SearchResponse(WorkflowResponse):
    query: str | None = None
    normalized: str | None = None
    matches: list[Match] | None = None

# AFTER: it inherits them
class SearchResponse(WorkflowResponse, MatchResult):
    """The fields come from MatchResult rather than being restated, so the service and the
    contract cannot drift apart. They are required, not optional: every outcome this schema
    serialises carries all three, and a technical failure is answered with the bare envelope
    through a different schema."""
```

Note the second move: `str | None = None` → **required `str`**. If every path that serialises
the schema fills the field, it is not optional. Defensive `| None = None` is noise.

---

## R9 — Backward compatibility does not exist before production · BLOCK · 1 comment

> *"fuck backward compatibility. we are not in prod lol . this is a very common claude thing,
> tell him to not care about it"*

The offending code:

```python
"items": items,
# Kept for backward compatibility: the first (primary) item.
"item": items[0] if items else build_item(primary),
```

The field was deleted, the schema entry was deleted, and the four tests that used it were
updated.

Applies equally to: shims, field aliases kept "just in case", `deprecated` markers,
`if old_format:` branches, defaults that tolerate a retired payload shape.

**This is called out explicitly as a Claude tic.** Say so in your prompt when generating code.

---

## R10 — Comments explain *why*, and define what they cite · ASK · 1 comment

> *"what is hydra?"*

The original comment already explained the why. What it did wrong was assume the reader knew
the standard it named. The accepted fix **tripled** the comment: what the term is, how we know
the upstream uses it, a concrete example of the shape, and what happens if it changes.

```python
# Where to find the items of a collection response.
#
# The middleware does not return a bare JSON array. It wraps the items in an envelope and puts
# them under a key, because it is built with <framework>, which follows <standard>: a
# vocabulary for describing REST APIs, whose convention is that a collection's items live
# under "<key>" alongside metadata such as "<count-key>". Two clues in its 403 give it away:
# an "x-powered-by" header and a link header pointing at its own API documentation. So a
# lookup answers:
#
#     {"<count-key>": 1, "<key>": [{"id": "1234", "name": "EXAMPLE"}]}
#
# Version 4 of that framework renames the key, and we do not know which version this
# deployment runs, so both are accepted. Anything else is a contract change worth failing on.
_COLLECTION_KEYS = ("<key>", "<alternate-key>")
```

**A shorter comment has never been requested.** Ten-line module docstrings explaining an
upstream's quirks pass review untouched. What is asked for is that an opaque term arrives
with: *what it is · how we know · an example · what happens if it changes.*

Comments that survive review:
1. **The upstream quirk and the evidence for it** — *"they receive the encoded separator and fail with a connection error"*
2. **The decision and the alternative rejected** — *"An enum rather than a code-to-name dict, so..."*
3. **Blocking external state** — *"The code exists but is not enabled for our channel yet, so the upstream returns error 2001 until they enable it."*

The one that gets deleted: **the comment that justifies debt** (*"kept for backward compatibility"*).

Before pushing, list the acronyms and product names your diff introduces. Any that a new
teammate could not look up needs a sentence.

---

## R11 — Examples in a spec are not business rules · ASK · 1 comment

> *"i dont think is needed, i would say they were just examples"*

The code had turned a list of placeholder values that appeared in the acceptance criteria into
a hardcoded, enforced blocklist. The reviewer's read: those were **illustrations of a
category**, not an exhaustive set to enforce.

### Detection

A constant collection (`frozenset`, `tuple`, `list`) of literal sample values lifted from a
spec, requirements doc, or ticket, used as a hard allow/deny list.

### Before coding it, answer

1. Did the spec say **"such as / e.g. / for example"**, or **"the following values"**?
2. Is the set stable, or will it need a code change every time someone invents a new value?
3. Is there a **rule** that captures the category instead of an enumeration?
   (*"the local part is only dots"* is a rule; `test@test.com` is an example of one.)
4. If it really is a closed set, it belongs in a **domain collection** other code validates
   against (see R1), not a private constant used once.

When in doubt, implement the rule and drop the list. The default assumption is that a list of
examples is not a requirement.

---

## R12 — Mind the transport/model boundary · ASK · 1 comment

> *"careful here. is this intended?"*

A `"_MOCK": True` key was sitting in what was about to become a Pydantic payload. Pydantic
**reserves underscore-prefixed names**, so the field would have been silently dropped. The
resolution kept it — deliberately, outside the model, with the reason written down:

```python
# ``_MOCK`` is the transport marker the response wrapper reads to tag the data source; it
# stays outside the Pydantic payload because Pydantic reserves underscore-prefixed names.
return ToolResult(code=FlowCode.HISTORY, payload={**history.model_dump(), "_MOCK": True})
```

Generalised: when transport metadata (mock markers, correlation ids, internal diagnostics)
mixes with business payload, make the boundary explicit and say why. *"is this intended?"* is
a real question — a documented deliberate choice closes the thread.
