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

### Reproduce the installed wheel check

On pull requests and pushes to `main`, CI builds a wheel on Ubuntu with Python 3.11–3.14, installs it into a fresh environment, and runs the synthetic tests and both demos. Dependency installation uses public packages; the tests and demos need no external services, credentials or research data.

To reproduce one matrix entry locally, run this from the checkout with the corresponding Python on your `PATH`:

```sh
(
  set -eu
  check_dir=$(mktemp -d)
  trap 'rm -rf "$check_dir"' EXIT
  python -m pip --isolated wheel --no-deps --wheel-dir "$check_dir/dist" .
  python -m venv "$check_dir/venv"
  wheel_python="$check_dir/venv/bin/python"
  "$wheel_python" -m pip --isolated install \
    "$check_dir"/dist/genedecision-*.whl 'pytest>=8,<9'
  mkdir "$check_dir/check"
  cp -R tests examples pyproject.toml "$check_dir/check/"
  cd "$check_dir/check"
  unset PYTHONPATH
  export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
  test ! -e src
  "$wheel_python" -I - <<'PY'
import sysconfig
from importlib.metadata import distribution
from pathlib import Path

import genedecision

expected = Path(sysconfig.get_path("purelib"), "genedecision", "__init__.py").resolve()
installed = Path(distribution("genedecision").locate_file("genedecision/__init__.py")).resolve()
actual = Path(genedecision.__file__).resolve()
if not actual == installed == expected:
    raise SystemExit(f"Expected the installed wheel at {expected}, got {actual}")
print(f"Testing installed package at {actual}")
PY
  "$wheel_python" -I -m pytest -o pythonpath= -m 'not live' tests
  "$wheel_python" -I examples/discovery_categories_demo.py
  "$wheel_python" -I examples/discovery_numeric_demo.py
)
```

The origin check confirms that Python imports the package from the fresh environment. The temporary test directory has no `src/` tree, and the pytest override disables the checkout's usual source import path. The CLI subprocess test therefore cannot fall back to the checkout either.

## License

The code and included documentation are available under the [MIT License](LICENSE). External data and services are subject to their own terms.
