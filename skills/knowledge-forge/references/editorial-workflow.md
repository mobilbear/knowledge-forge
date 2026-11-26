# LiangWiki editorial workflow

Read the archived source first. Save drafts outside the vault and pass their
paths to the shared Knowledge Forge executable. Use the original source's
language for its guide unless Liang asks for another language.

## Reading guide

Use the actual archived filename, including `.md`, as the JSON key. Read the
saved body, transcript, or PDF text before writing. The abstract explains the
claim. The meaning explains why Liang might care. The limitation states what
the evidence cannot support. Every quote must appear in the archived source.
Supply a topic ID from the current taxonomy when a source needs classification.

```json
{
  "actual-source-filename.md": {
    "abstract": "Describe the problem and the source's main finding.",
    "meaning": "Explain one way Liang could apply or test the idea.",
    "limitation": "Describe the scope and the main uncertainty.",
    "points": [
      {
        "point": "State one checked takeaway in your own words.",
        "quote": "Copy its short supporting passage from the actual source."
      }
    ]
  }
}
```

After applying the guide, use `library resources` to check explicit public
references. The command saves publisher excerpts and links. Explain how a
reference supports, qualifies, or extends the post using publisher evidence.
Record material gaps such as a login wall or an abstract-only capture.

## Personal edition

Open the live briefing and the current dated edition. Use up to ten recent
sources, carrying forward stories that still help Liang. Write one or two
short stories if the new source is the only substantive update. When sources
overlap, explain what they support together and where they differ. End with a
question, decision, or small test Liang could use. State when the original
capture was partial.

The Markdown draft needs a top-level title and at least one evidence link in
the triggering source, such as `[[10_sources/archived-note#Source evidence 1|Evidence]]`.
Use the real path and heading. Include citations for the other stories. An
optional `edition_deck` property describes the issue in one line. The edition
command creates or updates today's note after checking the links.

## Deep reading

When Liang asks to study a topic, explain its problem, prerequisites, method,
evidence, value, and limitations in the style of a teacher. Work through one
example and include a check for understanding. Define symbols in words.
Use a diagram or table where it helps. Cite exact page or transcript headings
from the complete source. Use `library lesson` to file the draft and attach the
original PDF when present.
