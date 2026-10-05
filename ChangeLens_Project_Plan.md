# ChangeLens — Project Plan

**AI-powered codebase change-impact analyzer.**
Given a Git diff, ChangeLens answers *"if I change this code, what else could break?"* — predicting affected files and tests, scoring risk with explainable signals, and serving that intelligence to AI coding agents over MCP.

---

## 1. Problem

Code changes have non-obvious blast radii. Developers and AI coding agents both struggle to know what a change actually affects: pure text search misses semantic relationships, and AI agents guess from the handful of files in their context. ChangeLens grounds that question in the repo's real structure.

The differentiator vs. "an AI code reviewer": ChangeLens fuses **structural dependency analysis** (a real call/import graph) with **ML semantic retrieval** (embeddings), rather than piping a diff to an LLM and hoping — and it **measures** how much each channel contributes.

---

## 2. Goals & Non-Goals

### Goals
- Analyze a Git diff and produce a ranked list of potentially affected files and tests.
- Explain *why* each item is affected: every result carries the dependency chain that reached it.
- Score change risk from transparent, defensible signals.
- Expose capabilities as an MCP server so AI coding agents can reason about change impact.
- Prove accuracy with a benchmark, and only add a component (embeddings, reranker, classifier) if the benchmark shows it helps.

### Non-Goals (v1)
- Multi-language support (Python-first; see §6 for the extension path).
- Automated code review / fix suggestions.
- Docs, issue-tracker, and monitoring-file analysis.
- A hosted multi-tenant SaaS deployment.
- A trained risk-severity model (replaced by an explainable score — see §5).

---

## 3. Core Architecture

```
          local repo / PR checkout
                    |
        git diff  ──┴──  git ls-files
            |                 |
     changed lines      Python ast parser
            |                 |
            |        symbols + reference graph ──── git log (volatility)
            |                 |                          |
            +──> changed symbols                         |
                      |                                  |
           Channel A: reverse-graph BFS                  |
           Channel B: embedding neighbours (§4, gated)   |
                      |                                  |
                 merged impact set ──> Explainable Risk Scorer
                                              |
                              +───────────────+──────────────+
                              |               |              |
                          MCP server         CLI     GitHub Action (uses CLI)
```

### Components
- **Indexer** — parses every tracked `.py` file with the stdlib `ast` module into symbols (modules, classes, functions, methods) with line spans, and resolves names through each file's imports to build a reference graph. A method's enclosing class is linked at zero cost, so callers that only hold an instance (`Calc().total()`) are still reached.
- **Analyzer** — maps diff lines onto the innermost enclosing symbol, then runs a 0-1 BFS over reverse edges for direct and transitive dependents. Each result records its depth and the chain that reached it.
- **Risk Scorer** — explainable score plus rule-based change-type labels (see §5).
- **Interfaces** — MCP server and CLI. Each CLI subcommand calls the same function as its MCP tool, so the two can't drift apart. The GitHub Action is a thin workflow around the CLI's Markdown output.

---

## 4. ML Layer (gated by the benchmark)

The structural channel ships first because it is exact where it applies. ML components are added **only when the benchmark shows a recall gap the graph cannot close** (dynamic dispatch, duck typing, pytest fixtures injected by name, string-based registries).

- **Feature Extraction / Sentence Similarity** — a local code-embedding model (sentence-transformers) embeds each symbol; the changed symbols' nearest neighbours become Channel B. Vectors are stored as float32 blobs and searched by brute-force cosine in numpy — fine up to ~100k symbols, no vector DB needed.
- **Text Ranking** — a cross-encoder reranker, only if the merged list's precision@k is the bottleneck.
- **Change-type classification** — rules first (path and AST based). A learned classifier is only worth it if labelled data appears and the rules measurably misfire.

All inference runs locally, so ChangeLens works on private repositories.

---

## 5. Risk Model (Design Decision)

Risk severity is a **transparent, explainable score**, not a trained classifier — there is no credible labelled dataset for HIGH/MEDIUM/LOW. Each signal contributes visible points (0–100 total):

| Signal | Measure | Max points |
|---|---|---|
| Fan-in | direct non-test dependents of the changed symbols (capped at 20) | 35 |
| Untested | share of changed non-test symbols no test reaches | 25 |
| Interface change | a public `def`/`class` signature line was touched | 25 |
| Volatility | share of recent commits touching these files that were fixes/reverts | 15 |
| Breaks a reference | code still uses a module-level name the diff removed or renamed, or imports from a deleted module or one whose import now fails. A certain `NameError`/`ImportError`, listed per symbol or per failing module with its root cause | 60 |

Levels: ≥60 high, ≥30 medium, else low; the total is capped at 100, so a certain break is always high. Weights are hand-set constants, tuned against the benchmark once it exists.

Change types (labels, not scored): `test-only`, `config`, `schema`, `breaking`, `api`, `logic`.

---

## 6. Tech Stack

- **Core:** Python 3.11+, stdlib `ast` + the `git` CLI (no tree-sitter or GitPython: for Python-only, `ast` is exact and free).
- **Interfaces:** `mcp` 2.x (`MCPServer`), argparse CLI, GitHub Actions.
- **Later, if the benchmark justifies it:** sentence-transformers, a cross-encoder, numpy, SQLite for a persisted index.

**Multi-language path:** tree-sitter replaces `ast` behind the same `Symbol` + reference-graph shape. It is deferred until a second language is actually in scope.

*Intentionally lean.* v1 rebuilds the index in memory on each call (seconds for a typical repo). Persistence and incremental re-indexing get added when a real repo shows the rebuild is too slow.

---

## 7. Workstreams

Vertical slice first, so every later piece is measured against something that works.

1. **Structural slice** ✅ — ast indexer, reference graph, diff → changed symbols → reverse BFS, affected files/tests with "why" chains.
2. **Risk scorer** ✅ — the four signals plus change-type rules.
3. **Interfaces** ✅ — MCP server and CLI exposing all six tools (§7.1).
4. **Benchmark** — the harness in §8, a grep baseline, and the first real numbers. *Do this next: every later decision depends on it.*
5. **Semantic channel** — embeddings as Channel B; keep it only if recall@k improves.
6. **GitHub PR bot** — workflow running `changelens analyze --base origin/main...HEAD` and posting/updating one PR comment.
7. **Hardening** — persisted/incremental index, pytest fixture resolution, docs. (Deleted-file tracing ✅ via the breaks signal.)
8. **Web UI** ✅ — see §9.

### 7.1 MCP tools

| Tool | Input | Returns | CLI |
|---|---|---|---|
| `analyze_change` | change | full report: changed symbols, affected files, tests, risk, untraced files | `analyze` |
| `find_affected_files` | change **or** `symbol` | non-test files that depend on it, starting files excluded | `files` |
| `find_related_tests` | change **or** `symbol` | pytest node ids, shallowest first | `tests` |
| `get_dependency_chain` | `symbol` | `depends_on` (forward) and `dependents` (reverse) | `deps` |
| `search_codebase` | `query` | symbols ranked by words in name, module path, docstring | `search` |
| `explain_risk` | change | score, level, change types, and points + reason per signal | `risk` |

Consistency rules, enforced by a test:
- A *change* is always `diff` text, else `git diff <base>` (default `HEAD`). Passing both `symbol` and `diff` is an error, not a silent choice.
- `find_affected_files`, `find_related_tests` and `explain_risk` on a change return exactly the matching slice of `analyze_change`, because `analyze_change` is assembled from the same functions.
- Every tool resolves symbols through one lookup: a qualified name or unique dotted suffix. Ambiguous or unknown names raise an error listing the candidates.
- Every result entry carries `depth` and `why` (the dependency chain), and `max_depth` / `limit` mean the same thing in every tool.
- `search_codebase` is lexical for now. The semantic channel (§4) replaces its internals without changing its signature.

---

## 8. Benchmark & Evaluation

The benchmark turns this from a demo into engineering, and it is a first-class deliverable.

Two ground-truth sources, both mechanical (no hand-labelling):

- **Mutation ground truth (primary).** Pick symbols in a well-tested repo, apply a small breaking mutation (e.g. make the function raise), run the test suite, and record which tests fail. Real and deterministic, and it scales to hundreds of samples per repo.
- **PR co-change ground truth (secondary).** For merged PRs in open-source Python repos, feed in the PR's non-test diff and check whether the predicted tests include the test files the PR itself modified. Noisier, but it reflects real developer changes.

The original "follow-up fix commit" idea was dropped as primary ground truth: linking a fix commit to the change that caused it is unreliable and would make the numbers easy to challenge.

- **Metrics:** recall@k and precision@k on affected tests, plus mean rank of the first true hit.
- **Baselines:** (a) `git grep` for changed symbol names, (b) direct importers only. Report the lift from each channel separately.

---

## 9. Interactive Web UI ✅ (was the primary stretch goal)

**Purpose:** an interactive console to explore impact visually, valuable for large changes where a flat list is hard to navigate.

**Built** (`changelens ui`, in `changelens/ui.py` + `changelens/ui.html`):
- **Graph:** the change at the centre, one ring per dependency step, with tests, will-break nodes and removed names (dashed "ghost" nodes) styled distinctly. It is drawn from the union of the results' `why` chains plus each break's `causes`, so it needs no extra backend data. Clicking a node fades everything except the chain that reached it.
- **Panels:** Impact (will-break, changed symbols, affected files with expandable chains), Tests (with a copy-pytest button), Risk (signals with point bars, will-break reasons) and History (past analyses, click to reload).
- **Modes:** a change (`git diff <base>`) or one symbol.

**Approach as built:** a stdlib `http.server` on 127.0.0.1 serving one static page and a JSON API over the same functions as the MCP tools. Cytoscape.js comes from cdnjs; if it fails to load, the lists still work. History is stored per repo in `.git/changelens/history.jsonl`, so it is never committed. Security: POST + JSON only (blocks cross-site form posts), a Host-header check (blocks DNS rebinding), and `base` may not start with `-` (blocks `git diff` option injection, shared with the MCP tools). Next.js only if the UI grows real routing or state.

**Next for the UI:** graph readability on large repos (collapse by file/module, filter by depth) once a real repo shows it's needed.

### Secondary Stretch Goals
- Multi-language support (JavaScript/TypeScript via tree-sitter).
- Issue / documentation linking.

---

## 10. Key Risks

- **Scope creep** — Mitigation: the benchmark gates every addition.
- **Dependency resolution accuracy** — dynamic imports, duck typing and pytest fixtures are invisible to static analysis. Mitigation: measure recall honestly; that gap is exactly what the semantic channel has to earn its place by closing.
- **Over-reach in large repos** — transitive BFS can flag half the codebase. Mitigation: depth limit (default 4), depth-ranked output, and precision@k in the benchmark.
- **Benchmark credibility** — Mitigation: mechanical ground truth, published methodology, honest baselines.

---

## 11. Résumé Framing

> Built a change-impact engine that fuses static dependency-graph traversal with ML semantic retrieval to predict the blast radius of a code change, served to AI coding agents over MCP; benchmarked with mutation-derived ground truth at X% recall@10 on affected-test prediction vs. a Y% grep baseline.
