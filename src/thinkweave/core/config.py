"""Configuration loading for thinkweave.

Priority (vault_root resolution):
1. ``THINKWEAVE_VAULT`` env var
2. ``~/.config/thinkweave/config.toml`` (XDG-respectful user-scope file)
3. ``vault/config/config.toml`` (vault-internal — also owns embedding/edge/dream;
   a pre-2026-06-13 file at ``vault/.weave/config.toml`` is read as a fallback)
4. Built-in defaults

The user-scope tier (2) only ever provides ``vault_root`` and ``rsi_rail``;
vault-internal fields (embeddings, edges, dream gates) remain owned by the
vault-internal file at tier 3.

Knob precedence: the ``rsi-rail`` policy repo's ``thinkweave/config.toml``
(located by ``THINKWEAVE_RSI_RAIL`` or the user-scope ``rsi_rail`` key)
merges over the vault file with the same key handling, so a harness-level
knob the user tunes lives in the repo while the vault keeps everything
else. No repo configured means no change. The repo file may carry only
the serving-chain knobs; any other key is a ``ValueError`` naming it and
the file.

Knob metadata (dec-db6baaab: the registry is derived, not hand-written).
Every field of ``Config`` and of the section dataclasses it nests is
declared through :func:`knob`, whose ``field(metadata=...)`` carries:

- ``read_at``: the events that read the value, from :data:`READ_AT`
  (``SessionStart``, ``UserPromptSubmit``, ``weave_call``, ``wrap``,
  ``dream``, ``index``, ``machine``).
- ``toml``: the TOML address relative to the enclosing table (``Config`` is
  the document root). ``None`` means the field name; ``""`` means the field
  has no address in a knob document; a trailing ``*`` is a key prefix whose
  remainder keys a dict (with ``choices_ref``, the remainders are the
  allowed names and any other is refused by name); ``"*"`` alone keys one
  sub-table per entry.
- ``parse``: a type called with the TOML value, or a function
  ``(value, cfg)`` for a value that needs the config (``weave_dir``).
- ``default_ref`` / ``choices_ref``: ``"module:attr"`` references to the
  reader's own declaration of the default or of the allowed names, for an
  override whose default lives with its reader (``retrieval/context.py``).
- ``each``: the dataclass of each entry of a keyed sub-table dict.
- ``unset``: what an unset override means, for a default that is computed.
- ``choice_noun``: what a refusal calls a name ``choices_ref`` does not
  list (``event``); ``name`` when unset.

From that one declaration :func:`_apply_knobs` reads every key, the rail
check derives the allowed set (a field is rail-eligible iff one of its
``read_at`` events is on the serving chain, :data:`SERVING_CHAIN`), and
``weave policy inventory --toml`` renders the rail template. An unset
knob keeps today's behaviour (dec-6d5f1913: seams only).
"""

from __future__ import annotations

import json
import os
import platform
import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import MISSING, Field, dataclass, field, fields, is_dataclass
from importlib import import_module
from pathlib import Path, PurePosixPath

from thinkweave.core.fswrite import atomic_write_text
from thinkweave.core.task_contract import GithubIssue


_DEFAULT_VAULT = Path.home() / "vault"
UNSCOPED_PROJECT = "_unscoped"


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _resolve_weave_dir_value(value: str, vault_root: Path) -> Path:
    """Expand ``~`` and anchor a relative path at ``vault_root``.

    Shared by the ``weave_dir`` TOML key and the ``THINKWEAVE_WEAVE_DIR``
    env override so both tiers apply the same rule: absolute paths pass
    through unchanged; relative paths resolve against the vault root, not
    the process cwd.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = vault_root / path
    return path


# The events a knob is read at. The first four are the serving chain: a
# value read there shapes what a session sees, so it is policy and may live
# in the rsi-rail repo; the rest stay in the vault's config.toml.
READ_AT = (
    "SessionStart",
    "UserPromptSubmit",
    "weave_call",
    "wrap",
    "dream",
    "index",
    "machine",
)
SERVING_CHAIN = frozenset(READ_AT[:4])


def knob(
    default=MISSING,
    *,
    read_at: str | tuple[str, ...],
    toml: str | None = None,
    parse=None,
    default_factory=MISSING,
    default_ref: str | None = None,
    choices_ref: str | None = None,
    each: type | None = None,
    unset: str | None = None,
    choice_noun: str | None = None,
):
    """A dataclass field carrying its knob metadata (see the module docstring)."""
    events = (read_at,) if isinstance(read_at, str) else tuple(read_at)
    if not events or any(e not in READ_AT for e in events):
        raise ValueError(f"read_at {events!r} must name events from {READ_AT}")
    metadata = {
        "read_at": events,
        "toml": toml,
        "parse": parse,
        "default_ref": default_ref,
        "choices_ref": choices_ref,
        "each": each,
        "unset": unset,
        "choice_noun": choice_noun,
    }
    if default_factory is not MISSING:
        return field(default_factory=default_factory, metadata=metadata)
    return field(default=default, metadata=metadata)


def _parse_weave_dir(value: str, cfg: Config) -> Path:
    return _resolve_weave_dir_value(value, cfg.vault_root)


def _parse_identities(value, cfg: Config) -> tuple[str, ...]:
    """A list, or one comma-separated string."""
    if isinstance(value, str):
        value = [t.strip() for t in value.split(",") if t.strip()]
    return tuple(value)


def _parse_band(value, cfg: Config) -> tuple[float, float]:
    """A ``[low, high]`` pair of probabilities, low first."""
    try:
        low, high = (float(v) for v in value)
    except (TypeError, ValueError):
        raise ValueError(f"a band is [low, high], got {value!r}") from None
    if not 0.0 <= low <= high <= 1.0:
        raise ValueError(f"a band needs 0 <= low <= high <= 1, got {value!r}")
    return low, high


def resolve_ref(ref: str):
    """The object a ``"module:attr"`` metadata reference names. Imported
    on use: the readers it names import this module."""
    module, _, attr = ref.partition(":")
    return getattr(import_module(module), attr)


def _field_default(f: Field):
    return f.default_factory() if f.default_factory is not MISSING else f.default


def _is_section(f: Field) -> bool:
    """A field holding a nested knob dataclass (a TOML sub-table)."""
    return not f.metadata.get("each") and is_dataclass(_field_default(f))


@dataclass(frozen=True)
class KnobSpec:
    """One knob, derived from a field's metadata.

    ``address`` is the full dotted TOML address, or ``None`` for a field no
    knob document can set. A keyed sub-table appears as a ``<name>``
    placeholder segment (``context.startup.<section>.pin``) and ``keyed`` is
    the dict field whose ``choices_ref`` names the allowed keys.
    """

    owner: type
    name: str
    address: str | None
    read_at: tuple[str, ...]
    default: object
    metadata: Mapping
    keyed: Field | None = None

    @property
    def rail(self) -> bool:
        return bool(SERVING_CHAIN.intersection(self.read_at))


def _join(prefix: str, rel: str) -> str:
    return f"{prefix}.{rel}" if prefix else rel


def knob_specs(root: type | None = None) -> list[KnobSpec]:
    """Every leaf knob under ``root`` (default: ``Config``), in field order.

    Raises ``TypeError`` for a field that declares no ``read_at``, so a
    field cannot join the config without saying where it is read.
    """
    out: list[KnobSpec] = []

    def walk(cls: type, prefix: str | None, keyed: Field | None) -> None:
        for f in fields(cls):
            md = f.metadata
            if "read_at" not in md:
                raise TypeError(f"{cls.__name__}.{f.name} declares no read_at")
            rel = f.name if md["toml"] is None else md["toml"]
            address = None if prefix is None or rel == "" else _join(prefix, rel)
            if md.get("each"):
                walk(md["each"], _join(prefix or "", f"<{f.name}>"), f)
            elif _is_section(f):
                walk(type(_field_default(f)), address, keyed)
            else:
                out.append(
                    KnobSpec(
                        cls, f.name, address, md["read_at"], _field_default(f), md, keyed
                    )
                )

    walk(root or Config, "", None)
    return out


def iter_rail_addresses(root: type | None = None) -> Iterator[tuple[str, str]]:
    """``(table, key)`` for every rail-eligible address, keyed tables expanded
    over their allowed names."""
    for spec in knob_specs(root):
        if not spec.rail or spec.address is None:
            continue
        table, _, key = spec.address.rpartition(".")
        if spec.keyed is None:
            for expanded, _name in expand_prefix(spec):
                yield table, expanded
            continue
        placeholder = f"<{spec.keyed.name}>"
        for name in resolve_ref(spec.keyed.metadata["choices_ref"]):
            yield table.replace(placeholder, name), key


def expand_prefix(spec: KnobSpec) -> list[tuple[str, str | None]]:
    """``[(key, name)]`` for one address: a key prefix with ``choices_ref``
    (``budget_ms.*`` over the canonical events) expands to one key per
    allowed name, anything else is itself with no name."""
    key = spec.address.rpartition(".")[2] if spec.address else ""
    ref = spec.metadata.get("choices_ref")
    if not (key.endswith("*") and ref):
        return [(key, None)]
    return [(key[:-1] + name, name) for name in resolve_ref(ref)]


def _subtable(table: dict, dotted: str) -> dict:
    for part in dotted.split(".") if dotted else ():
        table = table.get(part, {})
        if not isinstance(table, dict):
            return {}
    return table


def _check_choices(
    names, ref: str, address: str, path: Path | None, noun: str = "section"
) -> None:
    known = resolve_ref(ref)
    for name in names:
        if name not in known:
            where = f" in {path}" if path else ""
            raise ValueError(
                f"unknown {noun} {name!r} at {address!r}{where}: the known "
                f"{noun}s are {', '.join(known)}"
            )


def _parse(parse, value, cfg: Config):
    if parse is None:
        return value
    if isinstance(parse, type):
        return parse(value)
    return parse(value, cfg)


def _apply_fields(obj, table: dict, prefix: str, cfg: Config, path: Path | None) -> None:
    """Set each field of ``obj`` whose address ``table`` carries."""
    for f in fields(obj):
        md = f.metadata
        rel = f.name if md["toml"] is None else md["toml"]
        if rel == "":
            continue
        if md.get("each"):
            entries = {k: v for k, v in table.items() if isinstance(v, dict)}
            _check_choices(
                entries, md["choices_ref"], prefix, path,
                noun=md.get("choice_noun") or "section",
            )
            target = getattr(obj, f.name)
            for name, sub in entries.items():
                entry = target.setdefault(name, md["each"]())
                _apply_fields(entry, sub, _join(prefix, name), cfg, path)
            continue
        if _is_section(f):
            sub = _subtable(table, rel)
            if sub:
                _apply_fields(getattr(obj, f.name), sub, _join(prefix, rel), cfg, path)
            continue
        parent, _, key = rel.rpartition(".")
        sub = _subtable(table, parent)
        if key.endswith("*"):
            stem = key[:-1]
            found = {
                k[len(stem) :]: _parse(md["parse"], v, cfg)
                for k, v in sub.items()
                if k.startswith(stem)
            }
            if md.get("choices_ref"):
                _check_choices(
                    found, md["choices_ref"], _join(prefix, parent), path,
                    noun=md.get("choice_noun") or "name",
                )
            if found:
                setattr(obj, f.name, found)
        elif key in sub:
            value = _parse(md["parse"], sub[key], cfg)
            if md.get("choices_ref"):
                _check_choices(value, md["choices_ref"], _join(prefix, rel), path)
            setattr(obj, f.name, value)


@dataclass
class PromptTimeRetrieval:
    """Config for prompt-time retrieval enrichment (R2).

    On each substantive user prompt the UserPromptSubmit hook runs a bounded
    retrieval (gated by the ``prompt_gate`` question set when it is on),
    drops anything already served this session, applies hard caps, and
    prepends a small ignorable block. Defaults are deliberately
    conservative — this is default-on, so the caps are the safety net against
    the noise-tax failure mode that retired the old pre-Edit injection.
    """

    enabled: bool = knob(True, read_at="UserPromptSubmit", parse=bool)
    # Triviality gate only — skip trivially short inputs and slash-commands so
    # we don't pay an embedding on "ok"/"yes"/"/clear". NOT a semantic filter.
    min_prompt_chars: int = knob(12, read_at="UserPromptSubmit", parse=int)
    # Wall-clock budget for the similarity (embedding) arm, run in a daemon
    # thread; on overrun the arm is abandoned. Generous enough to let the embedding complete on a normal network; kept
    # under the UserPromptSubmit hook timeout.
    embed_deadline_seconds: float = knob(4.0, read_at="UserPromptSubmit", parse=float)
    # Adaptive skip — consecutive misses (an embedding deadline or a prompt
    # gate call that gave no verdict) this session before prompt-time
    # retrieval is skipped for the rest of the session. Protects against
    # re-paying a sunk deadline on every remaining turn when an endpoint is
    # slow or unreachable.
    deadline_miss_limit: int = knob(3, read_at="UserPromptSubmit", parse=int)
    # Cosine floor on the similarity arm — the relevance gate while the
    # prompt_gate question set is off (the gate replaces it). Drops low-cosine
    # nearest neighbours, so generic/meta prompts (~0.22–0.36) no-op while
    # domain prompts (~0.40+) fire. Model-dependent (text-embedding-3-small);
    # retune if you swap embedding models.
    min_similarity: float = knob(0.38, read_at="UserPromptSubmit", parse=float)
    # Per-turn caps.
    max_pieces_per_turn: int = knob(3, read_at="UserPromptSubmit", parse=int)
    max_injected_chars_per_turn: int = knob(1200, read_at="UserPromptSubmit", parse=int)
    # Per-session caps — make R2 self-extinguish as the session matures.
    max_firings_per_session: int = knob(8, read_at="UserPromptSubmit", parse=int)
    max_injected_chars_per_session: int = knob(
        6000, read_at="UserPromptSubmit", parse=int
    )
    # Bias toward the axes startup under-serves (sources + learnings); decisions
    # are already well-exposed at boot (Key Files table + Decisions Worth
    # Understanding), so they're left out of the default bias.
    bias_types: tuple[str, ...] = knob(
        ("source", "note"), read_at="UserPromptSubmit", parse=tuple
    )


@dataclass
class StartupSection:
    """Per-section overrides under ``[context.startup.<section>]``.

    Both unset by default: an unset ``cap`` leaves the section body whole
    (its builder's soft budget in ``retrieval/context.py`` is the reference
    value the inventory shows), an unset ``pin`` leaves the section to the
    budget rules every other section follows.
    """

    # Pinned: the budget never drops this section and never cuts its tail.
    pin: bool = knob(False, read_at=("SessionStart", "weave_call"), parse=bool)
    # Chars the section body is cut to (head kept), before the payload budget.
    cap: int | None = knob(
        None,
        read_at=("SessionStart", "weave_call"),
        parse=int,
        default_ref="thinkweave.retrieval.context:SECTION_SOFT_BUDGET_CHARS",
        unset="no cut (the builder's soft budget is {default})",
    )


@dataclass
class ContextStartup:
    """Overrides for the SessionStart context payload (#352, dec-6d5f1913).

    Every field is an override: unset (``None`` or empty) means the
    defaults ``retrieval/context.py`` declares, so a vault or rail file that
    names none of these keys serves today's payload byte for byte. Section
    names are checked against ``retrieval.context.SECTIONS`` when the file
    loads; an unknown name is refused by name.
    """

    # Ordered section ids SessionStart serves; also the default section set
    # of ``weave_project_snapshot`` when it is called without ``sections``.
    sections: tuple[str, ...] | None = knob(
        None,
        read_at=("SessionStart", "weave_call"),
        parse=tuple,
        default_ref="thinkweave.retrieval.context:SESSION_START_SECTIONS",
        choices_ref="thinkweave.retrieval.context:SECTIONS",
    )
    # Chars the SessionStart payload may spend. Unset: the harness profile's
    # inline cap less the guard reserve, or the default token budget when the
    # profile measures no cap.
    budget_chars: int | None = knob(
        None,
        read_at="SessionStart",
        parse=int,
        unset="the harness cap less the guard reserve",
    )
    # Sections dropped whole when over budget, cheapest-to-lose first.
    drop_order: tuple[str, ...] | None = knob(
        None,
        read_at=("SessionStart", "weave_call"),
        parse=tuple,
        default_ref="thinkweave.retrieval.context:DROP_ORDER",
        choices_ref="thinkweave.retrieval.context:SECTIONS",
    )
    # ``[context.startup.<section>]`` sub-tables, keyed by section id.
    section: dict[str, StartupSection] = knob(
        default_factory=dict,
        read_at=("SessionStart", "weave_call"),
        toml="*",
        each=StartupSection,
        choices_ref="thinkweave.retrieval.context:SECTIONS",
    )


@dataclass
class HooksStop:
    """``[hooks.stop]``: the Stop hook's seams. Off by default
    (dec-6d5f1913: seams only, an unset knob is today's behaviour)."""

    # The #345 seam: a final message claiming a verification ("tests pass")
    # with no test command run this turn blocks the Stop once with a nudge.
    # Read at Stop, the end of the turn the claim closes; the metadata
    # vocabulary names that moment ``wrap``.
    unverified_claim_nudge: bool = knob(False, read_at="wrap", parse=bool)


@dataclass
class Hooks:
    """``[hooks]``: hook behaviour as knobs (dec-4f158b58 layer 1).

    The handler reads these first and the firing harness's profile literal
    second. A value may only lower a literal: the harness's wall clock
    (``hook_budget_ms``) and its inline context cap
    (``additional_context_cap_chars``) sit outside python, so the handler
    spends ``min(knob, literal)`` and refuses a higher value with one log
    line. Unset keys keep the literal, which is today's behaviour.
    """

    # Chars of SessionStart additional context the payload budget derives
    # from (the profile's measured inline cap when unset).
    additional_context_cap_chars: int | None = knob(
        None,
        read_at="SessionStart",
        parse=int,
        unset="the harness profile's literal",
    )
    # ``[hooks.budget_ms] <Event> = <ms>``, keyed by canonical event; an
    # unknown event is refused by name. The handler reads it on every
    # event; ``read_at`` names the two serving events it shapes.
    budget_ms: dict[str, int] = knob(
        default_factory=dict,
        read_at=("SessionStart", "UserPromptSubmit"),
        toml="budget_ms.*",
        parse=int,
        choices_ref="thinkweave.core.harness:CANONICAL_EVENTS",
        choice_noun="event",
        unset="the harness profile's literal",
    )
    stop: HooksStop = knob(default_factory=HooksStop, read_at="wrap")

    def effective(
        self, budget_literals: Mapping[str, int], cap_literal: int | None
    ) -> EffectiveHooks:
        """What is in force against one harness profile's literals.

        A knob above its literal is refused (the literal stays) and the
        refusal is returned as one line for the caller to log; a knob for
        an event or cap the profile sets no literal for lowers nothing and
        stands alone (the harness waits forever there).
        """
        refused: list[str] = []
        budgets = dict(budget_literals)
        for event, ms in self.budget_ms.items():
            literal = budget_literals.get(event)
            if literal is not None and ms > literal:
                refused.append(
                    f"[hooks.budget_ms] {event} = {ms} is above the harness "
                    f"literal {literal}; the literal stays in force (a knob "
                    "may only lower it)"
                )
            else:
                budgets[event] = ms
        cap = cap_literal
        knob_cap = self.additional_context_cap_chars
        if knob_cap is not None:
            if cap_literal is not None and knob_cap > cap_literal:
                refused.append(
                    f"[hooks] additional_context_cap_chars = {knob_cap} is above "
                    f"the harness literal {cap_literal}; the literal stays in "
                    "force (a knob may only lower it)"
                )
            else:
                cap = knob_cap
        return EffectiveHooks(budgets, cap, self.stop.unverified_claim_nudge, tuple(refused))


@dataclass(frozen=True)
class EffectiveHooks:
    """:meth:`Hooks.effective`'s answer: the per-event budgets (ms) and the
    inline context cap (chars) in force, the Stop nudge's state, and the
    refusals of knobs above their literal."""

    budget_ms: dict[str, int]
    cap_chars: int | None
    nudge: bool
    refused: tuple[str, ...] = ()


_VERDICT_EVENTS = ("UserPromptSubmit", "weave_call", "wrap")


@dataclass
class QuestionSet:
    """One System 1 question set's serving policy (``[verdicts.<question_set>]``).

    Every question set is off until a knob turns it on. An empty ``backend`` or
    ``model`` takes the ``api.yaml`` default; ``accept`` is the probability a
    verdict must reach, ``pool_size`` the candidates scored per call, and
    ``deadline_ms`` how long the question set waits for the backend. The prompt gate
    reads its question set at UserPromptSubmit and the ex-post judge at wrap; the
    acquisition dedup and the concept guard read theirs inside ``weave_*`` calls
    and at wrap; the citation backfill and the news triage read theirs in a
    ``weave decide`` call. The deadline bounds the prompt gate only.
    ``escalate`` is the news triage's band, low to high inclusive: an item
    whose top verdict probability falls inside it goes to the triage worker.
    """

    enabled: bool = knob(False, read_at=_VERDICT_EVENTS, parse=bool)
    backend: str = knob("", read_at=_VERDICT_EVENTS, parse=str)
    model: str = knob("", read_at=_VERDICT_EVENTS, parse=str)
    accept: float = knob(0.7, read_at=_VERDICT_EVENTS, parse=float)
    pool_size: int = knob(30, read_at=_VERDICT_EVENTS, parse=int)
    deadline_ms: int = knob(1500, read_at="UserPromptSubmit", parse=int)
    escalate: tuple[float, float] = knob(
        (0.35, 0.65), read_at="weave_call", parse=_parse_band
    )


@dataclass
class Verdicts:
    """``[verdicts]``: the System 1 question sets, one sub-table each."""

    # ``[verdicts.<question_set>]`` sub-tables, keyed by name; an unknown question set
    # is refused by name.
    question_set: dict[str, QuestionSet] = knob(
        default_factory=dict,
        read_at=_VERDICT_EVENTS,
        toml="*",
        each=QuestionSet,
        choices_ref="thinkweave.operations.verdicts:QUESTION_SETS",
        choice_noun="question set",
    )


@dataclass
class Config:
    # Resolved by load_config itself (env, user-scope file, the vault file's
    # top-level ``vault_root``), never by the knob documents.
    vault_root: Path = knob(
        default_factory=lambda: _DEFAULT_VAULT, read_at="machine", toml=""
    )
    # Relocates derived state (index.db, embeddings.db, buffer/, logs — see
    # weave_dir below) off the vault path. The vault markdown must stay put
    # (Obsidian owns it), but everything under weave_dir is derived and
    # rebuildable, so it can move to fast local disk when the vault lives on
    # slow/remote/virtualized storage (e.g. a Windows drive crossed from
    # WSL2 via 9P — measured ~30x slower SQLite reads — or a NAS/Dropbox
    # mount). None (default) keeps the historical vault_root/.weave layout.
    weave_dir_override: Path | None = knob(
        None, read_at="machine", toml="weave_dir", parse=_parse_weave_dir
    )
    # The rsi-rail policy repo, when configured; its thinkweave/config.toml
    # is the last knob tier. None when the user has no repo.
    # Located from ``THINKWEAVE_RSI_RAIL`` or the user-scope file only.
    rsi_rail: Path | None = knob(None, read_at="machine", toml="")
    default_project: str = knob("", read_at="machine", toml="default_project")
    embedding_model: str = knob(
        "text-embedding-3-small", read_at="machine", toml="embeddings.model"
    )
    # env var name holding the key
    embedding_api_key_env: str = knob(
        "OPENAI_API_KEY", read_at="machine", toml="embeddings.api_key_env"
    )
    embedding_api_url: str = knob(
        "https://api.openai.com/v1/embeddings", read_at="machine", toml="embeddings.api_url"
    )

    # Edge generation thresholds
    # min shared concepts for relates_to edge
    concept_edge_threshold: int = knob(
        1, read_at="index", toml="edges.concept_threshold", parse=int
    )
    # skip concepts in >5% of notes
    concept_edge_max_freq_pct: float = knob(
        0.05, read_at="index", toml="edges.concept_max_freq_pct", parse=float
    )
    # min shared tags for relates_to edge
    tag_edge_threshold: int = knob(
        2, read_at="index", toml="edges.tag_threshold", parse=int
    )
    # skip tags in >10% of notes
    tag_edge_max_freq_pct: float = knob(
        0.10, read_at="index", toml="edges.tag_max_freq_pct", parse=float
    )
    tag_edge_exclude: tuple[str, ...] = knob(
        ("todo", "probe", "parked", "til"),
        read_at="index",
        toml="edges.tag_exclude",
        parse=tuple,
    )

    # Dream apply-phase gates (Slice 1.5)
    # When False (default), priority_signals with action='enqueue' are
    # counted as logged-only — the LLM's intent is preserved on disk
    # but no queue write happens. Flip to True after the first cycle's
    # report has been reviewed.
    dream_enqueue_priority_signals: bool = knob(
        False, read_at="dream", toml="dream.enqueue_priority_signals", parse=bool
    )

    # C19b — per-concept PageRank. When True, the dream apply phase
    # computes per-concept-induced-subgraph PageRank after each
    # rebuild. Stored in the ``graph_ranks`` table; consumed by
    # ``weave_concepts(action='canonical_for', concept=X)``. Adds ~1s
    # per 100 active concepts on a typical vault (pure-Python power
    # iteration), so off by default until the user opts in.
    dream_compute_pagerank: bool = knob(
        False, read_at="dream", toml="dream.compute_pagerank", parse=bool
    )

    # Cap on essence candidates surfaced per scan (themes + concept hubs,
    # placeholder-first). Keeps the nightly essence-worker payload bounded;
    # 0 = unlimited (the backfill lever — `weave dream scan --essence-cap 0`).
    dream_essence_cap: int = knob(
        12, read_at="dream", toml="dream.essence_cap", parse=int
    )

    # Drift v2 — embedding-geometry dedup (concepts AND themes).
    # ``cosine_threshold``: pairs at/above go to the merge worker.
    # ``drift_cap``: max concept pairs surfaced per scan, cosine-ranked
    # (0 = unlimited). Judged pairs are excluded via the maintenance-log
    # verdicts history, so the cap bounds payload size without starving
    # the tail the way the old lexical [:5] slice did.
    dream_cosine_threshold: float = knob(
        0.8, read_at="dream", toml="dream.cosine_threshold", parse=float
    )
    dream_drift_cap: int = knob(15, read_at="dream", toml="dream.drift_cap", parse=int)

    # Grain coarsening (drift v2, N-ary). The tighten worker may collapse a
    # tight near-clique of fine concepts/themes onto one coarser term.
    # ``coarsen_threshold``: complete-linkage floor — stricter than the
    # pairwise ``cosine_threshold`` because a fold is destructive.
    # ``coarsen_cap``: clusters surfaced per scan, per family (cohesion-ranked).
    # ``coarsen_max_size``: bounds the greedy clique-grow.
    # ``coarsen_apply``: True = nightly applies the fold; False = surface-only
    # (the worker still judges and the verdict is recorded, but apply skips
    # the fold so the on-demand ``/tighten`` front door applies on approval).
    dream_coarsen_threshold: float = knob(
        0.85, read_at="dream", toml="dream.coarsen_threshold", parse=float
    )
    dream_coarsen_cap: int = knob(
        3, read_at="dream", toml="dream.coarsen_cap", parse=int
    )
    dream_coarsen_max_size: int = knob(
        6, read_at="dream", toml="dream.coarsen_max_size", parse=int
    )
    dream_coarsen_apply: bool = knob(
        True, read_at="dream", toml="dream.coarsen_apply", parse=bool
    )

    # Max folded hubs the phase-2 seam-link worker drains per cycle.
    dream_seam_link_cap: int = knob(
        10, read_at="dream", toml="dream.seam_link_cap", parse=int
    )

    # Promotion gate (proposed → canonical concepts). ``threshold`` is the
    # min proposed-concept count for promotion eligibility; ``cap`` bounds
    # how many candidates one scan surfaces. The ``weave dream scan``
    # ``--promotion-{threshold,cap}`` flags override per-invocation; these
    # fields steer cron (which passes no flags).
    dream_promotion_threshold: int = knob(
        5, read_at="dream", toml="dream.promotion_threshold", parse=int
    )
    dream_promotion_cap: int = knob(
        20, read_at="dream", toml="dream.promotion_cap", parse=int
    )

    # Probe-pressure lookback window (days). Read by BOTH probe surfaces in
    # the dream scan — the ``recent_probes`` payload (priority worker) and
    # the knowledge-delta probe-match slice (digest worker).
    dream_probe_window_days: int = knob(
        14, read_at="dream", toml="dream.probe_window_days", parse=int
    )

    # Behavioral-salience activity window (days) for the digest's
    # "## Most actionable" active-focus block: which projects saw recent
    # sessions and which concepts are under probe pressure. The automatic
    # signal is the default; PRIORITIES.yaml ``focus.*`` lists are appended
    # as optional pins (a pinned-but-quiet project/concept still surfaces).
    salience_activity_window_days: int = knob(
        14, read_at="dream", toml="salience.activity_window_days", parse=int
    )

    # How many rejudge entries one cycle hands to the phase-2 judge worker.
    # Shared by the scan collector AND apply's consumption step — apply
    # removes exactly this prefix of the on-disk queue, so anything beyond
    # the cap survives for the next cycle.
    dream_rejudge_cap: int = knob(
        20, read_at="dream", toml="dream.rejudge_cap", parse=int
    )

    # Trajectory outcome judge (issue #60 — the phase-2 ``dream-outcome-worker``).
    # ``phase2_days``: the closed-horizon delay before the delayed-signal
    # (rework-blame + revert) pass runs, measured from the PR's merge date.
    # ``rework_threshold``: the fraction of a merged diff rewritten within that
    # window above which phase-2 rules ``reworked-post-merge`` (a categorical
    # label — the raw blame counts are always recorded on the entry, never a
    # composite score). ``agent_identities``: substrings that mark a PR commit
    # as agent-produced (matched against author login/email/name, incl.
    # co-authors) — loop commits carry the Claude co-author, so a pure-human
    # rework commit lacking it flips merged-clean → reworked.
    dream_trajectory_phase2_days: int = knob(
        14, read_at="dream", toml="dream.trajectory_phase2_days", parse=int
    )
    dream_trajectory_rework_threshold: float = knob(
        0.5, read_at="dream", toml="dream.trajectory_rework_threshold", parse=float
    )
    dream_trajectory_agent_identities: tuple[str, ...] = knob(
        ("claude", "noreply@anthropic.com"),
        read_at="dream",
        toml="dream.trajectory_agent_identities",
        parse=_parse_identities,
    )

    # Evidence-gated steering (issue #62 — the gate the slow self-improvement
    # loop #61 calls before filing proposals). ``weekly_budget``: max proposals
    # filed per run, ranked by evidence weight — the anti-invention cap.
    # ``steering_weights``: per-signal multipliers for the evidence weight (raw
    # counts are always preserved; the weights only rank). Empty by default so
    # every signal weighs 1.0 (see operations/steering.DEFAULT_WEIGHTS); the
    # ``[steering]`` TOML section fills it from ``weight_<signal>`` keys.
    steering_weekly_budget: int = knob(
        3, read_at="dream", toml="steering.weekly_budget", parse=int
    )
    steering_weights: dict = knob(
        default_factory=dict,
        read_at="dream",
        toml="steering.weight_*",
        parse=float,
    )

    # Knowledge-delta window (hours) for the phase-2 digest worker.
    dream_knowledge_delta_hours: int = knob(
        24, read_at="dream", toml="dream.knowledge_delta_hours", parse=int
    )

    # Catalyst entries shipped per essence candidate: ``max_catalysts``
    # for substantive essences, ``placeholder_max_catalysts`` for
    # placeholder ones (which need more material to compose fresh).
    dream_essence_max_catalysts: int = knob(
        10, read_at="dream", toml="dream.essence_max_catalysts", parse=int
    )
    dream_essence_placeholder_max_catalysts: int = knob(
        25, read_at="dream", toml="dream.essence_placeholder_max_catalysts", parse=int
    )

    # Memory seam (CC auto-memory ↔ vault reconciliation, phase-2
    # ``dream-seam-worker``). ``cosine_twin`` / ``cosine_none`` are the
    # calibrated bands the worker reads off ``weave_search(mode='similar')``
    # (≥twin = real twin, <none = no twin; the gap is an LLM read).
    # ``stale_age_days`` is the project-type stale prior (a ``project`` CC
    # fact untouched this long is a stale-state risk). ``recheck_days``
    # re-validates resolved verdicts periodically so vault drift is caught.
    # ``cap`` bounds how many dirty facts one cycle hands the worker.
    seam_cosine_twin: float = knob(
        0.70, read_at="dream", toml="seam.cosine_twin", parse=float
    )
    seam_cosine_none: float = knob(
        0.55, read_at="dream", toml="seam.cosine_none", parse=float
    )
    seam_stale_age_days: int = knob(
        30, read_at="dream", toml="seam.stale_age_days", parse=int
    )
    seam_recheck_days: int = knob(
        14, read_at="dream", toml="seam.recheck_days", parse=int
    )
    seam_cap: int = knob(20, read_at="dream", toml="seam.cap", parse=int)

    # Extraction — max insight notes one ``weave_extract`` call creates.
    extract_insights_cap: int = knob(
        3, read_at="wrap", toml="extract.insights_cap", parse=int
    )

    # Inline backfill fan-out — the keyless ``weave import claude-code --enrich
    # --via inline`` path (/seed-enrich). At/below ``fanout_threshold`` pending
    # sessions it synthesises in-process (no spawn overhead — see the
    # "no subagent for small wraps" finding); above it, it deterministically
    # fans out subagents, ``batch_size`` sessions per worker, ``parallelism``
    # workers concurrent (mirrors /drain's writer fan-out). Distinct from the
    # API batch path's provider ``batch_concurrency`` (vault/config/api.yaml).
    enrich_fanout_threshold: int = knob(
        12, read_at="index", toml="enrich.fanout_threshold", parse=int
    )
    enrich_batch_size: int = knob(
        6, read_at="index", toml="enrich.batch_size", parse=int
    )
    enrich_parallelism: int = knob(
        3, read_at="index", toml="enrich.parallelism", parse=int
    )

    # Theme cluster detection (synthesis/theme_candidates.detect_signals).
    # ``min_cluster_size``: smallest concept cluster that surfaces a signal;
    # ``recent_days``: event-grain source lookback; ``min_shared_concepts``:
    # concepts a concept-cluster's sources must share; ``name_family_jaccard``:
    # token-Jaccard at/above which two ``proposed_theme`` slugs join one arc
    # family; ``generic_concept_ratio``: concepts on more than this fraction
    # of the recent pool are "generic" and dropped from covering-theme scoring.
    theme_min_cluster_size: int = knob(
        3, read_at="dream", toml="themes.min_cluster_size", parse=int
    )
    theme_recent_days: int = knob(
        30, read_at="dream", toml="themes.recent_days", parse=int
    )
    theme_min_shared_concepts: int = knob(
        2, read_at="dream", toml="themes.min_shared_concepts", parse=int
    )
    theme_name_family_jaccard: float = knob(
        0.5, read_at="dream", toml="themes.name_family_jaccard", parse=float
    )
    theme_generic_concept_ratio: float = knob(
        0.5, read_at="dream", toml="themes.generic_concept_ratio", parse=float
    )

    # Deterministic staleness auto-resolve: an ``active`` theme whose newest
    # catalyst-log entry (or, for an empty stub, its created date) is older
    # than this many days is auto-marked ``resolved`` by the dream apply
    # phase. 0 disables. The only automatic theme-lifecycle trigger — and
    # mechanically observable (no semantic inference).
    theme_resolve_after_days: int = knob(
        60, read_at="dream", toml="themes.resolve_after_days", parse=int
    )

    # Landing docs — ``open_probes_cap``: classified prompt-probes gathered
    # into the landing context; ``probes_display_cap``: probes the rendered
    # STATE doc's "Open Probes" section displays.
    landing_open_probes_cap: int = knob(
        20, read_at=("wrap", "weave_call"), toml="landing.open_probes_cap", parse=int
    )
    landing_probes_display_cap: int = knob(
        10, read_at=("wrap", "weave_call"), toml="landing.probes_display_cap", parse=int
    )

    # RRF fusion constant for hybrid search (Σ 1/(k + rank)). 60 is the
    # standard constant from the original RRF paper.
    retrieval_rrf_k: int = knob(
        60, read_at="weave_call", toml="retrieval.rrf_k", parse=int
    )

    # `weave health` (#120): a scheduled job is stale once its last-run
    # evidence is older than cadence × stale_factor; a queue item is backlog
    # once it has sat unclaimed for more than backlog_days.
    health_stale_factor: float = knob(
        1.5, read_at="dream", toml="health.stale_factor", parse=float
    )
    health_backlog_days: int = knob(
        7, read_at="dream", toml="health.backlog_days", parse=int
    )

    # R2 — prompt-time retrieval enrichment (see PromptTimeRetrieval).
    retrieval_prompt_time: PromptTimeRetrieval = knob(
        default_factory=PromptTimeRetrieval,
        read_at="UserPromptSubmit",
        toml="retrieval.prompt_time",
    )

    # The SessionStart payload's shape (#352; see ContextStartup).
    context_startup: ContextStartup = knob(
        default_factory=lambda: ContextStartup(),
        read_at=("SessionStart", "weave_call"),
        toml="context.startup",
    )

    # Hook behaviour: budgets, the context cap, the Stop seams (see Hooks).
    hooks: Hooks = knob(
        default_factory=Hooks,
        read_at=("SessionStart", "UserPromptSubmit", "wrap"),
    )

    # System 1 question sets by name (see Verdicts).
    verdicts: Verdicts = knob(default_factory=Verdicts, read_at=_VERDICT_EVENTS)

    def question_set(self, name: str) -> QuestionSet:
        """The named question set's policy; an unconfigured question set is off."""
        return self.verdicts.question_set.get(name) or QuestionSet()

    @property
    def weave_dir(self) -> Path:
        if self.weave_dir_override is not None:
            return self.weave_dir_override
        return self.vault_root / ".weave"

    @property
    def config_dir(self) -> Path:
        return self.vault_root / "config"

    @property
    def index_db(self) -> Path:
        return self.weave_dir / "index.db"

    @property
    def embeddings_db(self) -> Path:
        return self.weave_dir / "embeddings.db"

    @property
    def config_path(self) -> Path:
        """Vault-internal tunables file (embedding / edge / dream knobs).

        Canonical home is ``vault/config/config.toml`` — alongside the other
        user-editable config (2026-06-13: moved out of ``.weave/`` so the whole
        config surface lives in one folder). A pre-move file at
        ``vault/.weave/config.toml`` is still read as a transparent fallback;
        move it to ``config/`` when convenient.
        """
        canonical = self.config_dir / "config.toml"
        if canonical.exists():
            return canonical
        legacy = self.weave_dir / "config.toml"
        if legacy.exists():
            return legacy
        return canonical  # neither exists — return canonical so writes commit forward

    @property
    def templates_dir(self) -> Path:
        return self.vault_root / "templates"


class LegacyConfigLocationError(RuntimeError):
    """Raised when a user-editable config still sits at the deprecated
    ``vault/.weave/<filename>`` path. Run ``scripts/move_configs_to_config_dir.sh``
    or ``mv`` the file to ``vault/config/<filename>``.
    """


def resolve_config_file(vault_root: Path, filename: str) -> Path:
    """Resolve a user-editable config file path under ``vault/config/``.

    Resolution rules:
    1. Canonical path (``<vault_root>/config/<filename>``) exists → return it.
    2. Only legacy path (``<vault_root>/.weave/<filename>``) exists → raise
       :class:`LegacyConfigLocationError`. The legacy fallback was retired
       in Phase 3.1B (2026-06-05); user is expected to move the file.
    3. Neither exists → return canonical path (writes commit forward).

    The returned path may not exist — the caller is responsible for the
    missing-file check.
    """
    new = vault_root / "config" / filename
    if new.exists():
        return new
    legacy = vault_root / ".weave" / filename
    if legacy.exists():
        raise LegacyConfigLocationError(
            f"{filename} still lives at vault/.weave/{filename}. "
            f"Move it to vault/config/{filename} "
            f"(e.g. `mv {legacy} {new}`) — the legacy fallback was retired "
            f"in Phase 3.1B."
        )
    return new  # neither exists — return canonical so writes commit forward


def user_config_path() -> Path:
    """Path to the user-scope thinkweave config, idiomatic per-OS.

    Resolution order for the base dir:
    1. ``$XDG_CONFIG_HOME`` when set (honoured on every OS — some Windows
       users export it deliberately).
    2. Windows: ``%APPDATA%`` (e.g. ``C:\\Users\\x\\AppData\\Roaming``).
    3. Otherwise: ``~/.config``.

    The final file is ``<base>/thinkweave/config.toml``. This is the tier
    ``/onboard`` writes to when persisting the user's chosen vault root —
    the seam that lets the plugin path work without a shell-rc edit. Reader
    (:func:`_load_user_config_vault_root`) and writer
    (:func:`write_user_config`) both go through this one function, so the
    per-OS branch can never drift between them.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg)
    elif _is_windows() and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path.home() / ".config"
    return base / "thinkweave" / "config.toml"


def user_cache_dir() -> Path:
    """Platform cache base for thinkweave runtime artifacts (cron/Task
    Scheduler logs).

    Resolution order mirrors :func:`user_config_path`:
    1. ``$XDG_CACHE_HOME`` when set.
    2. Windows: ``%LOCALAPPDATA%`` (e.g. ``C:\\Users\\x\\AppData\\Local``).
    3. Otherwise: ``~/.cache``.

    Returns ``<base>/thinkweave`` (underscore form, matching the historic
    ``~/.cache/thinkweave`` layout the example crontab logs into).
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        base = Path(xdg)
    elif _is_windows() and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    else:
        base = Path.home() / ".cache"
    return base / "thinkweave"


def write_user_config(vault_root: Path) -> None:
    """Atomically persist ``vault_root`` to the user-scope config file
    (via :func:`thinkweave.core.fswrite.atomic_write_text`).

    The file shape is intentionally minimal — one key — because the
    vault-internal ``config.toml`` (tier 3, at ``vault/config/config.toml``)
    remains the home for embedding / edge / dream fields. This tier only
    ever sets the path.
    """
    # The value MUST be escaped, not interpolated. A Windows vault root
    # (``C:\\Users\\me\\vault``) dropped raw into a TOML *basic* string reads
    # ``\\U`` as the start of an 8-hex-digit unicode escape, so tomllib rejects
    # the file we just wrote with "Invalid hex value" — /onboard would persist a
    # config that nothing can load back. ``json.dumps`` is the same mechanism
    # ``core/mcp_config.py:_toml_scalar`` already relies on: JSON's escape set
    # is a subset of TOML's basic-string escapes, and ``ensure_ascii=False``
    # keeps a non-BMP character (an emoji in a vault path) as a literal rather
    # than a surrogate pair, which TOML rejects as not a Unicode scalar value.
    payload = f"vault_root = {json.dumps(str(vault_root), ensure_ascii=False)}\n"
    atomic_write_text(user_config_path(), payload)


def is_vault_initialized(cfg: Config) -> bool:
    """True iff the vault's canonical ``config/sources.yaml`` exists.

    Single canonical predicate for "is this vault wired up?" — used by
    the hook handler's early-return gate (replaces the bash gate in
    ``hooks/hooks.json``) and by ``/onboard``'s idempotency checks.
    Phase-3.1 moved this file from ``vault/.weave/sources.yaml`` to
    ``vault/config/sources.yaml``; the predicate tracks the canonical
    location only — legacy paths are not honoured.
    """
    try:
        (cfg.vault_root / "config" / "sources.yaml").stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _load_user_config() -> dict:
    """Read the user-scope config document (``vault_root``, ``rsi_rail``).

    A missing file is the normal case and yields ``{}``. A file that does
    not parse raises with its path: it may name the policy repo, so a
    silent fall-through would serve the vault's values in its place.
    """
    path = user_config_path()
    if not path.exists():
        return {}
    try:
        return _read_toml(path)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"user-scope config {path} is not valid TOML: {e}") from e


def _read_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _apply_knobs(cfg: Config, data: dict, path: Path | None = None) -> None:
    """Apply one config.toml document's knob sections onto ``cfg``.

    Shared by the vault file and the rsi-rail repo file so both tiers
    read every key the same way; ``vault_root`` is the caller's. Each key's
    address and coercion come from the field's metadata (:func:`knob`).
    ``path`` names the file in the refusal of an unknown section name.
    """
    _apply_fields(cfg, data, "", cfg, path)


def _rail_tables(root: type | None = None) -> dict[str, frozenset[str]]:
    """The tables the rail file may carry and their keys, derived from the
    knob metadata; every ancestor of a table is listed so the walk can
    descend into it."""
    tables: dict[str, set[str]] = {}
    for table, key in iter_rail_addresses(root):
        tables.setdefault(table, set()).add(key)
        parts = table.split(".")
        for i in range(len(parts)):
            tables.setdefault(".".join(parts[:i]), set())
    return {t: frozenset(keys) for t, keys in tables.items()}


def _rail_section_labels(root: type | None = None) -> list[str]:
    """The rail tables as the refusal names them, a keyed table once with
    its placeholder (``[context.startup.<section>]``)."""
    labels: list[str] = []
    for spec in knob_specs(root):
        if spec.rail and spec.address is not None:
            label = f"[{spec.address.rpartition('.')[0]}]"
            if label not in labels:
                labels.append(label)
    return labels


def _check_rail_keys(data: dict, path: Path) -> None:
    """Raise ``ValueError`` naming the first key in the rail file that is not
    a serving-chain knob, so a typo or a vault-side key placed in the repo
    fails loudly instead of being silently ignored. The allowed set is
    derived from the knob metadata (dec-db6baaab), never listed by hand."""
    tables = _rail_tables()

    def walk(table: dict, prefix: str) -> None:
        allowed = tables.get(prefix, frozenset())
        for key, value in table.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict) and dotted in tables:
                walk(value, dotted)
            elif key not in allowed:
                sections = ", ".join(_rail_section_labels())
                raise ValueError(
                    f"unknown key {dotted!r} in {path}: the rsi-rail file "
                    f"carries only the serving-chain knobs ({sections}); move "
                    "it to the vault's config.toml or fix the spelling"
                )

    walk(data, "")


def load_config() -> Config:
    """Load config from env vars, user-scope TOML, vault-internal TOML, defaults.

    Vault-root precedence (high → low):
    1. ``THINKWEAVE_VAULT`` env var
    2. ``~/.config/thinkweave/config.toml`` (or ``$XDG_CONFIG_HOME``)
    3. ``vault/config/config.toml`` (fallback: legacy ``vault/.weave/config.toml``)
    4. Built-in default (``~/vault``)

    Knob flow: vault ``config.toml`` → rsi-rail ``thinkweave/config.toml``
    (when ``THINKWEAVE_RSI_RAIL`` or the user-scope ``rsi_rail`` key names
    the repo) → per-field env overrides.
    """
    cfg = Config()

    # Tier 1: env var (highest priority — preserves override-everything)
    # PERSONAL_MEM_VAULT is the pre-rename name, honoured as a migration
    # fallback (rename → thinkweave, 2026-06-13); drop once shells are updated.
    vault_env = os.environ.get("THINKWEAVE_VAULT") or os.environ.get("PERSONAL_MEM_VAULT")
    # Tier 2: user-scope TOML (~/.config/thinkweave/config.toml).
    # Only sets vault_root; embedding/edge/dream fields stay owned
    # by the vault-internal config at tier 3.
    user_cfg = _load_user_config()
    user_vault = (
        Path(user_cfg["vault_root"])
        if user_cfg.get("vault_root") and not vault_env
        else None
    )
    if vault_env:
        cfg.vault_root = Path(vault_env)
    elif user_vault is not None:
        cfg.vault_root = user_vault

    # Tier 3: vault-internal config.toml. Only sets vault_root when
    # neither tier 1 (env) nor tier 2 (user-config) provided one; every
    # other key is always honoured.
    toml_path = cfg.config_path
    if toml_path.exists():
        data = _read_toml(toml_path)
        if "vault_root" in data and not vault_env and user_vault is None:
            cfg.vault_root = Path(data["vault_root"])
        _apply_knobs(cfg, data, toml_path)

    # Tier 4: the rsi-rail policy repo. Harness-level knobs the user tunes
    # live there and win over the vault file; a configured repo whose file
    # is missing is an error, never a silent fall-through.
    rail = os.environ.get("THINKWEAVE_RSI_RAIL") or user_cfg.get("rsi_rail")
    if rail:
        cfg.rsi_rail = Path(rail).expanduser()
        rail_toml = cfg.rsi_rail / "thinkweave" / "config.toml"
        if not rail_toml.exists():
            raise FileNotFoundError(
                f"rsi-rail is set to {cfg.rsi_rail} but {rail_toml} does not "
                "exist; create it or unset THINKWEAVE_RSI_RAIL / the "
                "user-scope rsi_rail key"
            )
        rail_data = _read_toml(rail_toml)
        _check_rail_keys(rail_data, rail_toml)
        _apply_knobs(cfg, rail_data, rail_toml)

    # Per-field env overrides. PERSONAL_MEM_* are the pre-rename names,
    # honoured as migration fallbacks (rename → thinkweave, 2026-06-13).
    project_env = os.environ.get("THINKWEAVE_PROJECT") or os.environ.get("PERSONAL_MEM_PROJECT")
    if project_env:
        cfg.default_project = project_env
    weave_dir_env = os.environ.get("THINKWEAVE_WEAVE_DIR")
    if weave_dir_env:
        cfg.weave_dir_override = _resolve_weave_dir_value(weave_dir_env, cfg.vault_root)

    cfg.default_project = normalize_project_name(cfg.default_project)
    return cfg


_PROJECT_WORKTREE_PREFIX_RE = re.compile(r"^(mp|qdi)_")


def normalize_project_name(name: str) -> str:
    """Canonicalize a project name: lowercase, dashes/spaces -> underscores,
    strip a leading personal-worktree prefix (``mp-``/``mp_``/``qdi-``/``qdi_``).

    Prevents duplicate projects that differ only by separator, case, or
    worktree-naming convention (``trade-ideas`` vs ``trade_ideas`` vs
    ``mp-trade_ideas``, all personal clones of the same shared checkout).
    Empty / None pass through as "". Applied at every boundary where a
    project name enters the system — config load and
    ``VaultManager.create_note`` — so a stray dash or ``mp-`` worktree
    prefix can never mint a second project folder. Mirrors the prefix-strip
    that ``personal_mem``'s importer/hook already did before the rename;
    this is the port's missing half.
    """
    if not name:
        return ""
    normalized = name.strip().lower().replace("-", "_").replace(" ", "_")
    stripped = _PROJECT_WORKTREE_PREFIX_RE.sub("", normalized)
    return stripped or normalized


def worktree_repo_root(cwd: str) -> str:
    """``cwd`` with any ``.claude/worktrees/<branch>[/...]`` tail stripped.

    Separators come back normalized to ``/``, so a Windows cwd parses the
    same as a POSIX one.
    """
    parts = PurePosixPath(cwd.replace("\\", "/")).parts
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return str(PurePosixPath(*parts[:i])) if i else ""
    return cwd.replace("\\", "/")


_EPHEMERAL_CWD_RE = re.compile(r"^(agent-[a-f0-9]{12,}|[a-f0-9-]{32,})$")


def detect_project(cwd: str = "") -> str:
    """The live project for a working directory (default: the process cwd).

    Priority: THINKWEAVE_PROJECT env var > git repo name > cwd directory name,
    normalized the way every vault write normalizes it (``tw-dogfood`` is
    stored as ``tw_dogfood``), so reads find what writes stored.
    """
    raw = _raw_project(cwd or os.getcwd())
    return raw if raw == UNSCOPED_PROJECT else normalize_project_name(raw)


def tracker_project(ref: str, vault_root: Path) -> str:
    """The vault project a ``github:<owner>/<repo>#<n>`` ref's repo maps to,
    or ``""`` for any other ref or a repo with no project in the vault."""
    issue = GithubIssue.parse(ref)
    if not issue:
        return ""
    name = normalize_project_name(issue.repo.rpartition("/")[2])
    return name if (vault_root / "projects" / name).is_dir() else ""


def _raw_project(cwd: str) -> str:
    """The project name before normalization: env var, git repo, or cwd.

    A ``.claude/worktrees/<name>`` cwd resolves to its parent repo, never to
    the worktree's own directory name. When cwd looks ephemeral (e.g. ``agent-a4701018f1189051e/`` from a
    cloud-agent run, or a bare UUID), fall through to ``_unscoped`` instead
    of letting the runtime's session-id leak in as a project name.
    """
    # PERSONAL_MEM_PROJECT: pre-rename migration fallback (→ thinkweave 2026-06-13).
    env_proj = os.environ.get("THINKWEAVE_PROJECT") or os.environ.get("PERSONAL_MEM_PROJECT")
    if env_proj:
        return env_proj

    cwd_path = Path(worktree_repo_root(cwd) or cwd)

    # Walk up to find a .git directory — use that repo's directory name
    for parent in [cwd_path, *cwd_path.parents]:
        if (parent / ".git").exists():
            return parent.name
        if parent == parent.parent:
            break

    if _EPHEMERAL_CWD_RE.match(cwd_path.name):
        return UNSCOPED_PROJECT
    return cwd_path.name


def normalize_project(cwd: str) -> str:
    """Derive a vault project name from a coding-agent session's cwd.

    Shared by every session importer (Claude Code, Codex) so a repo worked on
    from more than one harness lands in a single vault project. Three steps:

    1. Strip a trailing ``.claude/worktrees/<branch>`` (worktree sessions; we
       want the repo root, not the branch name).
    2. Drop sessions whose cwd is the homedir or ``~/.claude`` itself —
       they map to ``_unscoped``.
    3. Take basename → lowercase → ``-`` → ``_``.
    """
    if not cwd:
        return UNSCOPED_PROJECT

    # Normalize separators up front so a Windows cwd (``C:\\Users\\x\\repo``)
    # and a POSIX cwd parse identically, regardless of which OS runs the
    # import. PurePosixPath then gives consistent ``.parts`` semantics on
    # the normalized string (a drive like ``C:`` becomes a leading part).
    parts = list(PurePosixPath(worktree_repo_root(cwd).rstrip("/")).parts)

    # Drop sessions whose cwd is the homedir, ``~/.claude``, or the root.
    home_parts = PurePosixPath(str(Path.home()).replace("\\", "/")).parts
    if (
        not parts
        or tuple(parts) == home_parts
        or tuple(parts) == home_parts + (".claude",)
    ):
        return UNSCOPED_PROJECT

    name = parts[-1]
    # Guard a bare drive root (``C:``) and dotfile dirs.
    if not name or name.startswith(".") or name.endswith(":"):
        return UNSCOPED_PROJECT

    name = name.lower().replace("-", "_")
    name = re.sub(r"_{2,}", "_", name).strip("_")
    return name or UNSCOPED_PROJECT
