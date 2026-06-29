"""One-shot script to estimate how many jobs pass the hard filter and what it'll cost."""
import json
import random
from pathlib import Path

from agents.scorer import passes_hard_filters
from agents.claude_client import cost_usd

JOBS_CACHE = Path("data/jobs/latest.json")
PROFILE_PATH = Path("data/profile/profile.json")
TOKENS_PER_JOB_IN = 1200
TOKENS_PER_JOB_OUT = 150

jobs = json.loads(JOBS_CACHE.read_text(encoding="utf-8"))
profile = json.loads(PROFILE_PATH.read_text())

passed = [j for j in jobs if passes_hard_filters(j, profile)]

est_cost = cost_usd(
    "claude-sonnet-4-6",
    len(passed) * TOKENS_PER_JOB_IN,
    len(passed) * TOKENS_PER_JOB_OUT,
)

print(f"Total cached:    {len(jobs)}")
print(f"Pass hard filter: {len(passed)}")
print(f"Estimated cost:  USD {est_cost:.3f}")
print()
print("Sample titles (random 30):")
sample = random.sample(passed, min(30, len(passed)))
sample.sort(key=lambda j: j.get("company", ""))
for j in sample:
    print(f"  [{j['company']}] {j['title']}")
