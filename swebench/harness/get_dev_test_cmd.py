from __future__ import annotations

import json

from argparse import ArgumentParser
from pathlib import Path

from swebench.harness.constants import (
    END_TEST_OUTPUT,
    KEY_INSTANCE_ID,
    MAP_REPO_VERSION_TO_SPECS,
    START_TEST_OUTPUT,
)
from swebench.harness.test_spec.test_spec import make_test_spec
from swebench.harness.utils import load_swebench_dataset


SUBSET_TO_DATASET = {
    "verified": "SWE-bench/SWE-bench_Verified",
    "lite": "SWE-bench/SWE-bench_Lite",
    "full": "SWE-bench/SWE-bench",
}

# Simplified test commands — strip verbose/summary flags, focus on failures only.
_TEST_CMD_OVERRIDES = {
    "pytest --no-header -rA --tb=no -p no:cacheprovider": "pytest --disable-warnings --runxfail -q --tb=short",
    "pytest -rA --tb=long -p no:cacheprovider": "pytest --disable-warnings --runxfail -q --tb=short",
    "pytest -rA": "pytest --disable-warnings --runxfail -q --tb=short",
    "pytest -rA --tb=long": "pytest --disable-warnings --runxfail -q --tb=short",
    "pytest -rA -vv -o console_output_style=classic --tb=no": "pytest --disable-warnings --runxfail -q --tb=short",
    "tox --current-env -epy39 -v --": "pytest --disable-warnings --runxfail -q --tb=short",
    "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1": "./tests/runtests.py --parallel 1",
    "./tests/runtests.py --verbosity 2": "./tests/runtests.py",
    "pytest --no-header -rA": "pytest --disable-warnings --runxfail -q --tb=short --no-header",
    "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose": "bin/test -C --no-subprocess",
}

# Instances where the full test file hangs or times out but individual
# FAIL_TO_PASS tests run fine.  For these, use the FAIL_TO_PASS nodeids
# directly as failing_test_directives instead of the whole file.
_USE_FAIL_TO_PASS_AS_DIRECTIVES = [
    "psf__requests-2317",
]

OUTPUT_PATH = Path(__file__).resolve().parents[4] / "assets" / "swebench-test-specs" / "verified.json"


def _test_cmd_format(test_cmd) -> str:
    if isinstance(test_cmd, list):
        return "list"
    if isinstance(test_cmd, str):
        return "string"
    return type(test_cmd).__name__


def _computed_test_commands(instance):
    test_spec = make_test_spec(instance)
    eval_script = test_spec.eval_script
    if START_TEST_OUTPUT not in eval_script or END_TEST_OUTPUT not in eval_script:
        return []

    content = eval_script.split(START_TEST_OUTPUT, 1)[1].split(END_TEST_OUTPUT, 1)[0]
    commands = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line in {"'", '"'} or line.startswith(": '"):
            continue
        commands.append(line)
    return commands


def _extract_directives(test_cmd: str, full_failing) -> str | list[str] | None:
    """Extract test directives by stripping test_cmd from full_failing_test_cmd.

    Returns the pure test file paths / test labels with test_cmd removed.
    """
    if full_failing is None:
        return None

    def _strip_one(cmd: str) -> str:
        if cmd.startswith(test_cmd):
            return cmd[len(test_cmd):].strip()
        # Handle test_cmd with shell suffixes (e.g. "pytest -q || echo ...")
        # where files are inserted before the suffix.
        for sep in (" || ", " && ", " ; "):
            if sep in test_cmd:
                base, suffix = test_cmd.split(sep, 1)
                tail = sep + suffix
                if cmd.startswith(base) and cmd.endswith(tail):
                    return cmd[len(base):len(cmd) - len(tail)].strip()
        return cmd

    if isinstance(full_failing, str):
        return _strip_one(full_failing)
    elif isinstance(full_failing, list):
        return [_strip_one(cmd) for cmd in full_failing]
    return full_failing


def _process_standard_instance(instance) -> dict:
    instance_id = instance[KEY_INSTANCE_ID]
    repo = instance["repo"]
    version = instance.get("version")

    spec = MAP_REPO_VERSION_TO_SPECS.get(repo, {}).get(version)
    if spec is None or "test_cmd" not in spec:
        return {
            "instance_id": instance_id,
            "repo": repo,
            "version": version,
            "test_cmd_format": "missing",
            "test_cmd": None,
            "failing_test_directives": None,
            "env": {},
        }

    original_test_cmd = spec["test_cmd"]
    test_cmd = _TEST_CMD_OVERRIDES.get(original_test_cmd, original_test_cmd)

    computed_cmds = _computed_test_commands(instance)
    full_failing = computed_cmds[0] if len(computed_cmds) == 1 else computed_cmds

    if original_test_cmd != test_cmd:
        if isinstance(full_failing, str):
            full_failing = full_failing.replace(original_test_cmd, test_cmd, 1)
        elif isinstance(full_failing, list):
            full_failing = [cmd.replace(original_test_cmd, test_cmd, 1) for cmd in full_failing]

    if instance_id in _USE_FAIL_TO_PASS_AS_DIRECTIVES:
        fail_to_pass = json.loads(instance.get("FAIL_TO_PASS", "[]"))
        directives = " ".join(fail_to_pass)
    else:
        directives = _extract_directives(test_cmd, full_failing)

    return {
        "instance_id": instance_id,
        "repo": repo,
        "version": version,
        "test_cmd_format": _test_cmd_format(test_cmd),
        "test_cmd": test_cmd,
        "failing_test_directives": directives,
        "env": {},
    }


def main(
    subset: str,
    split: str,
    output: str | None = None,
) -> Path:
    dataset_path = SUBSET_TO_DATASET.get(subset, subset)
    print(f"Loading {dataset_path} split={split} ...")
    dataset = load_swebench_dataset(dataset_path, split)

    results = {instance[KEY_INSTANCE_ID]: _process_standard_instance(instance) for instance in dataset}

    output_path = Path(output) if output else OUTPUT_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2))
    print(f"Wrote {len(results)} records to {output_path}")
    return output_path


if __name__ == "__main__":
    parser = ArgumentParser(
        description="Export dev test commands for each SWE-bench instance."
    )
    parser.add_argument(
        "--subset",
        type=str,
        default="verified",
        help="Subset: verified, lite, full, or a HF dataset path.",
    )
    parser.add_argument("--split", type=str, default="test", help="Dataset split")
    parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        help=f"Output file path. Defaults to {OUTPUT_PATH}.",
    )
    args = parser.parse_args()
    main(subset=args.subset, split=args.split, output=args.output)
