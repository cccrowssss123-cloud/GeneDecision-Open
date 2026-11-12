# GeneDecision

GeneDecision is an early-stage Python toolkit for exploring candidate genes from local research tables. It inspects tabular data, audits explicit identifier mappings, and evaluates caller-declared category membership and numerical conditions. Input rows, provenance and unresolved values stay visible in the results.

**Project status:** Development is currently paused. This snapshot is available for use and extension; there is no active release schedule.

## Try it with synthetic data

Use Python 3.11 or newer. From this checkout, install the package and run either example:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install .
python examples/discovery_categories_demo.py
python examples/discovery_numeric_demo.py
```

Both examples build their own invented inputs and run locally without external services. To inspect the structure of a local table:

```sh
python -m genedecision.discovery path/to/table.csv
```

See the [Discovery walkthrough](docs/DISCOVERY_WORKFLOW.md), [column and identifier contracts](docs/DISCOVERY_COLUMN_CONTRACT_V1.md), and [exploratory numerical conditions](docs/DISCOVERY_NUMERIC_6B4B.md) for the public interfaces and examples. The repository also contains experimental evidence and interpretation components. The documented, tested workflow for this snapshot is local Discovery.

## Tests

```sh
python -m pip install 'pytest>=8,<9'
python -m pytest
```

The included Discovery tests use synthetic data. Exploratory matches are scientifically unverified and do not establish significance, causality or eligibility. Keep unpublished datasets, databases, outputs and credentials outside the repository and out of issues and pull requests.

## License

The code and included documentation are available under the [MIT License](LICENSE). External data and services are subject to their own terms.
