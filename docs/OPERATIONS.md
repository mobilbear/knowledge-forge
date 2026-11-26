# Operate Knowledge Forge

## Choose one vault and writer

Set `KF_VAULT` to the root directory opened by Obsidian. Set `KF_STATE_HOME`, `KF_CACHE_HOME`, and `KF_BACKUP_ROOT` outside the vault. Keep the env file outside the repository and the synced folder. See [the example configuration](../kf.env.example). The checkout launcher uses its own source code and defaults to the offline model profile. That profile can still fetch a URL supplied for capture. It skips calls to a model API and leaves interpretation for an agent.

Use `bin/kf --json library init` to stamp a selected vault, persist the starting taxonomy if it is absent, and generate HOME, Canvas and Bases. For an existing vault, take and verify a backup before first initialization. Re-run `bin/kf --json library rebuild` after manual taxonomy or navigation edits. Use `bin/kf --json status` to check index freshness and incomplete transactions.

Each vault needs its own state directory. The library writer records the vault's resolved path in its journals. If an incomplete journal points to another path or predates vault binding, review the journal and original vault before retrying. Recovery retains backups and staged files. When the vault and state directories are on different filesystems, restored copies of interrupted new files may be kept under `.kf-recovery` beside the affected files. The library skips these folders in discovery and search.

## Capture and review

| Job | Command using the configured launcher |
| --- | --- |
| Save an article | `kf --json capture url "https://example.org/article"` |
| Save an article and check its public references | `kf --json capture url "https://example.org/article" --follow-references` |
| Submit a forwarded link for processing | `kf --json capture submit "<forwarded-message>"` |
| Process that receipt | `kf --json capture drain <receipt>` |
| Review a receipt | `kf --json status <receipt>` |
| Import synced browser clips | `kf --json library import-clips` |
| File a checked source guide | `kf --json library reading-guides <draft.json>` |
| Check references in an archived article | `kf --json library resources <source-id-or-path>` |
| File a deep-reading lesson | `kf --json library lesson <source-id-or-path> <draft.md>` |
| Update today's personal edition | `kf --json library edition <source-id-or-path> <draft.md>` |
| Find past evidence | `kf --json search "topic"` |

Import the [Web Clipper template](../examples/obsidian-web-clipper.json) in Obsidian Web Clipper. Use the configured vault in the extension. Browser clips enter `00_inbox/Web Clips`. Set `clip_scope` to `Full article` only after checking the complete body.

Optional format support depends on the machine. The Python `capture` extra includes article extraction and `yt-dlp`. Text PDFs use `pdfinfo` and `pdftotext`. Image OCR uses Tesseract with the appropriate language packs or a configured OCR adapter. Short local speech previews use FFmpeg, the `speech` extra, and a compatible model file selected with `KF_WHISPER_MODEL`. Videos without usable captions, access-controlled pages, and scanned documents may need user-supplied material.

## Route Ohayo and Munger through the same checkout

1. Configure one launcher with the target vault and state paths. Give both agents that same absolute launcher path and confirm the runtime is using it. A bare `kf` can resolve to an older install.
2. Review [the shared skill](../skills/knowledge-forge/SKILL.md) and its [editorial draft formats](../skills/knowledge-forge/references/editorial-workflow.md). Install that skill using each agent runtime's documented skill mechanism after obtaining approval for production changes. Keep the prior skill copy until the new route has been verified.
3. Save a public article through each agent. Confirm the receipt, exact source path, checked guide, resource review where eligible, and current edition link. Re-submit one of the links to confirm that it opens the existing source.
4. Open the synced vault on the reading device. Check the briefing, a Canvas branch, and an evidence jump to an original PDF or transcript.

On Liang's machine the agent command is `/home/ohayo/.local/bin/kf-liangwiki`. It delegates to `kf-live` and the committed checkout. Both runtimes can link the skill directory to `skills/knowledge-forge` in that checkout. OpenClaw accepts that link under its managed skills directory. Hermes Munger accepts it under its profile's skills directory. Preserve prior copies outside the discovery directories so their shared name cannot override the link. Verify the selected path with `openclaw skills info --agent main --json knowledge-forge` and check that `hermes -p munger skills list --enabled-only` includes `knowledge-forge`.

Keep source text, transcripts, and linked pages as evidence. The agent writes its interpretation in a local draft and uses the CLI to file it. An incomplete receipt should describe the missing input. Use the editable personal edition for the narrative. The engine regenerates Current Brief from saved state after capture.

## Sync and updates

Sync Markdown, Canvas, Bases and attachments through your chosen file sync, such as Syncthing or VaultSync. Keep the writer's journals, queues, logs, locks and index on the writer machine. Exclude `.kf-recovery` folders from sync. A search index can be rebuilt. Pending jobs and transaction records need their own backup if they must survive a move.

Start code changes from the tracked Git branch. On Liang's machine use `/home/ohayo/Projects/knowledge-forge`. Check the current worktree, pull with `git pull --ff-only` when the upstream is available, and run `bin/check` from the checkout. This suite uses synthetic captures and sandboxed runtime paths under `.runtime` and retains its fixtures for review. Live provider checks, migration, installation and legacy cleanup use separate procedures. Review the committed skill before deploying it to agents.
