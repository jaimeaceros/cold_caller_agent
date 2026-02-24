"""
Knowledge Base — vector-powered retrieval layer.

Uses Qdrant (local mode, no external server) + sentence-transformers
for semantic search. Replaces the keyword-matching approach while
keeping the exact same interface brain.py calls:

    retrieve(query, categories) → list[RetrievedEntry]

On init:
  1. Loads the embedding model (BAAI/bge-small-en-v1.5)
  2. Creates a local Qdrant collection
  3. Embeds all entries from knowledge_base.json
  4. Upserts them with metadata (category, subcategory, etc.)

On retrieve():
  1. Embeds the prospect's message
  2. Queries Qdrant filtered by category
  3. Returns top matches as RetrievedEntry objects
  4. Compliance entries are always included (rule-based, not vector)
"""

import json
from pathlib import Path
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams,
    Distance,
    PointStruct,
    Filter,
    FieldCondition,
    MatchAny,
)
from sentence_transformers import SentenceTransformer


# ---------------------------------------------------------------------------
# Model config
# ---------------------------------------------------------------------------
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"  # 384 dimensions, ~130MB, fast
EMBEDDING_DIM = 384
COLLECTION_NAME = "knowledge"


@dataclass
class RetrievedEntry:
    id: str
    category: str
    subcategory: str
    content: str
    follow_up_action: str | None
    score: float


class KnowledgeBase:

    def __init__(self, json_path: str):
        path = Path(json_path)
        if not path.exists():
            raise FileNotFoundError(f"Knowledge base not found: {json_path}")

        with open(path, "r") as f:
            data = json.load(f)

        self.metadata = data.get("metadata", {})
        self.entries: list[dict] = data.get("knowledge_entries", [])

        # --- Load embedding model ---
        self.model = SentenceTransformer(EMBEDDING_MODEL)

        # --- Init Qdrant (in-memory, no external server) ---
        self.client = QdrantClient(":memory:")
        self._build_index()

    def _build_index(self):
        """Embed all knowledge entries and upsert into Qdrant."""

        # Create collection
        self.client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=EMBEDDING_DIM,
                distance=Distance.COSINE,
            ),
        )

        # Separate compliance entries (these are rule-based, not vector-searched)
        self._compliance_entries: list[dict] = []
        points: list[PointStruct] = []

        for i, entry in enumerate(self.entries):
            if entry.get("category") == "compliance_rules":
                self._compliance_entries.append(entry)
                continue

            # Build text to embed: combine trigger phrases + content
            # for richer semantic representation
            trigger_text = " ".join(entry.get("trigger_phrases", []))
            content_text = entry.get("content", "")
            embed_text = f"{trigger_text} {content_text}".strip()

            if not embed_text:
                continue

            vector = self.model.encode(embed_text).tolist()

            points.append(
                PointStruct(
                    id=i,
                    vector=vector,
                    payload={
                        "entry_id": entry.get("id", ""),
                        "category": entry.get("category", ""),
                        "subcategory": entry.get("subcategory", ""),
                        "content": content_text,
                        "follow_up_action": entry.get("follow_up_action"),
                        "effectiveness_score": entry.get("effectiveness_score", 0.0),
                    },
                )
            )

        if points:
            self.client.upsert(
                collection_name=COLLECTION_NAME,
                points=points,
            )

    def retrieve(
        self,
        query: str,
        categories: list[str],
        top_k: int = 3,
        include_compliance: bool = True,
    ) -> list[RetrievedEntry]:
        """
        Main retrieval method. Interface is identical to the old version.

        Args:
            query: The prospect's last message (raw text)
            categories: Which knowledge categories to search
            top_k: Max number of scored results to return
            include_compliance: Always include compliance rules

        Returns:
            List of RetrievedEntry objects sorted by relevance score (desc).
            Compliance entries (score=1.0) appear first.
        """

        # --- Compliance: rule-based, always included ---
        compliance: list[RetrievedEntry] = []
        if include_compliance:
            query_lower = query.lower()
            for entry in self._compliance_entries:
                triggers = entry.get("trigger_phrases", [])
                if not triggers or any(t.lower() in query_lower for t in triggers):
                    compliance.append(self._to_retrieved_entry(entry, score=1.0))

        # --- Vector search filtered by category ---
        query_vector = self.model.encode(query).tolist()

        search_filter = Filter(
            must=[
                FieldCondition(
                    key="category",
                    match=MatchAny(any=categories),
                )
            ]
        )

        results = self.client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            query_filter=search_filter,
            limit=top_k,
        ).points

        scored: list[RetrievedEntry] = []
        for result in results:
            payload = result.payload
            scored.append(
                RetrievedEntry(
                    id=payload.get("entry_id", ""),
                    category=payload.get("category", ""),
                    subcategory=payload.get("subcategory", ""),
                    content=payload.get("content", ""),
                    follow_up_action=payload.get("follow_up_action"),
                    score=round(result.score, 3),
                )
            )

        return compliance + scored

    def _to_retrieved_entry(self, entry: dict, score: float) -> RetrievedEntry:
        """Convert a raw dict entry to a typed RetrievedEntry."""
        return RetrievedEntry(
            id=entry.get("id", ""),
            category=entry.get("category", ""),
            subcategory=entry.get("subcategory", ""),
            content=entry.get("content", ""),
            follow_up_action=entry.get("follow_up_action"),
            score=score,
        )

    def get_by_id(self, entry_id: str) -> dict | None:
        """Direct lookup by ID. Useful for follow_up_action chains."""
        for entry in self.entries:
            if entry.get("id") == entry_id:
                return entry
        return None

    def list_categories(self) -> list[str]:
        """List all unique categories in the knowledge base."""
        return list(set(entry.get("category", "") for entry in self.entries))