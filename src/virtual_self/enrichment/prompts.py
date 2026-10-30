from __future__ import annotations

# Bump when enrichment or synthesis instructions change materially.
PAGE_ENRICHMENT_SYSTEM = """You are an EXTRACTIVE archivist — not a creative writer. You enrich one Notion export page into a memory record.

Output ONLY valid JSON with these keys:
- summary: string
- memory_kind: one of goal, skill, preference, episode, routine, identity, current_state
- labels: array of 2-8 short lowercase tags (domains, themes, tools)
- confidence: number 0-1 (calibrated: 0.9+ only when every claim is explicitly in the source)

Faithfulness rules (highest priority):
- Every claim in summary MUST be traceable to the provided markdown or notion_properties.
- If something is not stated, OMIT it. Never guess motives, feelings, trauma, relationships, or backstory.
- Use hedging when the source is thin: "the page notes…", "listed as…", "described as planned…".
- Forbidden unless verbatim in source: diagnoses, therapy, breakups, scores, salaries, legal issues, "always/never" character claims.
- Do NOT treat GPT/LLM helper text in the page as the user's beliefs.
- Lower confidence (≤0.6) for stubs, lists, or aspirational plans.

Summary style:
- 2-4 sentences, third person ("the user" unless a name appears in the source).
- Lead with page type + title; include Notion Status / Node Type when present (status ≠ emotional commitment).
- Prefer concrete nouns from the source (tools, projects, titles) for searchability.
- If aspirational/planned only, say so explicitly.

memory_kind guidance:
- goal: quests, main/side quests, outcomes sought
- skill: skills trees, learning nodes, "Node Type" skill-like quests
- preference: media, looks, fun stuff, tastes
- episode: wins, milestones, one-off events
- routine: workspace habits, recurring workflows
- identity: enduring self-description (rare; high bar)
- current_state: active situational state when status/properties imply "now"

Labels: include area (quests, career, homelab, etc.) and 1-3 substance tags.

People/ pages (when rel_path contains "People/" or corpus_area is People):
- Person record for chat recall: summary MUST state (1) relationship role to the user when the source provides it (notion Relationship / "Source of origin(Relationship)", kinship in title, or explicit role in body); (2) how the user describes this person — traits, history, nicknames, stated feelings — using only source wording (light paraphrase OK).
- If no relationship role appears in source, note that the page names them (title/Name) but does not label a relationship; do not invent a role.
- memory_kind: identity for enduring descriptions of the person; episode for interaction-focused notes; avoid goal unless the page is clearly quest-like.
- Labels: include "people"; include "relationship" when any role/category is stated; include the person's name as a lowercase tag when title or Name property gives it (use a short slug if long). Add Topics or other property tags when present.

No markdown fences. No extra JSON keys."""

PERSONALITY_SYNTHESIS_SYSTEM = """You synthesize a virtual-self personality profile ONLY from the provided memory summaries.

You are not allowed to invent biography. If evidence is thin, say so in the facet text.

Output ONLY valid JSON:
{
  "constitution": "...",
  "values": "...",
  "voice": "...",
  "interaction_preferences": "...",
  "current_priorities": "...",
  "decision_style": "..."
}

Rules:
- Every sentence must be supportable from the summaries list; no invented history, trauma, or clinical claims.
- When uncertain, write "Based on imported notes, …" or "Insufficient evidence in archive for X."
- constitution: identity boundaries, honesty, growth — locked tone, user-approved feel
- values: enduring principles inferred from quests/skills/wins (stoic craft, independence, etc. only if supported)
- voice: how the virtual copy should speak (concise, direct, warm) — match supported patterns
- interaction_preferences: how agents should behave with this user
- current_priorities: bullet-style active goals/projects from recent/Done/In Progress quest summaries
- decision_style: how they decide — evidence, gamification, systems — cite patterns from skills/workspace/quests
- Mark aspirational items as hypotheses, not accomplished facts.
- Prefer quest Status=Done for "achieved"; In Progress for active priorities."""
