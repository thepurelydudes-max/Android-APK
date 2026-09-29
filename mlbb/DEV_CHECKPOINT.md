# MLCA development checkpoint

Updated: 2026-09-29
Current target version: 2.1.0

## Continue from here

Do not revert to the old v1.0.5 or 2.0.x recommendation algorithm.

The current implementation is on `main` and changes the MLCA draft recommendation model as follows:

1. For the selected role, evaluate every role-valid candidate against every selected enemy using the literal raw matchup edge in percentage points.
2. Stage 1: select the TOP 10 candidates by the net sum of those raw matchup pp values. Positive, neutral and negative cells all participate in this sum.
3. Stage 2: rank only those matchup-qualified TOP 10 with the established composite:
   - matchup: 60%
   - strength-aware coverage: 20%
   - tier: 15%
   - role win rate: 5%
4. Coverage uses the positive matchup mass:
   - effective target count = `(sum(p_i)^2) / sum(p_i^2)`, where `p_i=max(edge_i,0)`
   - breadth score = effective target count / selected enemy count
   - strength score = mean positive matchup pp / the current real P95 matchup scale
   - final coverage score = breadth * strength
   This prevents five tiny positive matchups from automatically beating two genuinely strong counters.
5. The visible `Контрит` list is not every positive cell. It is based on relative contribution within the selected draft:
   - keep above-equal-share contributors;
   - if needed, add strongest remaining contributors until about 80% of same-sign matchup mass is explained;
   - preserve a single overwhelming target when it is at least 2x the second contribution;
   - keep near-ties at the boundary together.
   The negative `Опасен` list uses the analogous rule.
6. The UI still shows the literal draft matchup sum in pp, while ranking inside the qualified TOP 10 follows the composite score.

## Files changed for 2.1.0

- `mlbb/draft_matrix_engine.py`
- `mlbb/engine.py`
- `mlbb/test_regressions.py`
- `mlbb/VERSION.txt`

Relevant commits:
- `42a89bb` — strength-aware raw draft coverage
- `0fbc57d` — two-stage TOP-10 + composite ranking
- `db24d50` — regression tests
- `dd0840e` — version 2.1.0
- `ad19573` — build trigger

## Current build

GitHub Actions run ID: `36620173798`
Run number: `#72`
Run title: `Build MLCA 2.1.0`

At the last verified point:
- source verification: success
- canonical MLCA seed restore: success
- `Test MLBB recommendation engine`: success
- `Refresh bundled MLBB database and media`: in progress

Next action in a new chat: check run `36620173798`. If it failed, inspect the failed job/step and continue fixing from current `main`. If it succeeded, download/verify the MLCA 2.1.0 APK artifact. Do not restart the algorithm design from scratch.
