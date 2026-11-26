# Knowledge Forge design

Knowledge Forge is a local capture and editorial system for an Obsidian vault. It preserves the evidence behind each saved item and gives the reader a route from a short briefing to a guide, a lesson, and the original material. The project repository contains the software, contracts, and setup examples. The working vault and its operational state belong to the installation.

## Vault layout

All paths below are relative to the configured vault root. Source IDs and vault-relative links let the same vault open on multiple devices.

| Path | Purpose |
| --- | --- |
| `HOME.md`, `Reading Desk.md`, `Library Index.md`, `Knowledge Map.md` | Entry points for briefing, active reading, catalog browsing, and the topic hierarchy. |
| `Library.base` | Live views of Markdown note properties. |
| `00_inbox/Web Clips/` | Browser-saved Markdown retained as intake receipts with links to the filed copies. |
| `10_sources/` | Stable source notes with provenance, capture coverage, archived text, and a delimited reading guide when available. |
| `20_concepts/`, `30_entities/` | Evidence-linked synthesis, question pages, and notes about people, organizations, or other entities. |
| `40_maps/` | Domain and topic navigation, including branch Canvases and their Markdown indexes. |
| `50_skills/Deep Reading/`, `50_skills/Referenced Resources/` | Guided lessons and separate reviews of resources cited by a primary source. |
| `60_briefings/Current Brief.md`, `60_briefings/editions/` | Rolling recent coverage and dated editorial issues, respectively. |
| `70_media/`, `_attachments/` | Media and original files. Capture artifacts use a source ID and content digest in their paths. |
| `80_queues/`, `90_meta/` | Capture and review follow-up, the taxonomy registry, and vault workflow records. |

## Capture, identity, and receipts

An authenticated forward can enqueue an HTTPS share link and return a stable `job_` receipt. A worker records the processing outcome and any resulting source path. Job states such as `ready`, `retry`, `needs_input`, `partial`, and `archived` describe the work on a receipt. The source note’s `capture_status` describes evidence coverage. A receipt acknowledges intake. A verified archive requires a finished write and a source record discoverable in the local index.

The capture pipeline normalizes URLs and derives a stable `src_` identity from the canonical URL or local content. It checks the existing vault for that identity and legacy URL equivalents. An unchanged capture returns the existing note. New evidence can append a capture version to the same note while preserving the original, reading progress, and established navigation. A weaker retry leaves a complete archive intact. When a retry makes a quarantined item usable, the queue note records the resolution and links to the filed source. Browser imports keep the inbox copy, link it to the verified result, and skip an unchanged import fingerprint. An unverified clip carries an explicit coverage limitation.

| `capture_status` | Meaning |
| --- | --- |
| `complete` | Usable evidence consistent with the stated format and scope. |
| `usable_partial` | Usable evidence with a declared gap, such as an abstract in place of a paper or an incomplete transcript. |
| `metadata_only` | Identifying information without enough substantive content for a normal archive. |
| `failed` | A usable capture could not be made. Record the reason and the next input or retry needed. |

`reading_status` records the reader’s intent independently. The values are `To review`, `Read deeply`, `Reviewed`, and `Reference`. The review queue treats a missing status as `To review`. `analysis_status` and `classification_status` track editorial and filing readiness separately. A `Reviewed` source can still need capture follow-up. A complete capture can still need a checked guide.

## Reading and publication

| Artifact | Role and evidence |
| --- | --- |
| Source archive | The record of what was actually acquired. Store source URL, method, coverage, original text or transcript, and available original files. |
| Reading guide | A replaceable section above the saved body in the source note. Give an abstract, short takeaways with checked quotations, interpretation, limitations, and a jump to the saved evidence. |
| Guided lesson | A separate teaching note linked both ways to a complete primary archive. Explain the problem, prerequisites, method, evidence, practical value, and limits. Use a worked example and a check for understanding. Link exact evidence headings and the original PDF when present. |
| Referenced-resource review | A source-linked companion created after the primary archive. Record the exact project or publisher URL, supporting excerpts, and coverage. The pilot inspects up to four discovered public references and lists inaccessible or unreviewed items with their status. |
| Current Brief | A generated rolling seven-day view refreshed on library rebuild. It groups up to ten distinct usable sources, flags partial coverage, and links either checked takeaways or clearly labeled source excerpts. |
| Dated personal edition | An agent-authored Markdown synthesis that connects evidence across sources and ends with a question, decision, or small test. Publishing can revise the current day’s rolling issue in place. Prior dates stay separately addressable. A weekly editorial cadence is an operating preference, with shorter issues when evidence warrants them. |

HOME links to the current brief, latest personal edition, and up to three ranked reading picks. The reading picks depend on a complete capture and checked, evidence-linked analysis. The Read Deeply view opens the lesson where one exists and shows a pending state otherwise. The Markdown reading desk offers direct links alongside the Bases views.

## Bases, Canvas, and personal exploration

Bases projects existing Markdown properties into reading queues and catalog views. Use `To Review`, `Read Deeply`, `Reviewed`, and `Reference` for reading decisions. Use `Capture Follow-Up` for missing material and `Needs Attention` for broader review work. The Personal Brief view selects dated editorial issues. The Markdown index and topic outline provide navigation when Bases is unavailable.

The Brain Canvas provides an overview of active domains with drill-downs to branch Canvases, topic indexes, and archived sources. Generated cards have stable IDs. A rebuild carries over the position and size of surviving cards and keeps manually added nodes and edges whose endpoints still exist. Generated text and connections refresh from the registry and vault. New or retired categories can change the visible cards, so check the layout after a taxonomy change. Personal notes and exploratory Canvases can hold hypotheses and custom links independently of generated navigation.

### Personal graph and brainstorming prompts

- Which two archived sources address the same question with different evidence? Link their strongest passages and state where their conclusions diverge.
- Which idea connects these two topic branches? Suggest a question page with typed links that explain what each source contributes, and name the evidence still missing.
- What would change my mind about this hypothesis? Separate observations from interpretation, then propose one small reading or research step.

## Taxonomy governance

The schema-validated YAML registry at `90_meta/taxonomy/registry.yaml` owns category IDs, parent relationships, aliases, and lifecycle status. The classifier receives a shortlist of existing candidates and can abstain. Uncertain assignments go to review. Growing clusters begin as tags or links. Top-level domains require explicit approval. Provisional branches must meet evidence thresholds and parent-level rules, and fine-grained branches are used sparingly. Merges require approval and preserve aliases and old-ID redirects. Retiring a topic removes it from active navigation while retaining its historical pages and sources. Routine reclassification preserves stable source paths.

## Editorial decisions and local validation

The agent reads saved evidence, writes drafts outside the vault, and submits them through the same local command interface used by other callers. It explains significance and uncertainty, checks whether each citation supports its claim, and decides which stories are worth carrying forward. Quoted passages and edition links get local structural checks. A finished lesson requires a complete primary source and exact archive headings. A dated edition requires an exact heading citation in the triggering source. Those checks establish that evidence is reachable. Editorial review remains responsible for whether it supports the interpretation.

Local code owns capture grading, identity, candidate validation, path constraints, transaction staging, locking, recovery, and index refresh. Capture and map rebuilds are separate transactions. If derivative generation is interrupted after the source is committed, a library rebuild can regenerate the navigation from the saved vault. Captured text is treated as data. Reference inspection is bounded to public resources, with size and time limits and explicit blocked or unread outcomes. Editorial publication, capture receipts, and platform coverage each have their own visible status.

## Sync and operating limits

Sync the working vault through the reader’s chosen file sync. Include Markdown, attachments, the taxonomy registry, and Obsidian documents such as `.base` and `.canvas`. Keep the search index, receipt database, cache, logs, locks, staging, transaction journals, and credentials outside the synced vault. The index is rebuildable from the vault. Pending receipts and journals are local operational records that need their own backup if they must survive a move. Use one writer for automated ingestion and publishing per synced vault. Local file locks coordinate processes on that machine. Resolve sync conflicts before rebuilding and confirm that attachments and evidence links open on the reading device.

Capture coverage depends on the source and available adapters. Login walls, deleted or truncated content, missing captions, scanned PDFs, and unavailable OCR can leave partial evidence or require manual input. A browser clip can help where the reader already has authorized access and has configured the extension. A transcript preserves the spoken channel and any captured timing. Visual claims depend on available frames or notes. The pilot documented desktop Obsidian checks for evidence jumps and reading-queue updates. Mobile behavior and live access across providers require installation-specific verification.
