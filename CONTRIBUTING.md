# Contributing

Nerve is contract-first. Changes should preserve bounded provider work, deterministic host behavior, fail-open integration semantics, and explicit provenance.

## Development setup

Nerve has no required runtime Python dependencies beyond the standard library. Release verification additionally uses PyYAML:

```bash
python -m pip install "PyYAML>=6,<7" "pytest>=8,<9" "tomli>=2,<3; python_version < '3.11'"
```

Nerve intentionally has **no installable project extras or dependency groups**. Hermes
uses them to populate its shared Python dependency environment during ordinary
`hermes update` operations. Model runtimes, including Laya and its PyTorch
dependencies, are installed only into explicit, isolated sidecar environments.
Install these test tools into a **separate developer environment**, not Hermes.

Before opening a PR, run:

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
python scripts/verify_release.py
```

CI runs these checks on Python 3.10, 3.11, 3.12, 3.13, and 3.14.

## Change discipline

For a new contract or recipe, include:

1. explicit allowed outputs;
2. criteria for every output;
3. deterministic policy around confidence/failure;
4. privacy classification of state fields;
5. fixtures covering ambiguous and adversarial cases;
6. benchmark/eval methodology if making quality claims.

For bug fixes:

1. reproduce the defect first;
2. add a regression test at the narrowest public seam that proves the failure;
3. make the smallest fix that addresses the root cause;
4. rerun the full offline suite;
5. keep provider/live claims separate from wire-contract or mocked verification.

Avoid silently increasing provider fan-out, weakening fail-open behavior, or making unrecoverable evidence less protected.

## Provider testing

Use synthetic state for live provider checks. Never include API keys, bearer tokens, private prompts, or unsanitized transcripts in issues, PRs, screenshots, or fixtures.

When reporting a live check, identify:

- Nerve version / full commit SHA;
- Hermes Agent version;
- provider / transport;
- returned provider/model identifiers;
- sanitized result and latency;
- whether the result is interoperability evidence, benchmark data, or something else.

## Pull requests

Keep one coherent change per PR. Include exact verification commands and results.

If AI substantially assisted with implementation, review, test generation, or PR text, disclose that assistance and state what was independently verified. AI assistance is not a substitute for reproducing bugs, running tests, checking CI, or validating provider claims.
