"""AI-65 — retrieval score threshold: class materials (KB) vs Internet.

The dispatcher always runs class-scoped retrieval first (AC-01). The best
cosine score is compared with the admin-configured threshold (AC-05, default
``0.8``): a score ``>= threshold`` answers from the curriculum with citations
(AC-02); a lower score hands the question to the Internet source agent
(AC-03). Generation receives only numbered sources, so the LLM can never
answer from its own knowledge (AC-04) — when no source exists, a deterministic
message is returned instead of an invented answer.

The Internet provider lives here too: AI-67 owns the T19 agent, implemented with
SerpApi (Google organic results) plus the shared source-reputation tiers. AI-61
consumes :func:`answer_question` for the streaming chat endpoint; this module
owns the routing decision and the source contract.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from langchain_core.documents import Document
from sqlalchemy.orm import Session

from app.config import search_config, settings
from app.core.prompt_manager import get_prompt_manager
from app.models.system_config import SystemConfig
from app.services import citation_service, document_service
from app.services.ingestion.chunker import build_chunk_context

logger = logging.getLogger(__name__)

SOURCE_CURRICULUM = "curriculum"
SOURCE_INTERNET = "internet"

RETRIEVAL_THRESHOLD_KEY = "retrieval_threshold"
DEFAULT_RETRIEVAL_THRESHOLD = float(settings.retrieval_threshold)
UNRELATED_THRESHOLD_KEY = "unrelated_threshold"
DEFAULT_UNRELATED_THRESHOLD = 0.45
MIN_RETRIEVAL_THRESHOLD = 0.0
MAX_RETRIEVAL_THRESHOLD = 1.0

SOURCE_UNRELATED = "unrelated"

# AC-03 returns a normal answer, never a ticket; AC-04 forbids inventing one
# when no source exists, so this is the only truthful answer left.
NO_SOURCE_MESSAGE = (
    "I could not find this question in your class materials, and the Internet "
    "search agent did not return a usable source. Please rephrase the question "
    "or ask your trainer to add the missing material."
)

CITATION_SNIPPET_LENGTH = 280


class RetrievalThresholdError(ValueError):
    """The configured retrieval threshold is not a number in [0, 1]."""


class VectorStore(Protocol):
    """Minimal vector-store contract used by the dispatcher (injectable in tests)."""

    def similarity_search_with_score(
        self,
        query: str,
        k: int | None = None,
        *,
        class_id: str | None = None,
    ) -> list[tuple[Document, float]]:
        """Return ``(chunk, score)`` pairs for one class, best first."""


@dataclass(frozen=True)
class ScoredChunk:
    """One retrieved curriculum chunk and its similarity score."""

    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def document_id(self) -> str:
        """AI-39 guard key: chunks without a live document are unusable."""

        return str(self.metadata.get("document_id", ""))

    @property
    def title(self) -> str:
        """Human-readable source name shown in citations."""

        return str(
            self.metadata.get("file_name")
            or self.metadata.get("source")
            or self.metadata.get("title")
            or "Course material"
        )

    @property
    def page(self) -> int | None:
        """Page/slide locator when the ingestion pipeline recorded one."""

        raw = self.metadata.get("page_number", self.metadata.get("page"))
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class WebResult:
    """One Internet source returned by the T19 agent."""

    title: str
    url: str
    content: str


@dataclass(frozen=True)
class Source:
    """Citation attached to an answer (E5 contract)."""

    index: int
    kind: str
    title: str
    url: str | None = None
    snippet: str = ""
    document_id: str | None = None
    page: int | None = None
    # Answer markers ([n]) this page backs, and the exact passages used.
    indexes: tuple[int, ...] = ()
    highlights: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalDecision:
    """Routing decision produced before any answer is generated."""

    question: str
    class_id: str
    threshold: float
    top_score: float | None
    source: str
    chunks: tuple[ScoredChunk, ...] = ()
    unrelated_threshold: float = DEFAULT_UNRELATED_THRESHOLD
    cluster_count: int = 0
    margin: float = 0.0


@dataclass(frozen=True)
class QaAnswer:
    """Grounded answer plus the citations and routing metadata."""

    answer: str
    source: str
    used_web_search: bool
    threshold: float
    top_score: float | None
    citations: tuple[Source, ...] = ()


# --- AC-05: admin-configurable threshold -----------------------------------


def get_retrieval_threshold(db: Session) -> float:
    """Return the active threshold, falling back to the AI-107 default ``0.8``.

    A missing, malformed, or out-of-range stored value never breaks the chat:
    the documented default applies until an admin saves a valid number.
    """

    if not hasattr(db, "query"):
        return DEFAULT_RETRIEVAL_THRESHOLD

    row = db.query(SystemConfig).filter(SystemConfig.key == RETRIEVAL_THRESHOLD_KEY).first()
    if row is None:
        return DEFAULT_RETRIEVAL_THRESHOLD
    try:
        value = float(row.value)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r; using default.", RETRIEVAL_THRESHOLD_KEY, row.value)
        return DEFAULT_RETRIEVAL_THRESHOLD
    if not math.isfinite(value) or not MIN_RETRIEVAL_THRESHOLD <= value <= MAX_RETRIEVAL_THRESHOLD:
        logger.warning("Ignoring out-of-range %s=%r; using default.", RETRIEVAL_THRESHOLD_KEY, row.value)
        return DEFAULT_RETRIEVAL_THRESHOLD
    return value


def get_unrelated_threshold(db: Session) -> float:
    """Return the active lower threshold below which questions are deemed out of scope."""

    if not hasattr(db, "query"):
        return DEFAULT_UNRELATED_THRESHOLD

    row = db.query(SystemConfig).filter(SystemConfig.key == UNRELATED_THRESHOLD_KEY).first()
    if row is None:
        return DEFAULT_UNRELATED_THRESHOLD
    try:
        value = float(row.value)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r; using default.", UNRELATED_THRESHOLD_KEY, row.value)
        return DEFAULT_UNRELATED_THRESHOLD
    if not math.isfinite(value) or not MIN_RETRIEVAL_THRESHOLD <= value <= MAX_RETRIEVAL_THRESHOLD:
        logger.warning("Ignoring out-of-range %s=%r; using default.", UNRELATED_THRESHOLD_KEY, row.value)
        return DEFAULT_UNRELATED_THRESHOLD
    return value


def retrieval_threshold_config(db: Session) -> dict[str, Any]:
    """Serialize both thresholds for the Admin settings API."""

    row = db.query(SystemConfig).filter(SystemConfig.key == RETRIEVAL_THRESHOLD_KEY).first()
    threshold = get_retrieval_threshold(db)
    unrelated_threshold = get_unrelated_threshold(db)
    return {
        "threshold": threshold,
        "unrelated_threshold": unrelated_threshold,
        "default_threshold": DEFAULT_RETRIEVAL_THRESHOLD,
        "default_unrelated_threshold": DEFAULT_UNRELATED_THRESHOLD,
        "is_default": (
            threshold == DEFAULT_RETRIEVAL_THRESHOLD
            and unrelated_threshold == DEFAULT_UNRELATED_THRESHOLD
        ),
        "updated_at": getattr(row, "updated_at", None) if row is not None else None,
    }


def save_retrieval_threshold(
    db: Session,
    threshold: float,
    unrelated_threshold: float | None = None,
) -> float:
    """Persist new thresholds; the next question reads them immediately."""

    try:
        high = float(threshold)
    except (TypeError, ValueError) as error:
        raise RetrievalThresholdError("Threshold must be a number between 0 and 1.") from error
    if not math.isfinite(high) or not MIN_RETRIEVAL_THRESHOLD <= high <= MAX_RETRIEVAL_THRESHOLD:
        raise RetrievalThresholdError("Threshold must be a number between 0 and 1.")

    if unrelated_threshold is not None:
        try:
            low = float(unrelated_threshold)
        except (TypeError, ValueError) as error:
            raise RetrievalThresholdError("Unrelated threshold must be a number between 0 and 1.") from error
        if not math.isfinite(low) or not MIN_RETRIEVAL_THRESHOLD <= low <= MAX_RETRIEVAL_THRESHOLD:
            raise RetrievalThresholdError("Unrelated threshold must be a number between 0 and 1.")
        if low > high:
            raise RetrievalThresholdError("Lower threshold cannot be greater than upper threshold.")

        row_low = db.query(SystemConfig).filter(SystemConfig.key == UNRELATED_THRESHOLD_KEY).first()
        if row_low is None:
            db.add(SystemConfig(key=UNRELATED_THRESHOLD_KEY, value=str(low)))
        else:
            row_low.value = str(low)

    row_high = db.query(SystemConfig).filter(SystemConfig.key == RETRIEVAL_THRESHOLD_KEY).first()
    if row_high is None:
        db.add(SystemConfig(key=RETRIEVAL_THRESHOLD_KEY, value=str(high)))
    else:
        row_high.value = str(high)

    db.commit()
    logger.info("Retrieval thresholds updated: high=%.3f, low=%s.", high, unrelated_threshold)
    return high


# --- AC-01/AC-02: retrieval runs first -------------------------------------


def default_vector_store(db: Session | None = None):
    """Build the production class-scoped vector store lazily (needs API keys)."""

    from app.services.embedding.gemini_embedding import GeminiEmbedding
    from app.services.vector_store.qdrant_store import QdrantStore

    return QdrantStore(embeddings=GeminiEmbedding(db=db))


def retrieve_class_chunks(
    db: Session,
    *,
    question: str,
    class_id: uuid.UUID | str,
    store: VectorStore | None = None,
    top_k: int | None = None,
) -> list[ScoredChunk]:
    """Retrieve the top-k chunks of one class (AC-01) and drop dead documents.

    The Qdrant filter is applied on the ``class_id`` payload written by
    ``document_service.build_qdrant_payload``, so another class can never leak
    into an answer. AI-39's guard then removes chunks whose document was
    deleted or replaced before answer generation (AC-02 defense in depth).
    """

    class_key = str(class_id)
    vector_store = store or default_vector_store(db)
    pairs = vector_store.similarity_search_with_score(
        question,
        k=top_k or settings.retriever_top_k,
        class_id=class_key,
    )

    allowed = document_service.retrievable_document_ids(db, class_key)
    chunks: list[ScoredChunk] = []
    for document, raw_score in pairs:
        chunk = ScoredChunk(
            content=document.page_content,
            score=float(raw_score),
            metadata=dict(document.metadata or {}),
        )
        if chunk.document_id and chunk.document_id in allowed:
            chunks.append(chunk)
    return sorted(chunks, key=lambda item: item.score, reverse=True)


def decide_source(
    db: Session,
    *,
    question: str,
    class_id: uuid.UUID | str,
    store: VectorStore | None = None,
    top_k: int | None = None,
) -> RetrievalDecision:
    """Run retrieval, then route between Curriculum, Internet, or Unrelated."""

    threshold_high = get_retrieval_threshold(db)
    threshold_low = get_unrelated_threshold(db)
    try:
        chunks = retrieve_class_chunks(
            db,
            question=question,
            class_id=class_id,
            store=store,
            top_k=top_k,
        )
    except Exception:  # retrieval outage must not refuse the request
        logger.warning(
            "Class retrieval failed; handing the question to the Internet agent (AI-65 AC-03).",
            exc_info=True,
        )
        chunks = []

    top_score = chunks[0].score if chunks else None
    if top_score is None:
        source = SOURCE_INTERNET
    elif top_score >= threshold_high:
        source = SOURCE_CURRICULUM
    elif top_score >= threshold_low:
        source = SOURCE_INTERNET
    else:
        source = SOURCE_UNRELATED

    return RetrievalDecision(
        question=question,
        class_id=str(class_id),
        threshold=threshold_high,
        unrelated_threshold=threshold_low,
        top_score=top_score,
        source=source,
        chunks=tuple(chunks) if source == SOURCE_CURRICULUM else (),
    )


# --- AC-03: Internet agent hand-off ---------------------------------------


_search_client_lock = threading.Lock()
_search_client: httpx.Client | None = None
SERPAPI_SEARCH_ENDPOINT = "https://serpapi.com/search.json"
SERPAPI_MAX_RESULTS = search_config.SERPAPI_MAX_RESULTS
SERPAPI_FETCH_RESULTS = search_config.SERPAPI_FETCH_RESULTS
_VIETNAMESE_CHARS = set("ăâđêôơưàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ")


def _get_search_client() -> httpx.Client:
    global _search_client
    with _search_client_lock:
        if _search_client is None or _search_client.is_closed:
            _search_client = httpx.Client(
                timeout=httpx.Timeout(settings.serpapi_timeout_seconds),
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
        return _search_client


def _is_vietnamese(text: str) -> bool:
    return any(char in _VIETNAMESE_CHARS for char in text.lower())


def _serpapi_params(question: str, api_key: str) -> dict[str, Any]:
    """Build Google search parameters localised to the question language.

    No ``gl`` is pinned: Google's global index ranks authoritative sources
    (docs, Stack Overflow, MDN, Real Python) above low-traffic blogs, which was
    the quality problem with the previous provider.
    """

    return {
        "engine": "google",
        "q": question,
        "api_key": api_key,
        "num": SERPAPI_FETCH_RESULTS,
        "hl": "vi" if _is_vietnamese(question) else "en",
    }


def _serpapi_web_results(
    data: dict[str, Any],
    jump_builder: Callable[[str, str], str] | None = None,
) -> list[WebResult]:
    """Map a SerpApi ``organic_results`` response to the T19 web-result contract."""

    web_results: list[WebResult] = []

    # Extract Google featured snippet / direct answer box if available
    answer_box = data.get("answer_box") or {}
    if isinstance(answer_box, dict) and answer_box:
        ab_title = str(answer_box.get("title") or "Google Quick Answer")
        ab_link = citation_service.normalize_search_url(str(answer_box.get("link") or ""))
        ab_snippet = str(
            answer_box.get("snippet")
            or answer_box.get("answer")
            or ""
        ).strip()
        if ab_snippet and ab_link:
            jump_url = jump_builder(ab_link, ab_snippet) if jump_builder is not None else ab_link
            web_results.append(WebResult(title=ab_title, url=jump_url, content=ab_snippet))

    for item in data.get("organic_results") or []:
        if len(web_results) >= SERPAPI_FETCH_RESULTS:
            break
        title = str(item.get("title") or "Web Source")
        raw_url = citation_service.normalize_search_url(str(item.get("link") or ""))
        snippet = str(item.get("snippet") or "").strip()

        rich_snippet = item.get("rich_snippet")
        content_parts = [snippet] if snippet else []
        if isinstance(rich_snippet, dict):
            for val in rich_snippet.values():
                if isinstance(val, dict):
                    for sub_k, sub_v in val.items():
                        if isinstance(sub_v, str) and sub_v.strip():
                            content_parts.append(f"{sub_k}: {sub_v.strip()}")
        content = " ".join(content_parts).strip()

        if not raw_url:
            continue
        jump_url = jump_builder(raw_url, content) if jump_builder is not None else raw_url
        web_results.append(WebResult(title=title, url=jump_url, content=content))
    return web_results


def search_internet(question: str) -> list[WebResult]:
    """T19 Internet source agent powered by SerpApi (Google Search) (AI-67).

    Uses Google's organic results so answers cite mainstream, well-maintained
    pages instead of the low-traffic sites a metered search API tends to return.
    """

    api_key = settings.serpapi_api_key
    if not api_key:
        logger.info("SERPAPI_API_KEY is not configured; returning no web results.")
        return []

    try:
        jump_builder = None
        try:
            from app.services.citation_service import build_universal_jump_url

            jump_builder = build_universal_jump_url
        except ImportError:
            pass

        response = _get_search_client().get(
            SERPAPI_SEARCH_ENDPOINT,
            params=_serpapi_params(question, api_key),
        )
        response.raise_for_status()
        web_results = _serpapi_web_results(response.json(), jump_builder)

        # Google does not accept an include/exclude domain list, so the shared
        # reputation tiers are applied locally: blocked sites are dropped and
        # the rest are ranked Tier 1 -> Tier 2 -> Tier 3 -> unknown.
        allowed = [item for item in web_results if not search_config.is_blocked(item.url)]
        allowed.sort(key=lambda item: search_config.get_source_tier(item.url) or 99)
        web_results = allowed[:SERPAPI_MAX_RESULTS]

        logger.info("SerpApi search returned %d results for %r", len(web_results), question)
        return web_results
    except Exception as exc:  # noqa: BLE001
        logger.warning("SerpApi search failed: %s", exc)
        return []


# --- AC-02/AC-04: grounded answer generation ------------------------------


GenerateAnswer = Callable[[str, str], str]
InternetSearch = Callable[[str], Sequence[WebResult]]


def answer_question(
    db: Session,
    *,
    question: str,
    class_id: uuid.UUID | str,
    store: VectorStore | None = None,
    internet_search: InternetSearch | None = None,
    generate: GenerateAnswer | None = None,
    top_k: int | None = None,
) -> QaAnswer:
    """Answer one question from a cited source (curriculum or Internet).

    AC-01: retrieval always runs first, even when the Internet path wins.
    AC-04: the model only sees numbered sources; an empty source list short
    circuits to :data:`NO_SOURCE_MESSAGE` without calling the LLM at all.
    """

    cleaned = question.strip()
    if not cleaned:
        raise ValueError("Question must not be empty.")

    decision = decide_source(
        db,
        question=cleaned,
        class_id=class_id,
        store=store,
        top_k=top_k,
    )

    if decision.source == SOURCE_UNRELATED:
        return _no_source_answer(decision)

    if decision.source == SOURCE_CURRICULUM:
        sources = curriculum_citations(decision.chunks)
        if not sources:
            return _no_source_answer(decision)
        context = "\n\n".join(
            _curriculum_context_line(index, chunk)
            for index, chunk in enumerate(decision.chunks, start=1)
        )
        text = _generate(
            generate,
            prompt_name="answer-course-prompt",
            context=context,
            question=cleaned,
        )
        answer = text or _source_only_answer(sources)
        return QaAnswer(
            answer=answer,
            source=SOURCE_CURRICULUM,
            used_web_search=False,
            threshold=decision.threshold,
            top_score=decision.top_score,
            citations=tuple(cited_page_citations(decision.chunks, answer, question=cleaned)),
        )

    searcher = internet_search or search_internet
    try:
        results = list(searcher(cleaned))
    except Exception:  # an agent outage must not refuse the request
        logger.warning("Internet source agent failed (AI-65 AC-03).", exc_info=True)
        results = []

    if not results:
        return _no_source_answer(decision)

    sources = internet_citations(results)
    context = "\n\n".join(
        f"[{source.index}] {source.title} — {source.url}\n{result.content}"
        for source, result in zip(sources, results, strict=True)
    )
    text = _generate(
        generate,
        prompt_name="answer-internet-prompt",
        context=context,
        question=cleaned,
    )
    answer = text or _source_only_answer(sources)
    used = cited_markers(answer)
    return QaAnswer(
        answer=answer,
        source=SOURCE_INTERNET,
        used_web_search=True,
        threshold=decision.threshold,
        top_score=decision.top_score,
        citations=tuple(source for source in sources if source.index in used),
    )


def curriculum_citations(chunks: Sequence[ScoredChunk]) -> list[Source]:
    """Build provisional source labels; final passages require answer markers."""

    return [
        Source(
            index=index,
            kind=SOURCE_CURRICULUM,
            title=chunk.title,
            snippet=chunk.content[:CITATION_SNIPPET_LENGTH],
            document_id=chunk.document_id or None,
            page=chunk.page,
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def cited_markers(answer: str) -> set[int]:
    """Return the ``[n]`` / ``[n, m]`` source markers the answer actually uses."""

    return citation_service.cited_markers(answer)


def cited_page_citations(
    chunks: Sequence[ScoredChunk],
    answer: str,
    max_citations: int = 3,
    *,
    question: str = "",
) -> list[Source]:
    """Citations for only the pages whose passages the answer referenced.

    Chunks from the same document page collapse into a single citation that
    carries every passage used (``highlights``) so the UI can mark them on
    that page. An answer without markers (greeting, "not covered") yields no
    citations at all.
    """
    passages = citation_service.extract_cited_passages(answer, chunks, question=question)

    grouped: dict[tuple[str, int | None], list[tuple[int, ScoredChunk]]] = {}
    for index, chunk in enumerate(chunks, start=1):
        if index not in passages:
            continue
        grouped.setdefault((chunk.document_id, chunk.page), []).append((index, chunk))

    sources: list[Source] = []
    for position, ((document_id, page), members) in enumerate(grouped.items(), start=1):
        first = members[0][1]
        highlights = tuple(dict.fromkeys(
            passage for index, _chunk in members for passage in passages[index]
        ))
        preview = highlights[0] if highlights else citation_service.extract_best_sentence(first.content)
        sources.append(
            Source(
                index=position,
                kind=SOURCE_CURRICULUM,
                title=first.title,
                snippet=preview[:CITATION_SNIPPET_LENGTH],
                document_id=document_id or None,
                page=page,
                indexes=tuple(index for index, _ in members),
                highlights=highlights,
            )
        )
    return sources[:max_citations]


def internet_citations(results: Sequence[WebResult], *, start_index: int = 1) -> list[Source]:
    """Build E5 citations from the Internet agent results (separate label)."""

    return [
        Source(
            index=index,
            kind=SOURCE_INTERNET,
            title=result.title,
            url=citation_service.normalize_search_url(result.url),
            snippet=result.content[:CITATION_SNIPPET_LENGTH],
        )
        for index, result in enumerate(results, start=start_index)
    ]


# --- Internals -------------------------------------------------------------


def _curriculum_context_line(index: int, chunk: ScoredChunk) -> str:
    locator = f", page {chunk.page}" if chunk.page is not None else ""
    content = build_chunk_context(chunk.content, chunk.metadata)
    return f"[{index}] {chunk.title}{locator}\n{content}"


def _no_source_answer(decision: RetrievalDecision) -> QaAnswer:
    return QaAnswer(
        answer=NO_SOURCE_MESSAGE,
        source=decision.source,
        used_web_search=decision.source == SOURCE_INTERNET,
        threshold=decision.threshold,
        top_score=decision.top_score,
        citations=(),
    )


def _source_only_answer(sources: Sequence[Source]) -> str:
    """Deterministic fallback when generation fails: quote sources, invent nothing."""

    lines = ["The answer could not be generated, but these sources answer the question:", ""]
    for source in sources:
        target = source.url or source.title
        lines.append(f"[{source.index}] {target}: {source.snippet}")
    return "\n".join(lines)


def _generate(
    generate: GenerateAnswer | None,
    *,
    prompt_name: str,
    context: str,
    question: str,
) -> str:
    """Call the injected generator (or Gemini) and never let failures hide sources."""

    generator = generate or generate_with_gemini
    try:
        system_prompt, user_prompt = _format_prompt(prompt_name, context, question)
        raw_text = generator(system_prompt, user_prompt).strip()
        # Clean heading hashes (#, ##, ###) into clean bold text
        cleaned_lines = []
        for line in raw_text.splitlines():
            heading_match = re.match(r"^#{1,6}\s+(.+)$", line)
            if heading_match:
                cleaned_lines.append(f"**{heading_match.group(1)}**")
            else:
                cleaned_lines.append(line)
        cleaned_text = "\n".join(cleaned_lines)

        # Strip inline URLs attached to citation markers: [1](http...) or [1] (http...) -> [1]
        cleaned_text = re.sub(r"\[(\d+)\]\s*\([^)]+\)", r"[\1]", cleaned_text)
        # Strip redundant outer parentheses around citations: ([1]) -> [1]
        cleaned_text = re.sub(r"\(\s*(\[\d+\])\s*\)", r"\1", cleaned_text)

        return cleaned_text
    except Exception:  # the request still returns cited sources
        logger.exception("Answer generation failed; returning source-only answer.")
        return ""


def _format_prompt(
    prompt_name: str,
    context: str,
    question: str,
    history: str = "",
) -> tuple[str, str]:
    prompt = get_prompt_manager().format(
        prompt_name,
        chunk=context,
        context=context,
        question=question,
        history=history,
    )
    return str(prompt["system-prompt"]), str(prompt["user-prompt"])


def generate_with_gemini(system_prompt: str, user_prompt: str) -> str:
    """Default generator: Gemini with the strict grounding prompt and key rotation."""

    from langchain_core.messages import HumanMessage, SystemMessage

    from app.services.llm import gateway

    return gateway.invoke_chat(
        None,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)],
    ).strip()
