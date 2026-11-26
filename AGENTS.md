# Knowledge Forge development

- Use this Git checkout as the source of truth for the engine, agent skill, schemas, prompts, and vault design. Start code changes from its committed history and record fixes with tests. The previous pilot copy is historical.
- On Liang's machine the canonical checkout is `/home/ohayo/Projects/knowledge-forge`. The `kf-live` launcher uses `bin/kf` here. Keep test vaults and test state separate from the production vault and agent installations.
- Run `bin/check` for the offline verification suite. Use synthetic sources and keep credentials, the active vault, SQLite state, and captured files outside Git.
- Verify the commit and explicit target before pushing. Review changes to deployed agent skills and runtime configuration separately.
