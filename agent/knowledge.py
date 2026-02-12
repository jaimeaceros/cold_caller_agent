import json
from pathlib import Path
from dataclasses import dataclass


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

    def retrieve(
        self,
        query: str,
        categories: list[str],
        top_k: int = 3,
        include_compliance: bool = True,
    ) -> list[RetrievedEntry]:
        """
        Main retrieval method. This is the interface brain.py calls.

        Args:
            query: The prospect's last message (raw text)
            categories: Which knowledge categories to search
                        (from StateConfig.knowledge_categories)
            top_k: Max number of scored results to return
                   (compliance entries are added on top of this)
            include_compliance: Always include compliance rules
                                (default True, only disable for testing)

        Returns:
            List of RetrievedEntry objects sorted by relevance score (desc).
            Compliance entries (score=1.0) appear first.
        """
        query_lower = query.lower()
        query_words = set(query_lower.split())

        scored: list[RetrievedEntry] = []
        compliance: list[RetrievedEntry] = []

        for entry in self.entries:
            entry_category = entry.get("category", "")

            # --- Compliance: always included, no scoring needed ---
            if include_compliance and entry_category == "compliance_rules":
                # Only include compliance entries that are either:
                # - General rules (no trigger phrases) → always relevant
                # - Triggered by prospect's words (e.g., "do not call")
                triggers = entry.get("trigger_phrases", [])
                if not triggers or self._has_phrase_match(query_lower, triggers):
                    compliance.append(self._to_retrieved_entry(entry, score=1.0))
                continue

            # --- Category filter: skip entries not relevant to current state ---
            if entry_category not in categories:
                continue

            # --- Score by keyword/phrase matching ---
            score = self._score_entry(query_lower, query_words, entry)
            if score > 0:
                scored.append(self._to_retrieved_entry(entry, score))

        # Sort scored results by relevance, take top_k
        scored.sort(key=lambda e: e.score, reverse=True)
        top_results = scored[:top_k]

        # Compliance first, then scored results
        return compliance + top_results

    def _score_entry(
        self, query_lower: str, query_words: set[str], entry: dict
    ) -> float:
        """
        Score a knowledge entry against the prospect's query.

        Scoring logic (simple, effective):
        - Exact phrase match in trigger_phrases → 3.0 points per match
        - Individual word overlap with trigger_phrases → 1.0 per word
        - Boost by effectiveness_score if available

        This is the part you replace with vector similarity when
        moving to Qdrant.
        """
        trigger_phrases = entry.get("trigger_phrases", [])
        if not trigger_phrases:
            return 0.0

        score = 0.0

        # --- Exact phrase matching (highest signal) ---
        for phrase in trigger_phrases:
            phrase_lower = phrase.lower()
            if phrase_lower in query_lower:
                score += 3.0

        # --- Word-level overlap ---
        trigger_words = set()
        for phrase in trigger_phrases:
            for word in phrase.lower().split():
                trigger_words.add(word)

        overlap = query_words & trigger_words
        score += len(overlap) * 1.0

        # --- Boost by historical effectiveness ---
        effectiveness = entry.get("effectiveness_score")
        if effectiveness is not None and score > 0:
            score *= (0.5 + effectiveness)  # Range: 0.5x to 1.5x

        return round(score, 3)

    def _has_phrase_match(self, query_lower: str, trigger_phrases: list[str]) -> bool:
        """Check if any trigger phrase appears in the query."""
        return any(phrase.lower() in query_lower for phrase in trigger_phrases)

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
