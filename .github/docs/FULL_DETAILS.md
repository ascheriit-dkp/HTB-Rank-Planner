# HTB Rank Planner — Full Details

HTB Rank Planner is a read-only CLI for Hack The Box Labs. It pulls the current active Machine and Challenge state, calculates active-content Ownership, determines the next Hacker Rank target, and produces three possible paths toward it.

This file lives under `.github/docs/` so the repository root stays intentionally short.

## What it reads

The tool uses the HTB Labs API v4 to obtain:

- the authenticated user's current Hacker Rank and next rank;
- current active Machines and the user's active user/root ownership state;
- current active Challenges and the user's solve state;
- user-rated difficulty where available;
- Machine and Challenge detail data used to extract first-blood times.

It does not submit flags or modify the HTB account.

## Ownership calculation

The active-content Ownership formula used by HTB is:

```text
(ActiveSystemOwns + ActiveUserOwns/2 + ActiveChallengeOwns/10)
---------------------------------------------------------------- * 100
(activeMachines + activeMachines/2 + activeChallenges/10)
```

Rank thresholds used by the planner:

```text
Noob           >= 0%
Script Kiddie   > 5%
Hacker          > 20%
Pro Hacker      > 45%
Elite Hacker    > 70%
Guru            > 90%
Omniscient      = 100%
```

The strict `>` matters. Reaching exactly 45%, 70%, or 90% is not enough for the corresponding promotion. Planner actions move the formula numerator in 0.1-point increments, so the tool calculates the first actually reachable value above the target rather than stopping at the mathematical threshold.

## Protected ranks and retired content

HTB keeps an earned Hacker Rank when active content retires, but the retired content stops contributing to current active Ownership.

Example:

```text
API rank:       Pro Hacker
Current active Ownership: 8%
Next rank:      Elite Hacker
```

The account remains Pro Hacker, but Elite Hacker still requires current active Ownership to exceed 70%. The previously earned 45% is not stored as reusable Ownership credit.

As a result, progress toward the next rank remains 0% while current Ownership is below the floor of the retained rank. Once Ownership goes back above 45%, progress toward Elite Hacker starts moving again.

The planner therefore uses:

- API-reported current rank and next rank for rank identity;
- current active Ownership for the actual amount of work still required.

The CLI prints both a raw mathematical gap and the minimum achievable gain required after respecting strict thresholds and action granularity.

## Planner modes

### Fastest

Uses dynamic programming to minimize estimated total completion time.

The time estimate prefers parsed first-blood data. If first-blood data is unavailable, the tool falls back to a difficulty-based estimate.

### Easiest

Greedy by lowest user-rated difficulty first. First-blood time is used only as a tie-breaker between equally rated items.

Time does not otherwise influence the Easiest ordering.

### Hybrid

Uses dynamic programming with a weighted cost combining estimated time and difficulty.

The current weighting is implemented in code and can be adjusted if a different balance is preferred.

## Machine user/root handling

A fresh Machine normally contributes:

```text
user flag        0.5 numerator points
root flag        1.0 numerator points
user + root      1.5 numerator points
```

Getting root assumes the user flag is also obtained, so the normal recommendation is the complete user + root path.

A user-only Machine recommendation is deliberately restricted:

- at most one user-only Machine can appear in a plan;
- it is allowed only when the 0.5-point granularity is necessary to cross the target efficiently;
- it is displayed as the final step;
- the optimizer cannot farm multiple cheap user-only solves instead of completing Machines.

## First-blood data

First-blood times are used as a heuristic for how quickly content was solved after release.

They are not a prediction of how long a specific user will take.

Machine profiles are parsed for user and root first-blood values. Challenge detail responses are walked recursively because the field shape has changed across HTB responses and community observations.

Sub-minute values are displayed in seconds where possible. Estimated planner time is clamped to at least one minute to avoid unrealistic optimization around tiny first-blood values.

## Difficulty normalization

Difficulty values are normalized to a 0–10 scale. Values returned on a 0–100 style scale are divided by ten, so a value such as `54` becomes `5.4`.

If no usable difficulty can be extracted, the planner uses a neutral fallback.

## Caching

The tool uses two cache classes:

- short-lived/list-style data where freshness matters;
- long-lived item-detail data for Machine profiles and Challenge info.

Cache data is namespaced using a SHA-256 hash of the HTB token:

```text
~/.cache/htb_rank_planner/<token-hash>/
```

The raw token is not stored in the directory name.

Where supported, cache directories are restricted to mode `0700` and cache files to `0600`.

Item detail data is fetched incrementally. After the first run, later runs normally request detail data only for newly active content. Cached entries for content that is no longer active are pruned.

Use `--no-cache` to disable disk caching.

## Rate limiting and retries

The HTB Labs API does not provide this project with a stable public numeric contract for every endpoint, so the client is conservative.

It includes:

- a global rolling-window limiter;
- separate endpoint buckets;
- safety margins below configured limits;
- shared cooldown after HTTP 429 responses;
- `Retry-After` handling where provided;
- retries for transient network failures and HTTP 5xx responses;
- reduced pressure on detail-heavy endpoints.

The rate-limit values are configurable from the CLI rather than being treated as guaranteed HTB limits.

Cold starts can still take time because Challenge detail data requires many requests. Cached runs should be considerably faster.

## Token handling

Preferred options, in order:

```bash
python3 htb_rank_planner.py --token-file ~/.htb-token
```

or:

```bash
export HTB_TOKEN='YOUR_HTB_TOKEN'
python3 htb_rank_planner.py
```

`--token` exists for convenience but is not recommended because command-line arguments may be visible in shell history or process listings.

The tool does not print the token in normal output or debug output.

Recommended token-file permissions:

```bash
chmod 600 ~/.htb-token
```

Do not commit tokens, `.env` files containing tokens, or cache data.

## Install

```bash
git clone --depth 1 https://github.com/ascheriit-dkp/HTB-Rank-Planner.git
cd HTB-Rank-Planner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Then run:

```bash
python3 htb_rank_planner.py --token-file ~/.htb-token --show-progress
```

## CLI

Run:

```bash
python3 htb_rank_planner.py --help
```

Important options include:

```text
--token TOKEN             HTB token; less safe than token-file/env usage
--token-file PATH         read token from a file or stdin with '-'
--debug                   request/rate-limit diagnostics; token is not printed
--top N                   maximum actions printed per plan
--workers N               worker-thread count
--no-cache                disable all disk caching
--list-cache-ttl SEC      list cache TTL
--show-progress           progress bars for uncached detail calls
--fb-challenge-cap N      optional cap on Challenge detail calls
--rl-global N             configured global requests/window
--rl-challenge-info N     configured Challenge detail requests/window
--rl-machine-profile N    configured Machine profile requests/window
--rl-lists N              configured list-endpoint requests/window
--rl-margin N             safety margin below configured rate limits
--rl-window SEC           rolling-window duration
```

Exact defaults are shown by `--help` and should be treated as implementation defaults, not official HTB guarantees.

## Output interpretation

A typical run contains:

- active Machine and Challenge counts;
- current active user/root/Challenge ownership;
- calculated Ownership and API-reported Ownership for comparison;
- protected current rank and next rank;
- progress between those two rank thresholds;
- raw percentage gap to the next threshold;
- minimum achievable gain required to actually cross it;
- per-action Ownership gains;
- Fastest, Easiest, and Hybrid plans;
- first-blood coverage information.

Historical profile totals such as all-time `user_owns` and `system_owns` may remain non-zero even when none of those Machines are currently active. They are displayed for context but are not substituted into active Ownership calculations.

## Tests

The test suite uses Python's built-in `unittest`; there is no separate dev dependency file.

```bash
python3 -m unittest discover -s .github/tests -p "test_*.py" -v
```

The test suite covers core behaviors including:

- active Challenge solved-field aliases;
- rank thresholds;
- retained-rank progress;
- Ownership math;
- first-blood time parsing;
- strict-threshold crossing;
- user-only Machine restrictions in DP and Easiest planning.

## Known limitations

- HTB Labs API v4 is not a stable public contract. Endpoint paths or response fields can change.
- First-blood time is only a heuristic.
- Difficulty ratings are community/user-rated data, not objective solve-time estimates.
- A cold cache may require many API calls.
- Planner results optimize the available metrics; they cannot account for a user's specific strengths, spoilers, prior knowledge, team help, or preferred categories unless those preferences are explicitly added later.

## Possible future work

Useful additions that fit the current design:

- OS/category filters;
- category preferences or penalties;
- JSON/CSV output;
- machine-vs-challenge preference controls;
- a small TUI;
- what-if simulation for future active-content changes;
- optional personal solve-time history to replace generic first-blood estimates;
- CI against mocked API fixtures to detect parser regressions;
- a lightweight endpoint-compatibility check mode.

## Disclaimer

This is an unofficial project and is not affiliated with or endorsed by Hack The Box.

## Reference

HTB's current rank thresholds, Ownership formula, active-content rules, and rank-protection behavior are documented here:

https://help.hackthebox.com/en/articles/5185158-introduction-to-htb-labs

Community documentation used while working with the Labs v4 API:

- Kris Stanley (Propolisa): https://github.com/Propolisa/htb-api-docs
- Gubarz OpenAPI specs: https://github.com/Gubarz/unofficial-htb-api

License: MIT.
