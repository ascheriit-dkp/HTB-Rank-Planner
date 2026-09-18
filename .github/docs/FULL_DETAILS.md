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

First-blood times are used as a lower-bound heuristic for how quickly content was solved after release.

They are not a prediction of how long a specific user will take.

When first-blood data is available, the planner calls the result a **minimum estimated time** and uses a fixed multiplier of **2.5x**:

```text
Challenge           min estimate = challenge FB × 2.5
Machine user        min estimate = user FB × 2.5
Machine root-only   min estimate = root FB × 2.5
Full Machine        min estimate = (user FB + root FB) × 2.5
```

For a full Machine, user and root first-blood times are added before applying the multiplier; the planner no longer uses only the longest of the two.

If only part of the Machine first-blood data is available, the normal difficulty-based fallback remains a floor so incomplete data cannot make a full Machine look artificially cheap. If no usable first-blood data is available, the planner uses the difficulty-based fallback directly.

Machine profiles are parsed for user and root first-blood values. Challenge detail responses are walked recursively because the field shape has changed across HTB responses and community observations.

Sub-minute values are displayed in seconds where possible. Minimum estimated planner time is clamped to at least one minute to avoid unrealistic optimization around tiny first-blood values.

## Difficulty normalization

Difficulty values are normalized to a 0–10 scale. Values returned on a 0–100 style scale are divided by ten, so a value such as `54` becomes `5.4`.

Explicit difficulty fields are preferred. Generic `stars` / content-rating fields are not treated as difficulty.

If no usable difficulty can be extracted, the planner uses a neutral fallback.

## Caching

Live Machine and Challenge list endpoints are intentionally fetched fresh because they carry current ownership and solve state.

Long-lived caching is used only for item-detail data such as Machine profiles and Challenge info.

Cache data is namespaced using a SHA-256 hash of the HTB token:

```text
~/.cache/htb_rank_planner/<token-hash>/
```

The raw token is not stored in the directory name.

Where supported, cache directories are restricted to mode `0700` and cache files, including temporary writes, to `0600`.

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

Every actual HTTP retry passes through the limiter again. A network failure or HTTP 5xx therefore cannot silently bypass the configured request budget.

The rate-limit values are configurable from the CLI rather than being treated as guaranteed HTB limits.

Cold starts can still take time because Challenge detail data requires many requests. Cached runs should be considerably faster.

If an optional Machine/Challenge detail request still fails after retries, the planner falls back to its difficulty-based timing estimate instead of throwing away the whole plan. Authentication failure remains fatal.

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

Authentication/API failures are shown as concise CLI errors by default. `--debug` keeps detailed diagnostics for troubleshooting.

Recommended token-file permissions:

```bash
chmod 600 ~/.htb-token
```

Do not commit tokens, `.env` files containing tokens, or cache data.

## Install

```bash
git clone --depth 1 https://github.com/ascheriit-dkp/HTB-Rank-Planner.git
cd HTB-Rank-Planner
python3 -m pip install -r requirements.txt
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
--version                 print the planner version
--token TOKEN             HTB token; less safe than token-file/env usage
--token-file PATH         read token from a file or stdin with '-'
--debug                   request/rate-limit diagnostics; token is not printed
--top N                   maximum actions printed per plan
--workers N               worker-thread count
--no-cache                disable all disk caching
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

## Example output

The output below is representative of a real v1.1.0 run, with the account state adjusted so the active Ownership matches the displayed rank. Exact recommendations can change as HTB's active content, ratings, and first-blood data change.

**Timing note:** v1.1.1 changes the timing model to the 2.5x minimum-estimate rules documented above. The Ownership/rank parts of this example remain representative, but the v1.1.0 `Est time` values below are intentionally preserved as historical output until the example is refreshed from a live v1.1.1 run.

```text
HTB Rank Planner v1.1.0 — user: example (id=1234567)  tz=Europe/Paris
Rank (API user/info): rank_id=4

Active content counts
  Machines:   20
    - user owns: 16 (user-only: 0)
    - root owns: 16
    - unowned:   4
  Challenges: 198  (active via challenge/list)
    - solved (ACTIVE):   40
    - unsolved (ACTIVE): 158

Ownership% (HTB official formula)
  Current Ownership% = (16 + 16/2 + 40/10) / (20 + 20/2 + 198/10) * 100 = 56.2249%
  API rank_ownership = 56.2200%   (diff=0.0049%)
  API profile owns   = user_owns=16 system_owns=16

Rank progress (like HTB profile)
  Current rank: Pro Hacker (threshold >45.0%)
  Next rank:    Elite Hacker (threshold >70.0%)
  Progress:     [###########-------------] 44.9%
  API rank:     Pro Hacker
  API next:     Elite Hacker
  Raw ownership gap to >70.0%: 13.7751%

Per-action ownership gains
type             |  absolute gain |  relative gain
----------------------------------------------------
user on machine  |       +1.0040% |         +1.79%
root on machine  |       +2.0080% |         +3.57%
user + root      |       +3.0120% |         +5.36%
challenge        |       +0.2008% |         +0.36%

Minimum achievable gain needed to reach Elite Hacker (>70.0%): +13.8554%

machine/profile [############################] 4/4
challenge/info  [############################] 158/158

Fastest path (DP minimizes total estimated time)
  Steps: 13   Flags: 17   Est time: 1h29m
  Projected ownership%: 70.0803%   (gain 6.9000 numerator points)
  Recommended actions:
    type         | name                         |  diff |     fb(u/r) |    est |     gain |  gain/min | flags
    -------------+------------------------------+-------+-------------+--------+----------+-----------+------
    machine_full | Management                   |   4.0 |       6m/7m |     7m |  3.0120% |   0.43653 |     2
    machine_full | Paperwork                    |   4.1 |       6m/8m |     8m |  3.0120% |   0.39632 |     2
    machine_full | MakeSense                    |   4.8 |       5m/9m |     9m |  3.0120% |   0.33221 |     2
    machine_full | Scaffold                     |   5.4 |     28m/11m |    11m |  3.0120% |   0.27976 |     2
    challenge    | Space Explorer               |   2.7 |       38s/- |     1m |  0.2008% |   0.20080 |     1
    challenge    | Forklifts R Us               |   2.8 |        1m/- |     1m |  0.2008% |   0.17212 |     1
    challenge    | CubeMadness1                 |   2.0 |        2m/- |     2m |  0.2008% |   0.11366 |     1
    challenge    | Ether Tag                    |   3.0 |        2m/- |     2m |  0.2008% |   0.09413 |     1
    challenge    | Lucky Dice                   |   2.7 |        3m/- |     3m |  0.2008% |   0.06620 |     1
    challenge    | LightningFast                |   3.1 |        6m/- |     6m |  0.2008% |   0.03523 |     1
    challenge    | RFlag                        |   2.6 |        7m/- |     7m |  0.2008% |   0.02960 |     1
    challenge    | No Errors                    |   2.7 |       16m/- |    16m |  0.2008% |   0.01255 |     1
    challenge    | The Needle                   |   2.9 |       16m/- |    16m |  0.2008% |   0.01255 |     1

Easiest path (greedy: lowest user-rated difficulty, then first-blood time)
  Steps: 41   Flags: 43   Est time: 11h02m
  Projected ownership%: 70.0803%   (gain 6.9000 numerator points)
  Recommended actions:
    type         | name                         |  diff |     fb(u/r) |    est |     gain |  gain/min | flags
    -------------+------------------------------+-------+-------------+--------+----------+-----------+------
    challenge    | CubeMadness1                 |   2.0 |        2m/- |     2m |  0.2008% |   0.11366 |     1
    challenge    | RFlag                        |   2.6 |        7m/- |     7m |  0.2008% |   0.02960 |     1
    challenge    | Space Explorer               |   2.7 |       38s/- |     1m |  0.2008% |   0.20080 |     1
    challenge    | Lucky Dice                   |   2.7 |        3m/- |     3m |  0.2008% |   0.06620 |     1
    challenge    | No Errors                    |   2.7 |       16m/- |    16m |  0.2008% |   0.01255 |     1
    challenge    | Forklifts R Us               |   2.8 |        1m/- |     1m |  0.2008% |   0.17212 |     1
    challenge    | The Needle                   |   2.9 |       16m/- |    16m |  0.2008% |   0.01255 |     1
    challenge    | Wander                       |   2.9 |       16m/- |    16m |  0.2008% |   0.01255 |     1
    challenge    | Baby Frame                   |   2.9 |       16m/- |    16m |  0.2008% |   0.01255 |     1
    challenge    | Ether Tag                    |   3.0 |        2m/- |     2m |  0.2008% |   0.09413 |     1
    challenge    | Secure Server                |   3.0 |       21m/- |    21m |  0.2008% |   0.00946 |     1
    challenge    | LightningFast                |   3.1 |        6m/- |     6m |  0.2008% |   0.03523 |     1
    ... (+29 more)

Hybrid path (DP minimizes weighted time + difficulty)
  Steps: 13   Flags: 17
  Projected ownership%: 70.0803%   (gain 6.9000 numerator points)
  Recommended actions:
    type         | name                         |  diff |     fb(u/r) |    est |     gain |  gain/min | flags
    -------------+------------------------------+-------+-------------+--------+----------+-----------+------
    machine_full | Management                   |   4.0 |       6m/7m |     7m |  3.0120% |   0.43653 |     2
    machine_full | Paperwork                    |   4.1 |       6m/8m |     8m |  3.0120% |   0.39632 |     2
    machine_full | MakeSense                    |   4.8 |       5m/9m |     9m |  3.0120% |   0.33221 |     2
    machine_full | Scaffold                     |   5.4 |     28m/11m |    11m |  3.0120% |   0.27976 |     2
    ... (+9 more)

First-blood data coverage
  Machines with FB parsed: 4/4 (missing 0)
  Challenges with FB parsed: 158/158 (missing 0)
  Note: remaining items fall back to a difficulty→minutes estimate.

Notes
  - Cold start time is limited by HTB API rate limits for item-detail endpoints.
  - With caching enabled, later runs fetch detail data only for newly active IDs.
  - First-blood time is a heuristic, not a personal completion-time prediction.
```

## Tests

The test suite uses Python's built-in `unittest`; there is no separate dev dependency file.

```bash
python3 -m unittest discover -s .github/tests -p "test_*.py" -v
```

The test suite covers core behaviors including:

- active Challenge solved-field aliases and duplicate-ID handling;
- rank thresholds and retained-rank progress;
- Ownership math and strict-threshold crossing;
- difficulty normalization;
- first-blood parsing, explicit units, and false-positive rejection;
- Machine active/retired parsing;
- restrictive cache-file permissions;
- user-only Machine restrictions in DP and Easiest planning;
- 2.5x first-blood minimum-estimate formulas, including full-Machine user+root addition;
- root-upgrade timing;
- HTTP status propagation and clean error messages;
- retry/rate-limiter interaction;
- detail-endpoint fallback behavior.

## Known limitations

- HTB Labs API v4 is not a stable public contract. Endpoint paths or response fields can change.
- First-blood time is only a heuristic; the 2.5x multiplier is deliberately a minimum estimate, not a personal solve-time prediction.
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
- broader saved API-response fixtures to detect future parser regressions;
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
