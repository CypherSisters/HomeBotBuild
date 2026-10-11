"""
PuffPAI Knowledge Ingestion Pipeline

Handles ingestion of personal and general knowledge into the dual-zone
ChromaDB system:
  - Zone 1 (Vault): Personal documents, dictated info, scanned docs
  - Zone 2 (Intelligence): ZIM knowledge packs, shipped content

All personal data in Zone 1 is stored with encryption metadata and requires
authenticated session access. Routing is PII-aware and every document
carries source provenance.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import os
import re
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ContentSource(Enum):
    """How the content was added."""
    DOCUMENT_UPLOAD = "document_upload"     # PDF, TXT, etc.
    VOICE_DICTATION = "voice_dictation"     # From STT pipeline
    MANUAL_ENTRY = "manual_entry"           # Typed by user
    ZIM_PACK = "zim_pack"                   # From ZIM knowledge pack
    WEB_CLIP = "web_clip"                   # User-saved web content


class TargetZone(Enum):
    """Which ChromaDB collection the content goes into."""
    VAULT = "puffpai_vault"           # Zone 1: personal, auth-gated
    KNOWLEDGE = "puffpai_knowledge"   # Zone 2: general, open access


@dataclass
class IngestedDocument:
    """Metadata for a piece of ingested content."""
    doc_id: str
    title: str
    source: ContentSource
    target_zone: TargetZone
    chunk_count: int
    has_pii: bool
    ingested_at: str
    file_origin: Optional[str] = None  # original filename if from upload


@dataclass
class TextChunk:
    """A single chunk of text ready for embedding."""
    chunk_id: str
    doc_id: str
    text: str
    metadata: dict = field(default_factory=dict)


class KnowledgeIngestion:
    """Ingest documents, text, and ZIM packs into the dual-zone ChromaDB.

    Usage:
        ingestion = KnowledgeIngestion(vault_collection, knowledge_collection)

        # Personal document (goes to vault)
        doc = ingestion.ingest_text(
            "My doctor's name is Dr. Smith, phone 555-1234",
            title="Medical Notes",
            source=ContentSource.MANUAL_ENTRY,
        )

        # ZIM knowledge pack (goes to knowledge base)
        ingestion.ingest_zim("wikipedia_en.zim", max_articles=5000)
    """

    # Chunk size for splitting long documents
    CHUNK_SIZE = 800       # characters per chunk
    CHUNK_OVERLAP = 100    # overlap between chunks

    def __init__(self, vault_collection=None, knowledge_collection=None):
        """Initialize with ChromaDB collection handles.

        Args:
            vault_collection: ChromaDB collection for Zone 1 (personal data).
            knowledge_collection: ChromaDB collection for Zone 2 (general).
        """
        self._vault = vault_collection
        self._knowledge = knowledge_collection
        self.last_skipped = 0  # articles skipped by ingest_zim's resume filter

    # ── Text Ingestion ──────────────────────────────────────────────────────

    def ingest_text(self, text: str, title: str = "Untitled",
                    source: ContentSource = ContentSource.MANUAL_ENTRY,
                    force_zone: Optional[TargetZone] = None,
                    owner: Optional[str] = None) -> IngestedDocument:
        """Ingest a block of text into the appropriate zone.

        PII detection automatically routes personal data to the vault.

        Args:
            text: The text content to ingest.
            title: A title/label for this content.
            source: How this content was created.
            force_zone: Override automatic zone routing.
            owner: Multi-user (Increment 1) — the principal that owns this
                content. Stamped onto Zone-1 (vault) chunks only, so the
                query-side owner filter can restrict reads to that principal.
                Defaults to the household owner (how untagged legacy content is
                attributed); inert for Zone-2 and until a filtered read runs.

        Returns:
            IngestedDocument with metadata about what was stored.
        """
        doc_id = self._generate_doc_id(text)
        # PII detection only exists to auto-route content to the vault. When the
        # caller forces a zone (ZIM knowledge ingestion passes force_zone), the
        # result is unused metadata, so the 7-regex scan is pure waste on
        # million-article runs. Skip it. Non-forcing callers are unchanged.
        has_pii = self._detect_pii(text) if force_zone is None else False

        # Route to zone
        if force_zone:
            zone = force_zone
        elif has_pii or source in (ContentSource.VOICE_DICTATION,
                                    ContentSource.DOCUMENT_UPLOAD):
            zone = TargetZone.VAULT
        else:
            zone = TargetZone.KNOWLEDGE

        # Chunk the text
        chunks = self._chunk_text(text, doc_id, title, source, zone)

        # Multi-user (Increment 1): stamp the owning principal on Zone-1 chunks
        # so the query-side owner filter can restrict reads. Zone-2 chunks are
        # never stamped — general knowledge stays shared. Defaults to the
        # household owner (also how untagged legacy content is attributed).
        if zone == TargetZone.VAULT:
            try:
                from puffpai.security.multiuser import record_owner
                owner_tag = record_owner(owner)
            except Exception:
                owner_tag = 'owner'
            for c in chunks:
                c.metadata['owner'] = owner_tag

        # Store in ChromaDB
        collection = self._vault if zone == TargetZone.VAULT else self._knowledge
        if collection is not None:
            self._store_chunks(collection, chunks)

        return IngestedDocument(
            doc_id=doc_id, title=title, source=source,
            target_zone=zone, chunk_count=len(chunks),
            has_pii=has_pii,
            ingested_at=datetime.now().isoformat(),
        )

    # ── File Ingestion ──────────────────────────────────────────────────────

    def ingest_file(self, filepath: str,
                    source: ContentSource = ContentSource.DOCUMENT_UPLOAD,
                    force_zone: Optional[TargetZone] = None) -> IngestedDocument:
        """Ingest a file (TXT, PDF text layer, etc.) into the system.

        Args:
            filepath: Path to the file.
            source: How the file was provided.
            force_zone: Override automatic zone routing.

        Returns:
            IngestedDocument with metadata.
        """
        ext = os.path.splitext(filepath)[1].lower()
        title = os.path.basename(filepath)

        if ext == '.txt':
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
        elif ext == '.md':
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
        elif ext == '.pdf':
            text = self._extract_pdf_text(filepath)
        else:
            # Attempt to read as text
            try:
                with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except Exception:
                raise ValueError(f"Cannot read file type: {ext}")

        if not text.strip():
            raise ValueError(f"No text content extracted from {filepath}")

        doc = self.ingest_text(
            text, title=title, source=source, force_zone=force_zone)
        doc.file_origin = filepath
        return doc

    # ── ZIM Pack Ingestion ──────────────────────────────────────────────────

    # MIME types carrying indexable text. HTML is the bulk, but several packs in
    # the library keep their actual knowledge elsewhere: the TED packs store talk
    # transcripts as WebVTT subtitles (ted_mul_law: 2,202 vtt vs 123 html), and
    # fas-military-medicine is 25 PDFs behind a single HTML index page. Indexing
    # html only made those packs look almost empty.
    INDEXABLE_MIMES = ('text/html', 'text/vtt', 'application/pdf', 'text/plain')

    # The ted_mul_* packs are multilingual: 14,337 subtitle tracks across 65
    # languages, but only 688 English ones -- roughly 20 translations of every
    # talk. Indexing them all would embed each talk 20 times in languages the
    # retrieval model cannot use, so subtitle tracks are language-filtered.
    VTT_LANGUAGES = ('en',)
    _VTT_LANG_RE = re.compile(r'[._-]([a-z]{2}(?:-[a-z]{2,4})?)\.vtt$', re.I)

    def ingest_zim(self, zim_path: str, max_articles: int = 0,
                   min_text_length: int = 100,
                   include_media_text: bool = True,
                   skip_doc_ids: "set | None" = None) -> list:
        """Index a ZIM knowledge pack into the Zone 2 knowledge collection.

        Args:
            zim_path: Path to the .zim file.
            max_articles: Maximum articles to index (0 = all).
            min_text_length: Skip articles shorter than this.
            include_media_text: Also index VTT transcripts, PDFs and plain text,
                not just HTML. Set False to restore html-only behaviour.
            skip_doc_ids: If given, articles whose content-hash doc_id is already
                in this set are skipped (used to resume an interrupted mid-pack
                run without re-embedding what the store already holds). The count
                of skipped articles is exposed on ``self.last_skipped``.

        Returns:
            List of IngestedDocument records (only articles actually stored;
            skipped articles are counted in ``self.last_skipped``).
        """
        from .zim_reader import ZIMReader

        results = []
        count = 0
        self.last_skipped = 0
        mimes = self.INDEXABLE_MIMES if include_media_text else ('text/html',)

        with ZIMReader(zim_path) as reader:
            namespace = self._detect_article_namespace(reader)

            for index, entry in self._iter_article_indices(reader, namespace,
                                                           mime_types=mimes):
                if max_articles and count >= max_articles:
                    break

                if entry.mime_type.startswith('text/vtt') and \
                        not self._vtt_language_ok(entry.url):
                    continue

                # Retrieve by entry index, not by URL. ZIM URLs routinely contain
                # '?' and '&' (e.g. FEMA query-string paths), and namespace prefixes
                # differ between ZIM v5 ('A') and v6 ('C'), so URL lookup is fragile.
                article = reader.get_article(index)
                if article is None:
                    continue

                text = self._article_to_text(article)
                if len(text) < min_text_length:
                    continue

                # Resume support: skip articles already stored. doc_id is the
                # same content hash ingest_text would compute, so recomputing it
                # here (before the chunk/embed work) lets an interrupted run
                # continue instead of re-embedding millions of stored articles.
                if (skip_doc_ids is not None
                        and self._generate_doc_id(text) in skip_doc_ids):
                    self.last_skipped += 1
                    continue

                doc = self.ingest_text(
                    text,
                    title=article.title,
                    source=ContentSource.ZIM_PACK,
                    force_zone=TargetZone.KNOWLEDGE,
                )
                doc.file_origin = zim_path
                results.append(doc)
                count += 1

        return results

    # ── ZIM namespace handling ──────────────────────────────────────────────

    @staticmethod
    def _detect_article_namespace(reader, probe_limit: int = 2000) -> str:
        """Return the namespace that holds article content in this archive.

        ZIM v5 archives store articles under namespace 'A'. ZIM v6 archives
        (everything produced by modern zimit/libzim) store them under 'C'. Probing keeps the ingester
        correct for both instead of hard-coding one era of the format.
        """
        limit = min(reader.entry_count, probe_limit)
        counts = {'C': 0, 'A': 0}
        for i in range(limit):
            try:
                entry = reader.get_entry(i)
            except Exception:
                break
            if entry.is_redirect:
                continue
            if entry.namespace in counts and entry.mime_type.startswith('text/html'):
                counts[entry.namespace] += 1

        if counts['C'] >= counts['A'] and counts['C'] > 0:
            return 'C'
        if counts['A'] > 0:
            return 'A'
        return 'C'  # modern default

    def _article_to_text(self, article) -> str:
        """Extract indexable text from an article of any supported MIME type."""
        mime = (article.mime_type or '').split(';')[0].strip().lower()

        if mime == 'text/vtt':
            return self._vtt_to_text(article.content)

        if mime == 'application/pdf':
            return self._pdf_bytes_to_text(article.content)

        if mime == 'text/plain':
            return article.content.decode('utf-8', errors='replace').strip()

        return article.plain_text  # text/html and anything else

    @classmethod
    def _vtt_language_ok(cls, url: str) -> bool:
        """True if a subtitle track is in a wanted language.

        Tracks with no language marker in the filename are kept (single-language
        packs name them plainly); tagged tracks must match VTT_LANGUAGES. Region
        subtags are ignored, so 'pt-br' tests as 'pt'.
        """
        match = cls._VTT_LANG_RE.search(url or '')
        if not match:
            return True
        base = match.group(1).lower().split('-')[0]
        return base in cls.VTT_LANGUAGES

    @staticmethod
    def _vtt_to_text(raw: bytes) -> str:
        """Flatten a WebVTT subtitle track into readable prose.

        Strips the WEBVTT header, cue identifiers, timestamp lines and inline
        tags, then de-duplicates consecutive repeats (rolling captions repeat
        lines across cues) and joins the result into continuous text.
        """
        text = raw.decode('utf-8', errors='replace')
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.upper().startswith('WEBVTT'):
                continue
            if '-->' in line:                      # timestamp cue
                continue
            if line.isdigit():                     # cue number
                continue
            if line.startswith(('NOTE', 'STYLE', 'REGION')):
                continue
            line = re.sub(r'<[^>]+>', '', line)    # <v Speaker>, <i>, etc.
            line = line.strip()
            if not line:
                continue
            if lines and lines[-1] == line:        # rolling-caption repeat
                continue
            lines.append(line)
        return ' '.join(lines).strip()

    def _pdf_bytes_to_text(self, raw: bytes) -> str:
        """Extract text from PDF bytes held inside a ZIM cluster.

        Reuses the existing _extract_pdf_text() file-based extractor by staging
        the blob to a temp file, since the PDF readers want a path or stream.
        """
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name
        except Exception:
            return ''
        try:
            return self._extract_pdf_text(tmp_path).strip()
        except Exception:
            return ''
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _iter_article_indices(reader, namespace: str, max_bad_ratio: float = 0.25,
                              mime_types=('text/html',)):
        """Yield (index, entry) for every indexable entry in the given namespace.

        Isolated unreadable entries are tolerated (a big archive can carry a few),
        but a systematically broken archive must not silently present as "0 articles
        indexed" -- that failure mode hid a 174 GiB truncated pack in this library.
        If more than ``max_bad_ratio`` of entries fail, raise instead.
        """
        bad = 0
        total = reader.entry_count
        for i in range(total):
            try:
                entry = reader.get_entry(i)
            except Exception:
                bad += 1
                if bad > 32 and bad > total * max_bad_ratio:
                    raise IOError(
                        f"{os.path.basename(reader.filepath)}: {bad:,} of {total:,} "
                        f"directory entries are unreadable -- the archive is corrupt "
                        f"or incomplete. Re-download this pack."
                    )
                continue
            if entry.namespace != namespace or entry.is_redirect:
                continue
            if not any(entry.mime_type.startswith(m) for m in mime_types):
                continue
            yield i, entry

    # ── Chunking ────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, doc_id: str, title: str,
                    source: ContentSource, zone: TargetZone) -> list:
        """Split text into overlapping chunks with metadata."""
        chunks = []
        text = text.strip()

        if len(text) <= self.CHUNK_SIZE:
            chunks.append(TextChunk(
                chunk_id=f"{doc_id}_0",
                doc_id=doc_id,
                text=text,
                metadata={
                    'title': title,
                    'source': source.value,
                    'zone': zone.value,
                    'chunk_index': 0,
                    'total_chunks': 1,
                    'ingested_at': datetime.now().isoformat(),
                },
            ))
            return chunks

        # Split by paragraphs first, then merge into chunks
        paragraphs = re.split(r'\n\s*\n', text)
        current_chunk = ''
        chunk_idx = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current_chunk) + len(para) + 2 > self.CHUNK_SIZE and current_chunk:
                chunks.append(TextChunk(
                    chunk_id=f"{doc_id}_{chunk_idx}",
                    doc_id=doc_id,
                    text=current_chunk.strip(),
                    metadata={
                        'title': title,
                        'source': source.value,
                        'zone': zone.value,
                        'chunk_index': chunk_idx,
                    },
                ))
                chunk_idx += 1
                # Overlap: keep the last CHUNK_OVERLAP chars
                if len(current_chunk) > self.CHUNK_OVERLAP:
                    current_chunk = current_chunk[-self.CHUNK_OVERLAP:]
                # Don't reset to empty -- keep overlap

            current_chunk += ('\n\n' if current_chunk else '') + para

        # Final chunk
        if current_chunk.strip():
            chunks.append(TextChunk(
                chunk_id=f"{doc_id}_{chunk_idx}",
                doc_id=doc_id,
                text=current_chunk.strip(),
                metadata={
                    'title': title,
                    'source': source.value,
                    'zone': zone.value,
                    'chunk_index': chunk_idx,
                },
            ))

        # Update total_chunks in all metadata
        total = len(chunks)
        for c in chunks:
            c.metadata['total_chunks'] = total
            c.metadata['ingested_at'] = datetime.now().isoformat()

        return chunks

    # ── ChromaDB Storage ────────────────────────────────────────────────────

    def _store_chunks(self, collection, chunks: list):
        """Store chunks in a ChromaDB collection."""
        if not chunks:
            return

        ids = [c.chunk_id for c in chunks]
        documents = [c.text for c in chunks]
        metadatas = [c.metadata for c in chunks]

        # Embed explicitly rather than letting the collection decide. Chroma
        # applies whatever function the collection persisted — and the legacy
        # collections persisted an empty config, so a documents=-only add
        # would use chroma's default ONNX model instead of the configured one.
        # See puffpai.embeddings.open_collection.
        from puffpai.embeddings import embed_documents

        collection.add(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embed_documents(documents),
        )

    # ── PII Detection (lightweight) ────────────────────────────────────────

    _PII_PATTERNS = [
        re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),  # email
        re.compile(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b'),                          # phone
        re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),                                  # SSN
        re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'),           # credit card
        re.compile(r'\b(?:my\s+)?(?:name\s+is|i\s+am)\s+[A-Z][a-z]+',        # self-identification
                   re.IGNORECASE),
        re.compile(r'\b(?:dr\.?|doctor)\s+[A-Z][a-z]+', re.IGNORECASE),       # doctor names
        re.compile(r'\b\d{1,5}\s+[A-Z][a-z]+\s+(?:St|Ave|Blvd|Dr|Rd|Ln)',    # street address
                   re.IGNORECASE),
    ]

    def _detect_pii(self, text: str) -> bool:
        """Quick check for PII patterns in text."""
        for pattern in self._PII_PATTERNS:
            if pattern.search(text):
                return True
        return False

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _generate_doc_id(self, text: str) -> str:
        """Generate a deterministic document ID from content hash."""
        h = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
        return f"doc_{h}"

    def _extract_pdf_text(self, filepath: str) -> str:
        """Extract text from a PDF file.

        Tries multiple approaches: pypdf (preferred), PyPDF2, or pdfplumber.
        """
        # Try pypdf first (maintained successor to PyPDF2)
        try:
            from pypdf import PdfReader
            reader = PdfReader(filepath)
            pages = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            return '\n\n'.join(pages)
        except ImportError:
            pass

        # Try legacy PyPDF2 (deprecated but still works)
        try:
            import PyPDF2
            with open(filepath, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                pages = []
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        pages.append(text)
                return '\n\n'.join(pages)
        except ImportError:
            pass

        # Try pdfplumber
        try:
            import pdfplumber
            with pdfplumber.open(filepath) as pdf:
                pages = []
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        pages.append(text)
                return '\n\n'.join(pages)
        except ImportError:
            pass

        raise ImportError(
            "PDF extraction requires pypdf, PyPDF2, or pdfplumber. "
            "Install with: pip install pypdf"
        )
