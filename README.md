# Knowledge Forge for Obsidian

Knowledge Forge turns saved articles, papers, videos and browser clips into a Markdown library you can read, search and build on. Its Obsidian dashboard combines a live seven-day briefing, personal editions, a deep-reading lesson queue, and a clickable Canvas graph. The shared agent skill gives Ohayo and Munger the same editorial workflow.

- [System and vault design](docs/DESIGN.md)
- [Configure a vault and agents](docs/OPERATIONS.md)
- [Shared agent skill](skills/knowledge-forge/SKILL.md)
- [Web Clipper template](examples/obsidian-web-clipper.json)

## Try the synthetic demo

Use Python 3.12 or later on a POSIX system and install the dependencies into your development environment.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Copy the synthetic demo into a fresh runtime directory, then generate its Obsidian pages.

```sh
mkdir -p .runtime
cp -R examples/demo-vault .runtime/demo-vault
export KF_VAULT="$PWD/.runtime/demo-vault"
export KF_STATE_HOME="$PWD/.runtime/demo-state"
export KF_ENV_FILE="$PWD/.runtime/demo-unused.env"
bin/kf --json library init
bin/kf --json search "winter"
```

Open `.runtime/demo-vault` as an Obsidian vault, enable Bases and Canvas, and open **Library Start**. Demo dates are fixed so the checked-in examples stay reproducible. The live briefing includes captures whose dates fall within its seven-day window.

## From a saved link to a new edition

```text
Primary archive → checked reading guide → cited-resource brief → updated personal edition
                           ↘ live briefing, catalog, graph and search index
```

The engine saves the original, refreshes generated pages, and checks the links before publishing. The agent reads the archive and writes the interpretation. For a deep read it writes a lesson with examples, limitations and links to exact primary evidence. Blocked captures show their coverage and stay in the follow-up queue.

## Version control

This checkout holds the code, schemas, prompts, shared skill, design and synthetic fixtures. Keep your active vault and runtime state in separate directories. Start future changes from this Git checkout and use the checkout’s launcher to avoid running an older installed copy.
