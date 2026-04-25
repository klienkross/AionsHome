# Memory V2: Zettelkasten-Style Atomic Memory System

## Overview

Redesign the memory system from conversation-level summaries to atomic "one fact, one card" architecture with inter-card relationships, event lifecycle management, and multi-agent digest pipeline.

**Approach:** Incremental refactor on existing SQLite architecture, phased rollout.

---

## 1. Data Structure

### 1.1 `memory_cards` Table (replaces `memories`)

| Field | Type | Description |
|-------|------|-------------|
| id | TEXT PK | `card_{timestamp}_{hash}` |
| content | TEXT | Atomic description of one fact/event |
| type | TEXT | `event`, `preference`, `emotion`, `promise`, `plan`, `fact`, `aggregate` |
| status | TEXT | `open` / `closed` / `merged` |
| created_at | REAL | Creation timestamp |
| updated_at | REAL | Last update timestamp |
| source_conv | TEXT | Source conversation ID |
| source_start_ts | REAL | Original message start timestamp |
| source_end_ts | REAL | Original message end timestamp |
| embedding | BLOB | 1024-dim vector (float32) |
| keywords | TEXT | JSON array of keywords |
| importance | REAL | 0.0-1.0 |
| unresolved | INTEGER | Pending/unfinished flag |
| valence | REAL | Emotional valence (-1.0 to 1.0) |
| arousal | REAL | Emotional arousal (-1.0 to 1.0) |
| intensity_score | REAL | Conversation intensity metric (experimental) |

### 1.2 `memory_links` Table (new)

| Field | Type | Description |
|-------|------|-------------|
| id | INTEGER PK | Auto-increment |
| from_id | TEXT | Source card ID (FK → memory_cards.id) |
| to_id | TEXT | Target card ID (FK → memory_cards.id) |
| relation | TEXT | Relationship type |
| created_at | REAL | Creation timestamp |

**Relation types:**
- `follow_up` — Event progression ("planned cafe visit" → "visited cafe")
- `derived_from` — Preference/insight derived from event ("visited cafe" → "likes lattes")
- `aggregated_into` — Fragment merged into aggregate card
- `related` — Loose association

### 1.3 Card Type Taxonomy

| Type | Description | Example |
|------|-------------|---------|
| `event` | Something that happened | "4/20 went to the cafe" |
| `preference` | Like/dislike | "Likes iced lattes" |
| `emotion` | Emotional state | "Felt anxious about exam" |
| `promise` | Commitment/agreement | "Promised to call mom on Sunday" |
| `plan` | Intention/to-do | "Plans to go hiking next week" |
| `fact` | Objective information | "Birthday is March 15" |
| `aggregate` | Summary card from merged fragments | "4/20-4/23 had a cold, recovered" |

Removed types from v1: `digest`, `重要事件`, `shared_moment`, `life_event`, `milestone` — these are now represented via `event` type + importance score + link relationships.

---

## 2. Digest Engine Redesign

### 2.1 Pipeline (triggered 30 min after last message)

```
Step 1: Message grouping (retain existing time + semantic split logic)
   ↓
Step 2: Agent A — Atomic split
   Input: message group
   Output: multiple atomic cards with content, type, keywords, importance
           + optional "closes" field (list of card IDs this event completes)
           + confidence score for close suggestions
   ↓
Step 3: Agent B — Emotional evaluation (separate or unified, configurable)
   Input: same message group
   Output: valence + arousal per card
   ↓
Step 4: Agent C — Conversation rhythm analysis (experimental)
   Input: message timestamps + lengths
   Output: intensity_score (pure computation, no LLM call)
   ↓
Step 5: Relationship matching
   For each new card, search existing open cards by embedding similarity:
     ≥ auto_threshold (default 0.85) → auto link/close/merge
     ≥ ask_threshold (default 0.65) → AI decides whether to ask user
     < ask_threshold → no action
   ↓
Step 6: Generate embeddings, write to DB
   ↓
Step 7: Existing post-digest flow (AI reflection + gift judgment)
```

### 2.2 Agent Split Configuration

```json
// settings.json
{
  "digest_agents": {
    "split_mode": "separate"    // "separate" | "unified"
  },
  "digest_matching": {
    "auto_threshold": 0.85,
    "ask_threshold": 0.65
  }
}
```

- `separate`: Steps 2/3 each call qwen-flash independently; Step 4 is pure math
- `unified`: Single prompt for atomic split + emotion; Step 4 still separate

### 2.3 Atomic Split + Lifecycle Detection (two-step)

**Step A: Agent A outputs atomic cards (no card IDs needed):**

```json
[
  {
    "content": "Recovered from cold",
    "type": "event",
    "keywords": ["cold", "recovery", "health"],
    "importance": 0.4
  }
]
```

**Step B: Backend matches each new card against existing `open` cards by embedding similarity. For candidates above `ask_threshold`, a second AI call determines whether the old event should be closed:**

```json
{
  "new_card": "Recovered from cold",
  "candidate_open_card": "card_1713600000_abc: Got a cold",
  "should_close": true,
  "confidence": 0.9,
  "relation": "follow_up"
}
```

This avoids requiring Agent A to know existing card IDs.

### 2.4 Aggregate Generation Triggers

- Event chain (connected via `follow_up`) reaches **≥ 3 cards** → auto-generate aggregate
- Duplicate fragment events (via `aggregated_into`) reach **≥ 2 cards** → auto-generate aggregate
- Manual trigger via active memory organization

Aggregate content format: `"{date_range} {summary}, {final_status}"`
Example: `"4/20-4/23 had a cold, with fever, recovered 4/23"` with `status: closed`

### 2.5 Real-time Memory Write (`[MEMORY:...]`)

The existing `[MEMORY:...]` markup is retained. When AI outputs `[MEMORY:content]` during conversation, the backend:

1. Creates an atomic `memory_card` (auto-detect type from content via qwen-flash)
2. Generates embedding, extracts keywords
3. Runs relationship matching against existing open cards (same logic as digest Step 5)
4. Stores with `source_conv` and `source_msg_id` for traceability

**Deduplication with digest:** When digest processes a message group, it checks each candidate card against cards already created via `[MEMORY:...]` from the same `source_conv`:
- Embedding similarity ≥ 0.85 with an existing card from same conversation → skip (already recorded)
- Similarity 0.65-0.85 → merge: update existing card's keywords/importance if the digest version adds information, create link if they represent different aspects
- Similarity < 0.65 → treat as a new card (different topic)

This means `[MEMORY:...]` cards serve as anchors that digest respects rather than duplicates.

---

## 3. Recall & Retrieval

### 3.1 Passive Recall (modified `recall_memories`)

**Filter layer:** Only return:
- `aggregate` cards
- Independent cards NOT linked via `aggregated_into`
- (i.e., exclude raw fragments that have been aggregated)

**Scoring formula:**

```
score = (vec_sim × 0.6 + keyword_match × 0.3 + importance × 0.1)
        × time_decay
        × status_weight
```

- `status_weight`: `open` = 1.0, `closed` = 0.3, `merged` = excluded
- `unresolved` cards: no time decay (same as v1)
- `intensity_score`: returned but not yet factored into formula (experimental)

### 3.2 Surfacing (background injection into prompt)

Priority order:
1. `unresolved` + `status:open` cards (max 2)
2. Topic-related `aggregate` cards (max 3)
3. Cards created within last 3 days (fill to max_total=8)

### 3.3 Active Retrieval (new capability)

AI-triggered operations via markup syntax (consistent with existing `[MEMORY:...]` pattern):

| Markup | Function | Description |
|--------|----------|-------------|
| `[RECALL:keywords]` | `search_memory(keywords, top_k)` | Keyword + vector search, returns matching cards |
| `[EXPAND:card_id]` | `expand_memory(card_id)` | Expand aggregate to show underlying fragments |
| `[TIMELINE:card_id]` | `get_timeline(card_id)` | Get full follow_up chain for an event |
| `[ORGANIZE:keywords]` | `organize_memories(keywords)` | Trigger active organization on a topic |

**Result injection:** When AI triggers a retrieval markup, the backend intercepts it, executes the query, and injects results into the next prompt turn as a system context block (same mechanism as current `fetch_source_details`). The AI's markup is stripped from the visible response; only the natural language reply is shown to the user.

**Future migration path:** These map 1:1 to tool-use / MCP functions. Switching from markup to tool_use only requires changing the parsing layer; underlying functions remain the same.

### 3.4 Active Organization

When AI triggers `[ORGANIZE:keywords]`, backend executes:
1. Search related cards
2. Detect mergeable fragments, closeable events, missing links
3. Apply same rules as digest Step 5 (thresholds, confidence, aggregate generation)
4. Reuses digest engine's relationship matching + aggregate generation logic

---

## 4. Event Lifecycle

### 4.1 Status Transitions

```
open → closed       (event completed)
open → merged       (merged into aggregate, no longer surfaces independently)
closed → open       (correction, reopen)
```

### 4.2 Automatic Lifecycle (Digest Step 5)

Agent A outputs `closes` field with confidence:
- `confidence ≥ auto_threshold` → auto-close + create `follow_up` link
- `confidence ≥ ask_threshold` → AI decides whether to ask user in reflection message
- `confidence < ask_threshold` → no action

### 4.3 Frontend Changes

Memory management page additions:
- Status filter (open / closed / all)
- Aggregate cards expandable to show fragments
- Manual status editing and link management
- Visual link display between related cards

---

## 5. Conversation Intensity Metric (Experimental)

### 5.1 Signals

| Signal | Measurement |
|--------|-------------|
| Message interval | Seconds between consecutive messages |
| Message length | Character count |
| Consecutive turns | Number of rapid back-and-forth exchanges |

### 5.2 Computation

Pure math, no LLM call. Computed per message group during digest Step 4.

Initial placeholder formula:
```
speed_score = 1 - min(avg_interval_seconds / 300, 1.0)   # faster = higher
length_score = min(avg_msg_chars / 200, 1.0)              # longer = higher
density_score = min(turn_count / 20, 1.0)                 # more turns = higher
intensity = speed_score × 0.4 + length_score × 0.35 + density_score × 0.25
```

Weights and normalization constants are configurable. This is a starting point — adjust after observing real data.

Normalized to 0.0-1.0. Stored on each card produced from that group.

### 5.3 Usage

Phase 1: Collect only, not used in any scoring formula.
Phase 2: After sufficient data, analyze correlation with subjective importance. If useful, incorporate into recall scoring.

Raw data (timestamps + lengths) lives in `chat.db` messages table — formula can be changed and all scores recomputed at any time without re-running digest.

---

## 6. Migration Strategy

### 6.1 Phased Rollout

| Phase | Scope | Deliverable |
|-------|-------|-------------|
| 1 | Data structure + atomic digest | New tables, modified digest engine outputs atomic cards + links |
| 2 | Event lifecycle | Close/merge detection in digest, aggregate generation |
| 3 | Agent separation | Configurable split/unified mode for digest agents |
| 4 | Active retrieval + intensity | `[RECALL]`/`[ORGANIZE]` markup, intensity metric collection |

### 6.2 Backward Compatibility

- Old `memories` table retained, renamed to `memories_v1` for reference
- New cards created by new digest engine go into `memory_cards`
- Existing recall logic updated to read from `memory_cards`
- Old memories NOT auto-migrated; optionally re-digest from chat history using existing `rebuild_memories.py` (adapted for v2)

### 6.3 Branch Strategy

All work on a new branch `feature/memory-v2`, merged phase by phase or as a whole after testing.
