# AGENTS.md

## Code map (docs/codemap/)

- `docs/codemap/codemap.json`, `codemap.html`, and `codemap.lock` describe the repo's modules, data flows, and end-to-end scenarios. They must always reflect the current state of the code — never left stale.
- Before finishing any task that adds, removes, or renames a file, or changes how modules call/read/write each other (new fetch, new page, new external dependency, new data file), compare `docs/codemap/codemap.lock` against the current repo:
  - If any tracked module's fingerprint no longer matches its current files, that module changed — regenerate.
  - If a new top-level file/module exists that isn't in the lock, treat it as new — regenerate.
- Regenerate all three files together (`codemap.json`, `codemap.html`, `codemap.lock`) — never edit just one by hand. `codemap.html` must embed the exact same nodes/edges/flows as `codemap.json`.
- Every node, edge, and flow must be backed by real evidence from the source (file path + line/anchor). If a relationship can't be verified in the code, mark it `unknown` — do not guess.
- Do not modify product code while regenerating the code map. This is a read-and-document task only.
- After regenerating, verify: JSON parses, every node path exists (or is explicitly marked as not-yet-present with a reason, like a data file a workflow hasn't produced yet), every edge/flow step references a real node id, and `codemap.lock` matches the current commit and module fingerprints.
