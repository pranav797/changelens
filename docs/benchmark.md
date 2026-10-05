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

**Held-out repos.** click and jinja were used to *develop* the ranking, text channel and depth. These two, from other organisations, were benchmarked only afterwards, with no tuning on them:

**marshmallow** (marshmallow-code/marshmallow @ 1c63bda, 646 tests):

| Method | Recall@5 | Recall@10 | Recall | Any hit | Precision | Suite run | Predictions |
|---|---|---|---|---|---|---|---|
| changelens | 71% | 74% | 100% | 100% | 35% | 45% | 294 |
| changelens-files | 100% | 100% | 100% | 100% | 60% | 70% | 7.1 |
| grep | 42% | 38% | 31% | 48% | 34% | 18% | 2.1 |
| importers | 51% | 75% | 76% | 80% | 37% | 55% | 6.8 |

**rich** (Textualize/rich @ 9d8f9a3, 719 tests; 8 tests already fail on Windows and are ignored):

| Method | Recall@5 | Recall@10 | Recall | Any hit | Precision | Suite run | Predictions |
|---|---|---|---|---|---|---|---|
| changelens | 67% | 72% | 100% | 100% | 17% | 56% | 404 |
| changelens-files | 95% | 94% | 100% | 100% | 28% | 66% | 36.1 |
| grep | 50% | 44% | 38% | 60% | 39% | 18% | 4.9 |
| importers | 81% | 86% | 74% | 95% | 62% | 18% | 7.5 |

On both held-out repos ChangeLens finds **100%** of failing tests. In rich, many tests live in files named after the module they test, so the importers baseline (whole files) is strong on recall@10. ChangeLens's file-level list still wins on top 10 files in marshmallow (100% vs 75%) and is close in rich (94% vs 86%), while finding everything rather than 74–76%.

**playground** (this repo's `playground/`, 11 tests, 14 covered mutants): ChangeLens recall@10 100%, recall 100%; grep 46%; importers 27%.

### With per-test coverage (`bench --cov`)

One pytest-cov run of the unmutated code (`--cov-context=test`), imported with `changelens coverage`, marks the tests that actually executed each symbol; those rank first.

| Recall@10 | static | + coverage |
|---|---|---|
| jinja | 71% | **96%** |
| click | CLICK_STATIC | **CLICK_COV** |

Caveat: a "raise on entry" mutant fails every test that runs the function, so coverage is near-exact for this benchmark. Read these as an upper bound for subtler changes, not a typical number.

### PR co-change ground truth (`bench --prs`)

Replays recent commits that change both source and existing tests. It rebuilds each commit's parent tree plus only its *source* changes (so the index can't read the commit's own tests), then checks whether the predicted test files include the ones the commit modified. These are real developer changes, not injected breaks. 30 commits per repo, test-file level:

| | click recall@10 | click recall | jinja recall@10 | jinja recall |
|---|---|---|---|---|
| ChangeLens | **87%** | **99%** | **78%** | **100%** |
| grep | 53% | 69% | 38% | 61% |
| importers | 50% | 53% | 45% | 55% |

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
- **Precision by pruning hubs** (stop expanding through nodes with more than H dependents): recall collapses (jinja 96% → 53% at H=200, click 100% → 66% at H=100). The hubs *are* the real failure paths: breaking a core `Environment` or `Command` method genuinely breaks hundreds of tests.
- **A learned ranker** (logistic regression on 7 features: depth, name affinity, text channel, hub cost, last-hop hubness, support, module-level), evaluated leave-one-repo-out on click/jinja/marshmallow: recall@10 *dropped* 10–11 points on click and jinja and tied on marshmallow. A grid search adding one small extra weight to the current rule never beat it on a held-out repo either. The simple rule generalises better than anything fitted, which is why runtime coverage (above) is the precision lever.
- **Duck-typed callers as test seeds:** matching `gateway.charge()` to a changed `Gateway.charge` added no recall on any of the four repos (already 96–100%) and cost up to 3 points of recall@10. It now informs *affected files* only (`channel: duck`).

## Reproduce

```bash
git clone https://github.com/pallets/click && cd click
uv venv && uv pip install -e . pytest pytest-cov
cd /path/to/changelens
uv run changelens bench --repo /path/to/click --test-cmd ".venv/Scripts/python -m pytest" -n 40 --out click.json
uv run changelens bench --repo /path/to/click --test-cmd ".venv/Scripts/python -m pytest" -n 40 --cov   # + coverage
git -C /path/to/click fetch --deepen=600 && uv run changelens bench --repo /path/to/click --prs -n 30  # co-change
```

Use `.venv/bin/python` on macOS/Linux. Extra test dependencies: jinja `trio`, marshmallow `simplejson tzdata`, rich `attrs`. A mutation run takes roughly mutants × (suite time + a few seconds).

## Limits of this benchmark

- **One mutation operator.** "Raise on entry" models a hard break. Subtle behaviour changes break fewer tests, and those may be harder to predict.
- **Four real repos.** Two were used for development and two are held out. That's enough to show the gains generalise, not enough for tight confidence intervals (40 mutants each).
- **Co-change ground truth is noisy.** Commits also touch tests for unrelated reasons, and merge commits bundle several changes; it measures "would a developer have looked here", not "would this fail".
