# ChangeLens benchmark

**Question:** when a function changes in a way that breaks it, does ChangeLens name the tests that actually fail?

## Method (`changelens bench`)

1. Pick functions at random (seeded) from the repo's non-test code.
2. **Mutate** one function so it raises on entry (`raise RuntimeError("changelens mutant")` right after the docstring), and run the **real test suite**. The tests that fail (minus any that already fail at baseline) are the ground truth. Functions no test exercises are skipped, since there's nothing to recall.
3. Ask each predictor for a ranked test list for the same change:
   - **changelens**: `analyze` on the mutation's diff.
   - **grep**: test files whose text contains the function's name (what a developer would grep for).
   - **importers**: test files that directly import the function's module.
4. Restore the file byte for byte and repeat. The repo must have no uncommitted changes to tracked files.

**Metrics**, averaged over mutants:

| Metric | Meaning |
|---|---|
| Recall@k | failing tests covered by the top k predictions, out of min(k, failures). A file-level prediction covers every test in the file. |
| Recall | share of failing tests covered by the whole prediction list |
| Any hit | share of mutants where at least one failing test was predicted |
| Precision | share of predictions that cover at least one failing test |
| Suite run | share of the whole suite you'd run if you ran every prediction |

## Results

40 covered mutants per repo (seed 0), default settings (depth 5).

**jinja** (pallets/jinja @ 5ef7011, 677 tests):

| Method | Recall@5 | Recall@10 | Recall | Any hit | Precision | Suite run | Predictions |
|---|---|---|---|---|---|---|---|
| changelens | 70% | 69% | 96% | 100% | 33% | 68% | 465 |
| grep | 25% | 25% | 18% | 25% | 20% | 4% | 0.7 |
| importers | 46% | 44% | 26% | 52% | 38% | 8% | 1.9 |

**click** (pallets/click @ 2247b35, 602 tests):

| Method | Recall@5 | Recall@10 | Recall | Any hit | Precision | Suite run | Predictions |
|---|---|---|---|---|---|---|---|
| changelens | 56% | 56% | 100% | 100% | 30% | 62% | 385 |
| grep | 50% | 46% | 31% | 55% | 46% | 12% | 2.2 |
| importers | 57% | 59% | 37% | 62% | 44% | 11% | 2.1 |

**Like for like, at file granularity.** The baselines predict test *files*, and one file entry covers every test in it, so their recall@k is flattered against ChangeLens's test-level list. Collapsing ChangeLens's ranking to files (in rank order; `changelens-files` in `bench` output) gives the fair comparison:

| Top-10 test files contain… | click | jinja |
|---|---|---|
| ChangeLens (files) | **96%** | **97%** |
| importers | 59% | 44% |
| grep | 46% | 25% |

(Top 5: ChangeLens 94% on both repos.)

**playground** (this repo's `playground/`, 11 tests, 14 covered mutants): ChangeLens recall@10 100%, recall 100%; grep 46%; importers 27%.

**How to read this.** ChangeLens finds nearly every failing test (96–100% recall) where the baselines find a fifth to a third. The price is breadth: on these repos, hub classes like click's `Command` or jinja's `Environment` connect most code to most tests, so the full list covers 50–70% of the suite. That's why ranking matters. An agent sees the top `limit` (50 by default). At test level, 56–69% of the best possible top 10 are tests that really fail; at file level, the top 10 files hold 96–97% of the failures.

## What the benchmark changed

Every row is a finding from running the benchmark and the fix that followed it. Numbers come from offline re-scoring of recorded failures (`recall@10` / overall recall).

| Finding | Fix | Effect |
|---|---|---|
| `from shop import checkout` resolved to the submodule, not the function `__init__` re-exports under that name | prefer the package's re-export over a same-named submodule | playground recall 82% → 93% |
| Tests reached only through a pytest fixture were invisible | resolve fixtures: parameters, `usefixtures`, autouse, nearest `conftest.py` | playground → 100% |
| recall@k was capped near 3% when a mutant breaks 300 tests | divide by min(k, failures) | metric fix |
| click: depth-only ranking left real failures outside the top 10 once a hub class pulled in half the suite | rank by depth minus 2 × name affinity (words shared by the changed symbol and the test id) | click recall@10 44% → 57% |
| jinja: 15% of mutants had zero hits, reached via templates, string-keyed registries and `getattr` dispatch | text channel: tests whose source names the changed code or a direct user of it | jinja recall 83% → 93%, any hit 85% → 98% |
| click: 4% of failures sat beyond 4 dependency steps (`--help` paths) | default depth 4 → 5 | click recall 96% → 99.7%, jinja 93% → 96%; +7–8% of suite |
| Parametrize ids with spaces were cut short in pytest's summary | normalise at the first `[` | metric fix |

**Tried and rejected** (no measurable gain, so not shipped):
- Hub-degree path costs and a method→class penalty in ranking: ±1 point.
- TF-IDF re-ranking over identifiers: +0–3 points.
- A real embedding model (model2vec `potion-base-8M`, cosine between the changed symbol and each test): +1–2 points on both repos, within noise for 40 mutants, and not worth a ~30MB model plus new dependencies.
- Depth 3: −20 to −25 points of recall.

## Reproduce

```bash
git clone https://github.com/pallets/click && cd click
uv venv && uv pip install -e . pytest
cd /path/to/changelens
uv run changelens bench --repo /path/to/click --test-cmd ".venv/Scripts/python -m pytest" -n 40 --out click.json
```

Use `.venv/bin/python` on macOS/Linux. jinja additionally needs `trio` for its async tests. A run takes roughly mutants × (suite time + a few seconds).

## Limits of this benchmark

- **One mutation operator.** "Raise on entry" models a hard break. Subtle behaviour changes break fewer tests, and those may be harder to predict.
- **Two real repos plus a toy one.** Both real repos are from the same organisation (Pallets) with similar style. More repos, especially ones with heavier dynamic dispatch, would test the ranking further.
- **PR co-change ground truth** (plan §8, secondary) isn't built yet.
