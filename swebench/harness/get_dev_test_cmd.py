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
    "pro": "ScaleAI/SWE-bench_Pro",
}

# Simplified test commands — strip verbose/summary flags, focus on failures only.
_TEST_CMD_OVERRIDES = {
    "pytest --no-header -rA --tb=no -p no:cacheprovider": "pytest -q --tb=short",
    "pytest -rA --tb=long -p no:cacheprovider": "pytest -q --tb=short",
    "pytest -rA": "pytest -q --tb=short",
    "pytest -rA --tb=long": "pytest -q --tb=short",
    "pytest -rA -vv -o console_output_style=classic --tb=no": "pytest -q --tb=short",
    "tox --current-env -epy39 -v --": "pytest -q --tb=short",
    "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1": "./tests/runtests.py --parallel 1",
    "./tests/runtests.py --verbosity 2": "./tests/runtests.py",
    "pytest --no-header -rA": "pytest -q --tb=short --no-header",
    "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose": "bin/test",
}

OUTPUT_PATH = Path(
    "/home/ruixinw/debugmaster_artifact/aclarr_experiments/swebench-test-cmd-format"
)


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


def resolve_dataset_names(
    dataset_names: list[str] | None, subsets: list[str] | None
) -> list[str]:
    """Return a list of HuggingFace dataset paths from CLI args.

    Accepts multiple ``--dataset_name`` and/or ``--subset`` values so that
    Verified + Pro (or any combination) can be processed in one invocation.
    """
    resolved: list[str] = []
    for name in dataset_names or []:
        resolved.append(name)
    for subset in subsets or []:
        key = subset.lower()
        if key not in SUBSET_TO_DATASET:
            raise ValueError(
                f"Unsupported subset '{subset}'. Choose from: {', '.join(sorted(SUBSET_TO_DATASET.keys()))}"
            )
        resolved.append(SUBSET_TO_DATASET[key])
    if not resolved:
        resolved.append(SUBSET_TO_DATASET["verified"])
    return resolved


def _sanitize_dataset_name(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _is_pro_dataset(dataset_path: str) -> bool:
    return "pro" in dataset_path.lower()


def _process_pro_instance(instance) -> dict:
    """Derive test command info for a SWE-bench Pro instance."""
    import ast

    instance_id = instance[KEY_INSTANCE_ID]
    repo = instance["repo"]

    selected_raw = instance.get("selected_test_files_to_run", "[]")
    try:
        selected = ast.literal_eval(selected_raw) if isinstance(selected_raw, str) else selected_raw
    except Exception:
        selected = []

    fail_raw = instance.get("fail_to_pass", "[]")
    try:
        fail_to_pass = ast.literal_eval(fail_raw) if isinstance(fail_raw, str) else fail_raw
    except Exception:
        fail_to_pass = []

    test_cmd = "pytest -q --tb=short"
    if selected:
        # Strip ::test_case suffixes to get file paths only.
        files = sorted({s.split("::")[0] for s in selected})
        full_failing = f"{test_cmd} {' '.join(files)}"
    elif fail_to_pass:
        files = sorted({f.split("::")[0] for f in fail_to_pass})
        full_failing = f"{test_cmd} {' '.join(files)}"
    else:
        full_failing = None

    return {
        "instance_id": instance_id,
        "repo": repo,
        "version": None,
        "test_cmd_format": "string",
        "test_cmd": test_cmd,
        "full_failing_test_cmd": full_failing,
    }


def _process_standard_instance(instance) -> dict:
    """Process a standard SWE-bench instance (Verified / Lite / Full)."""
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
            "full_failing_test_cmd": None,
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

    return {
        "instance_id": instance_id,
        "repo": repo,
        "version": version,
        "test_cmd_format": _test_cmd_format(test_cmd),
        "test_cmd": test_cmd,
        "full_failing_test_cmd": full_failing,
    }


def main(
    dataset_names: list[str] | None,
    subsets: list[str] | None,
    split: str,
    output: str | None = None,
    language: str | None = None,
) -> Path:
    resolved_datasets = resolve_dataset_names(dataset_names, subsets)

    results: dict[str, dict] = {}

    for dataset_path in resolved_datasets:
        print(f"Loading {dataset_path} split={split} ...")
        dataset = load_swebench_dataset(dataset_path, split)
        is_pro = _is_pro_dataset(dataset_path)
        skipped = 0

        for instance in dataset:
            if language and "repo_language" in instance and instance["repo_language"].lower() != language.lower():
                skipped += 1
                continue
            instance_id = instance[KEY_INSTANCE_ID]
            if is_pro:
                version = instance.get("version")
                spec = MAP_REPO_VERSION_TO_SPECS.get(instance["repo"], {}).get(version) if version else None
                if spec is None or "test_cmd" not in spec:
                    results[instance_id] = _process_pro_instance(instance)
                    continue
            results[instance_id] = _process_standard_instance(instance)

        kept = len(dataset) - skipped
        msg = f"  -> {kept}/{len(dataset)} instances from {dataset_path}"
        if skipped:
            msg += f" ({skipped} skipped by --language {language})"
        print(msg)

    if output:
        output_path = Path(output)
    else:
        tag = "+".join(_sanitize_dataset_name(d) for d in resolved_datasets)
        output_path = OUTPUT_PATH / f"{tag}-{split}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2))
    print(f"Wrote {len(results)} total records to {output_path}")
    return output_path


if __name__ == "__main__":
    parser = ArgumentParser(
        description="Export dev test commands for each SWE-bench instance."
    )
    parser.add_argument(
        "--dataset_name",
        type=str,
        nargs="*",
        default=None,
        help="One or more HF dataset paths (e.g. SWE-bench/SWE-bench_Verified ScaleAI/SWE-bench_Pro).",
    )
    parser.add_argument(
        "--subset",
        type=str,
        nargs="*",
        default=None,
        help="One or more subset shortcuts: verified, lite, full, pro.  E.g. --subset verified pro",
    )
    parser.add_argument("--split", type=str, default="test", help="Dataset split")
    parser.add_argument(
        "--language",
        type=str,
        default=None,
        help="Filter instances by repo_language (e.g. python, go, js). Only applies to datasets that have this field.",
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        help="Output file path. Defaults to auto-generated name under OUTPUT_PATH.",
    )
    args = parser.parse_args()
    main(
        dataset_names=args.dataset_name,
        subsets=args.subset,
        split=args.split,
        output=args.output,
        language=args.language,
    )
