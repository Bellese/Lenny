"""Inventory guard for `settings.MEASURE_ENGINE_URL` reads (issue #397).

Issue #397 is a bug CLASS, not a single bug: reading the env var instead of the
active MCS connection means Lenny silently operates on its own local container
while the user is looking at a different server. It is being fixed in slices, so
this file pins the exact inventory of remaining reads per file.

Two things it buys:

1. **No new reads, and no new defaulted targets.** Both an added
   `settings.MEASURE_ENGINE_URL` access and a call that omits its target keyword
   fail this suite. The second check exists because the first one alone reported a
   clean inventory while seven unscoped calls sat in validation.py (#397 slice 3).
2. **Progress is enforced, not asserted in prose.** Each slice that fixes sites
   must lower the numbers here, and the docstring below says which remaining
   reads are legitimate and which are known-outstanding defects.

Counting uses `ast`, not grep: the file is thick with prose mentioning the env
var in docstrings and comments, and a textual match cannot tell those from a real
attribute access.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# Per-file count of REAL attribute accesses (plus the declaration in config.py).
#
# LEGITIMATE — these are the "Not defects" list from issue #397. Do not "fix" them:
#   config.py            1  the setting's own declaration
#   dependencies.py      1  get_active_mcs fallback when no mcs_configs row is active.
#                           The single legitimate home for the env-var default.
#   main.py              2  startup backfill of Job.mcs_url on legacy rows, and seeding
#                           the built-in "Local Measure Engine" connection row. This is
#                           how the env var BECOMES a connection, so it must read it.
#   routes/settings.py   1  factory reset's no-active-row fallback, mirroring the CDR
#                           block's settings.DEFAULT_CDR_URL fallback (#397 slice 1).
#   orchestrator.py      1  _get_mcs_url fallback for legacy Job rows with NULL mcs_url.
#   routes/jobs.py       1  SAME legacy-Job fallback in the comparison endpoint. The
#                           #397 defect there is fixed — it reads job.mcs_url first —
#                           but a job predating the snapshot column has NULL, and
#                           without the fallback every historical comparison 502s.
#   routes/results.py    1  SAME legacy-Job fallback for the pre-snapshot
#                           evaluated-resource path. Identical reasoning.
#   fhir_client.py       3  of the 5: the _validate_ssrf_url allowlist (must include the
#                           env-var host), and the target_url/measure_engine_url
#                           back-compat defaults on push_resources and evaluate_measure.
#   bundle_loader.py     2  the _wait_for_hapi boot readiness probe (unrelated to MCS
#                           scoping — it just checks both HAPI containers are up), and
#                           the explicit McsTarget built for the boot seed path (task 5):
#                           seeding deliberately targets Lenny's own engine, so building
#                           that target from settings, with a comment saying why, is the
#                           legitimate way for the env var to become a connection here.
#   validation.py        1  run_validation's legacy-NULL fallback (`run_row.mcs_url or
#                           settings.MEASURE_ENGINE_URL`), added in task 7, mirroring the
#                           same legacy-Job fallback pattern in orchestrator.py,
#                           routes/jobs.py, and routes/results.py.
#
# KNOWN-OUTSTANDING DEFECTS — each must drop to 0 as its slice lands: none remain in
# this file. Task 8 cleared the last one (the wipe).
#
# CLEARED BY SLICE 2 (job-scoped):
#   fhir_client.py       5 -> 3  $data-requirements now takes the job's MCS from the
#                               strategy constructor, and resolve_evaluated_resource's
#                               env-var fallback is gone (base_url is required). The
#                               remaining 3 are the legitimate ones listed above.
#   routes/jobs.py       1 -> 1  count unchanged, but the read CHANGED MEANING: it was
#                               the unconditional target, it is now only the
#                               legacy-NULL fallback behind job.mcs_url. A count alone
#                               cannot see that, which is why the reasons above matter
#                               more than the numbers.
#
# CLEARED BY SLICE 3 (task 3): validation.py 9 -> 6. The three terminology helpers
# (_find_existing_valueset_id, _find_existing_codesystem_id, _delete_existing_valueset)
# now take a required McsTarget.
#
# CLEARED BY SLICE 3 (task 4): validation.py 6 -> 3. _assert_no_canonical_url_clash
# and _resolve_measure_id now take a required McsTarget. The remaining 3 were the
# valueset-expansion call (878), the wipe (1239), and the patient-name lookup (1359).
#
# CLEARED BY SLICE 3 (task 5): validation.py 3 -> 2. triage_test_bundle and
# _reload_measures_from_seed_bundles now take a required McsTarget, and their
# measure-engine pushes plus the valueset-expansion call use mcs.url explicitly.
# The remaining 2 (the wipe and the patient-name lookup) clear in tasks 7 and 8.
# bundle_loader.py 1 -> 2: the boot seed path now builds an explicit McsTarget from
# settings instead of letting triage_test_bundle's (now-required) mcs default
# implicitly — a second legitimate read alongside the existing _wait_for_hapi probe.
#
# SLICE 3 (task 7): validation.py 2 -> 2. The count does NOT move, and that is the
# interesting part — a number standing still is exactly where a reader assumes nothing
# happened. run_validation now resolves its target from the run's MCS snapshot and
# threads it through every measure-engine call, which REMOVED the patient-name lookup's
# read and ADDED one: the `run_row.mcs_url or settings.MEASURE_ENGINE_URL` fallback for
# runs created before the snapshot column existed. Net zero. That new read is
# legitimate, mirroring the same legacy-NULL fallback in orchestrator.py, routes/jobs.py
# and routes/results.py.
#
# SLICE 3 (task 8): validation.py 2 -> 1. The wipe now takes mcs.url (and mcs.auth_headers)
# instead of settings.MEASURE_ENGINE_URL, scoped by default via wipe_patients_by_id and
# falling back to the unfiltered wipe_patient_data only when mcs.wipe_before_job is set.
# The only remaining read is the legacy-NULL fallback from task 7, now listed above as
# legitimate — this file has zero outstanding #397 defects.
#
# When a slice lands, lower the number here in the same commit. A count that is
# too HIGH fails just as loudly as one that is too low — a stale expectation is
# how an inventory guard quietly stops guarding.
EXPECTED_READS: dict[str, int] = {
    "app/config.py": 1,
    "app/dependencies.py": 1,
    "app/main.py": 2,
    "app/routes/jobs.py": 1,
    "app/routes/results.py": 1,
    "app/routes/settings.py": 1,
    "app/services/bundle_loader.py": 2,
    "app/services/fhir_client.py": 3,
    "app/services/orchestrator.py": 1,
    "app/services/validation.py": 1,
}

_APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"


def _count_reads() -> dict[str, int]:
    """Count real `X.MEASURE_ENGINE_URL` accesses per file, ignoring prose."""
    counts: dict[str, int] = {}
    for path in sorted(_APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        hits = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "MEASURE_ENGINE_URL":
                hits += 1
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "MEASURE_ENGINE_URL"
            ):
                hits += 1
        if hits:
            rel = path.relative_to(_APP_ROOT.parent).as_posix()
            counts[rel] = hits
    return counts


def test_measure_engine_url_reads_match_the_inventory():
    """The set of files reading the env var, and how often, is pinned.

    A failure here is not necessarily a bug — it means the inventory moved. Read
    the docstring in EXPECTED_READS, decide whether your new read belongs in the
    legitimate list or is an instance of the #397 defect, and update the number
    in the same commit that changed the code.
    """
    actual = _count_reads()

    new_files = sorted(set(actual) - set(EXPECTED_READS))
    assert not new_files, (
        f"New file(s) reading settings.MEASURE_ENGINE_URL: {new_files}. "
        "Route through the active MCS connection (see get_active_mcs) instead, or "
        "add the file to EXPECTED_READS with a reason if the read is legitimate."
    )

    gone = sorted(set(EXPECTED_READS) - set(actual))
    assert not gone, f"File(s) no longer read the env var: {gone}. Good — remove them from EXPECTED_READS."

    mismatches = {f: (EXPECTED_READS[f], actual[f]) for f in EXPECTED_READS if actual[f] != EXPECTED_READS[f]}
    assert not mismatches, (
        "MEASURE_ENGINE_URL read count changed (expected, actual): "
        f"{mismatches}. If you fixed sites, lower the number. If you added one, "
        "route it through the active MCS instead."
    )


@pytest.mark.parametrize(
    "module",
    ["app/routes/measures.py", "app/routes/health.py"],
)
def test_already_scoped_modules_stay_scoped(module: str):
    """Modules migrated by #396/#398 must not regress to the env var.

    These have zero reads today. Pinning them separately gives a clearer failure
    than a count change: a read reappearing here means a fixed slice regressed.
    """
    assert module not in _count_reads(), (
        f"{module} was migrated to the active MCS connection (#396/#398) and must not "
        "read settings.MEASURE_ENGINE_URL again."
    )


# Calls that reach a measure engine but take their target from a DEFAULTED parameter
# rather than naming it. Invisible to the attribute-read counter above, which is how
# seven unscoped sites in validation.py hid behind a guard that reported "clean".
_TARGET_KWARG = {
    "push_resources": "target_url",
    "evaluate_measure": "measure_engine_url",
    "resolve_evaluated_resource": "base_url",
}

# Remaining allowed omissions, per file. push_resources' CDR calls are NOT counted:
# they pass target_url explicitly, which is the whole point.
EXPECTED_UNSCOPED_CALLS: dict[str, int] = {}


def _count_unscoped_calls() -> dict[str, int]:
    """Count calls to target-taking helpers that omit their target keyword."""
    counts: dict[str, int] = {}
    for path in sorted(_APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        hits = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in _TARGET_KWARG:
                continue
            # resolve_evaluated_resource takes base_url positionally as arg 2.
            if name == "resolve_evaluated_resource" and len(node.args) >= 2:
                continue
            if any(kw.arg == _TARGET_KWARG[name] for kw in node.keywords):
                continue
            hits += 1
        if hits:
            counts[path.relative_to(_APP_ROOT.parent).as_posix()] = hits
    return counts


def test_no_measure_engine_call_relies_on_a_defaulted_target():
    """Every call names the server it means.

    A defaulted target is the #397 mechanism itself: the call reads as innocuous and
    silently goes somewhere the caller did not choose.
    """
    actual = _count_unscoped_calls()
    unexpected = {f: n for f, n in actual.items() if EXPECTED_UNSCOPED_CALLS.get(f, 0) != n}
    assert not unexpected, (
        f"Call(s) taking their measure-engine target from a default: {unexpected}. "
        f"Pass the explicit keyword ({', '.join(f'{k}=>{v}' for k, v in _TARGET_KWARG.items())}), "
        "or add the file to EXPECTED_UNSCOPED_CALLS with a reason."
    )
