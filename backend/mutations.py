"""Mutation harness: break one thing, confirm a test notices.

Run from the backend root. Each mutation is (name, file, old, new, tests). The
file is restored even when the mutation fails to apply, and a mutation that does
not apply is reported as SKIP rather than silently counted as a survivor --
a mutation runner that quietly tests nothing is worse than no mutation runner.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run_tests(tests: str) -> tuple[bool, str]:
    """Run pytest and report whether anything failed.

    Decided by exit code, not by scraping the output. Parsing "N passed" out of
    the terminal summary proved unreliable here -- pytest's final count line was
    absent from captured stdout -- which silently turned every mutation into a
    "survivor". Exit code 0 means green, 1 means tests failed, anything else is
    a collection or internal error, which counts as caught because a mutation
    that stops the suite from even running has certainly changed behaviour.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", tests, "-p", "no:randomly", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="replace",
    )
    out = proc.stdout + proc.stderr
    failed = proc.returncode != 0
    counts = [
        line.strip()
        for line in out.splitlines()
        if (" passed" in line or " failed" in line or "error" in line.lower())
        and not line.startswith(" ")
    ]
    return failed, counts[-1] if counts else f"rc={proc.returncode}"


def mutate(name: str, rel: str, old: str, new: str, tests: str) -> None:
    path = ROOT / rel
    original = path.read_text(encoding="utf-8")
    # Normalise to LF for matching so a mutation written with \n applies to a
    # CRLF file, then write back in the file's own convention.
    uses_crlf = "\r\n" in original
    normalised = original.replace("\r\n", "\n")
    if old not in normalised:
        print(f"SKIP    {name}  (pattern not found in {rel})")
        return
    mutated = normalised.replace(old, new, 1)
    backup = Path(tempfile.gettempdir()) / "mut.bak"
    shutil.copy(path, backup)
    try:
        path.write_text(
            mutated.replace("\n", "\r\n") if uses_crlf else mutated,
            encoding="utf-8",
            newline="",
        )
        failed, summary = run_tests(tests)
        if name in EQUIVALENT_MUTATIONS:
            verdict = "EQUIVALENT" if not failed else "CAUGHT  "
        else:
            verdict = "CAUGHT  " if failed else "SURVIVED"
        print(f"{verdict} {name}  [{summary}]")
    finally:
        shutil.copy(backup, path)
        # Prove the restore worked before the next mutation is judged. A batch
        # run that leaves a file mutated makes every later verdict meaningless,
        # and the symptom is a survivor that contradicts a standalone run.
        after = path.read_text(encoding="utf-8")
        if after != original:
            print(f"ABORT   {name}  (restore left {rel} modified)")
            raise SystemExit(1)


#: Mutations that cannot fail, with the reason. Kept as data rather than deleted
#: so a later reader does not re-add them and waste an hour discovering the same
#: thing. A "survivor" is only a finding when some different input distinguishes
#: the two versions.
EQUIVALENT_MUTATIONS = {
    "throttle declared before auth": (
        "FastAPI solves every sub-dependency before running the body, so "
        "swapping the two parameters cannot change which one raises first."
    ),
    "rejected sync still advances the baseline": (
        "sync_mana never commits on the exception path, so an in-memory advance "
        "is discarded with the transaction whether or not it is written."
    ),
}


MUTATIONS = [
    (
        "no rollback on fail-open",
        "app/services/rate_limit_service.py",
        "        session.rollback()\n        return RateLimitDecision(",
        "        return RateLimitDecision(",
        "tests/test_rate_limit.py",
    ),
    (
        "throttle declared before auth",
        "app/api/deps.py",
        "        user: User = Depends(get_current_user),\n"
        "        session: Session = Depends(rate_limit_session),",
        "        session: Session = Depends(rate_limit_session),\n"
        "        user: User = Depends(get_current_user),",
        "tests/test_rate_limit.py",
    ),
    (
        "XFF leftmost -> rightmost",
        "app/api/deps.py",
        'forwarded.split(",")[0].strip()',
        'forwarded.split(",")[-1].strip()',
        "tests/test_rate_limit.py",
    ),
    (
        "malformed XFF no longer falls back to peer",
        "app/api/deps.py",
        'return forwarded.split(",")[0].strip() or (request.client.host or "unknown")',
        'return forwarded.split(",")[0].strip() or ""',
        "tests/test_rate_limit.py",
    ),
    (
        "no leash check on claims",
        "app/services/territory_service.py",
        "        check_world_leash(q, r)\n",
        "",
        "tests/test_world_leash.py",
    ),
    (
        "leash excludes the boundary ring",
        "app/services/territory_service.py",
        "if hex_distance((origin_q, origin_r), (q, r)) > max_radius:",
        "if hex_distance((origin_q, origin_r), (q, r)) >= max_radius:",
        "tests/test_world_leash.py",
    ),
    (
        "leash is read at import, not per call",
        "app/services/territory_service.py",
        "    settings = get_settings()\n    max_radius = settings.world_max_radius_from_origin",
        "    settings = get_settings()\n    max_radius = _LEASH_AT_IMPORT",
        "tests/test_world_leash.py",
    ),
    (
        "viewport ignores the leash",
        "app/services/world_service.py",
        "    rings = clamp_rings_to_leash(\n"
        "        centre=(centre_q, centre_r), rings=rings, hex_size_meters=hex_size_meters\n"
        "    )",
        "    pass",
        "tests/test_world_leash.py",
    ),
    (
        "viewport clamp off by one",
        "app/services/geo.py",
        "    return max_radius - hex_distance(origin, centre)",
        "    return max_radius - hex_distance(origin, centre) + 1",
        "tests/test_world_leash.py",
    ),
    (
        "baseline advances even with zero steps",
        "app/services/mana_service.py",
        "    if steps > 0:\n        advance_step_baseline(locked, now)",
        "    advance_step_baseline(locked, now)",
        "tests/test_step_plausibility.py",
    ),
    (
        "rejected sync still advances the baseline",
        "app/services/mana_service.py",
        "    check_step_plausibility(locked, steps, now=now)\n    outcome = settle_mana(",
        "    try:\n"
        "        check_step_plausibility(locked, steps, now=now)\n"
        "    except ImplausibleStepCount:\n"
        "        advance_step_baseline(locked, now)\n"
        "        raise\n"
        "    outcome = settle_mana(",
        "tests/test_step_plausibility.py",
    ),
    (
        "no first-sync grace",
        "app/services/mana_service.py",
        "        elapsed = max(elapsed, _FIRST_SYNC_GRACE_SECONDS)",
        "        pass",
        "tests/test_step_plausibility.py",
    ),
    (
        "plausibility boundary exclusive",
        "app/services/mana_service.py",
        "    if steps > max_plausible:",
        "    if steps >= max_plausible:",
        "tests/test_step_plausibility.py",
    ),
    (
        "no minimum elapsed floor",
        "app/services/mana_service.py",
        "    elapsed = max(elapsed, _MIN_ELAPSED_SECONDS)",
        "    pass",
        "tests/test_step_plausibility.py",
    ),
    (
        "speed cap read once at import",
        "app/services/mana_service.py",
        "    return int(elapsed * settings.max_steps_per_second)",
        "    return int(elapsed * 5.0)",
        "tests/test_step_plausibility.py",
    ),
    (
        "plausibility check skipped entirely",
        "app/services/mana_service.py",
        "    check_step_plausibility(locked, steps, now=now)",
        "    pass",
        "tests/test_step_plausibility.py",
    ),
]


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for mutation in MUTATIONS:
        if only and only not in mutation[0]:
            continue
        mutate(*mutation)
