# P115SubSearch v1.7.3 Development Context

## Project Goal
MoviePilot plugin P115SubSearch version bump 1.7.2 -> 1.7.3 with two features:
1. (CORE) Fix "fake search success" - a channel counts as success ONLY if at
   least one link is REALLY transferred to 115 this round; otherwise continue
   searching next channels in the SAME round
2. Restore cron minimum interval limit, threshold = 4 hours

## Requirements (authoritative, from user)

### Req 1: Channel fake-success fix (CORE)
Root cause (already diagnosed, do NOT re-investigate):
- Movie branch: handlers/search.py search_resources L125-155 stops at the
  first source with ANY results ("if results: return results" L145-148).
  handlers/sync.py process_movie_subscribe (~L211) calls it once, then enters
  the transfer loop; if all returned links fail to transfer
  (transferred_count=0), the round ends and OTHER channels are never searched
  this round. Next cron round starts from KDocs again -> effective deadlock.
- TV branch: handlers/sync.py process_tv_subscribe L636-905 already loops all
  sources; VERIFY no state pollution causes later sources to be skipped
  (e.g. KDocs result counting/cache/note pollution). If TV is already correct,
  leave its loop structure intact - do not regress it.

Implementation requirements:
1. MOVIE: a channel counts as "success" ONLY if at least one link from it is
   REALLY transferred successfully this round. If all results of the current
   channel fail to transfer (invalid link / no match / transfer API failure),
   immediately continue searching the NEXT channel within the same round,
   until one channel really transfers something or all channels are exhausted.
   - Restructure: movie branch must integrate channel-level fallback. Either
     aggregate-then-transfer with per-channel fallback, or loop
     search_single_source per channel with transfer inside the channel loop.
     search_resources must no longer "return on first non-empty result".
   - Reuse the EXISTING transfer pipeline in process_movie_subscribe
     (PanSou check map -> matching -> transfer) - do not invent a parallel
     one. The per-channel loop must preserve ALL existing guards:
     _build_pansou_check_map, SubscribeFilter, max_transfer_links (default 5),
     max_transfer_per_sync (50), HDHive unlock budget, password stitching
     (?password=), history recording, success-notification semantics
     (notify only when the movie really transferred).
2. TV: ALL enabled channels participate in searching EVERY round regardless
   of earlier-channel success - multi-episode chasing needs every channel.
   Do NOT skip later channels just because an earlier channel found something.
   Results per channel are matched against the CURRENT missing-episode set.
   (If current code already does this, verify and leave as-is.)
3. Existing protections NOT regressed: max_transfer_links(5) /
   max_transfer_per_sync(50) / PanSou dead-link pre-filter / HDHive unlock
   budget / rss_sites_backup A1 system (>=16 grep references must remain).
4. Observability: after each channel search+transfer, log
   "[渠道X] 搜索到N个/有效转存M个" so the user can locate fake-link channels.
   Movie branch logs after per-channel transfer attempt; TV branch logs per
   channel in its existing loop.

### Req 2: Restore cron minimum interval = 4 hours
- v1.7.2 (commit 9b935b1) deleted _MIN_INTERVAL_HOURS and
  _cron_interval_ge_min_hours entirely (currently 0 references).
- Restore the mechanism with threshold 4, based on the original implementation:
  git show 9b935b1~1:plugins.v2/p115subsearch/__init__.py
  (L140 constant, L161-188 static method, L571-575 init_plugin validation,
  L1030 get_service check).
- Changes vs original: _MIN_INTERVAL_HOURS = 4 (was 8); fallback cron becomes
  "30 */4 * * *" (was */8).
- Both validation points must exist: init_plugin config read + get_service
  registration. Cron < 4h falls back to "30 */4 * * *"; >= 4h passes as-is.
- UI hint: ui/config.py ~L110 reword to reflect 4-hour minimum.
- _is_last_run_today is NOT involved, leave alone.

## Boundaries (explicitly NOT doing)
- No MP core code changes
- v1.7.2 Scheme A1 untouched: rss_sites_backup backup/restore/self-heal stays
  behavior-equivalent (31 grep references currently, >=16 after)
- PanSou detection layer semantics unchanged (bad skip / locked fallback)
- No changes to existing subscribe rows or _excluded logic
- Do not delete or rewrite the TV branch loop unless fixing a real bug

## Version and Release chain
- Version 1.7.2 -> 1.7.3 in TWO places:
  - plugins.v2/p115subsearch/__init__.py L42 plugin_version = "1.7.3"
  - package.v2.json (repo root) P115SubSearch version = "v1.7.3"
  - package.v2.json history add "v1.7.3" entry with Chinese update notes
- Release chain: git commit -> tag P115SubSearch_v1.7.3 on main HEAD (Chinese
  message) -> GitHub Release with zip asset P115SubSearch_v1.7.3.zip (zip
  root must contain p115subsearch/ dir) -> MP API forced reinstall ->
  verify runtime version via docker exec.
- Git remote already has token configured, push works directly.
- GitHub API: use GITHUB_TOKEN from environment (bash -lc loads it). Release
  create: POST api.github.com/repos/wukangxxx/MoviePilot-Plugins/releases
  with tag_name P115SubSearch_v1.7.3; upload zip via uploads.github.com.

## Critical pitfalls from v1.6.x-1.7.2 history
- MP install API: token MUST go in URL query param (&repo_url=... too);
  Bearer header gets 403. install can take up to 120s - check logs.
- MP market update compares package.v2.json version vs local plugin_version.
- Plugin log: /config/logs/plugins/p115subsearch.log (separate file).
- MP container: moviepilot-v2; runtime code /app/app/plugins/p115subsearch/.
- MP plugin reinstall API: GET
  http://localhost:13000/api/v1/plugin/install/P115SubSearch?token=$MP_API_TOKEN&repo_url=https://github.com/wukangxxx/MoviePilot-Plugins&force=true
- docker exec heredoc needs -i; always read back to verify writes.
- Do not print tokens/cookies in logs or code.

## Acceptance criteria (self-test required, evidence in report)
1. grep assertions: search_resources no longer returns on first non-empty
   result (movie branch has per-channel transfer fallback); _MIN_INTERVAL_HOURS
   = 4 restored with BOTH validation call sites; rss_sites_backup references
   >= 16; both version numbers = 1.7.3.
2. Logic self-test (write CLI assertion script, run, include output):
   (a) channel A all fail -> channel B searched SAME round + B transfers ok;
   (b) TV multi-channel merges missing episodes (later channel not skipped);
   (c) cron 0 */3 * * * rejected->falls back 30 */4 * * *; 0 */4 and 0 */5 pass.
3. Container test after release: MP forced reinstall, docker exec verify
   runtime version 1.7.3.
4. Security: no credentials in code or log output.

## Working style
- py_compile every edited file before declaring done
- Run tests with crawler-tools venv python:
  /vol4/1000/hermes/workspaces/crawler-tools/venv/bin/python
- Self-test scripts in plugins.v2/p115subsearch/tests/ (existing naming),
  run them and include real output in the final report
