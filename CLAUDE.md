# PanSearch v1.5.0 Development Context

## Project
MoviePilot v2 plugin PanSearch (netdisk search assistant), Chinese-language
plugin, repo root /tmp/mp115-fork. Plugin code: plugins.v2/pansearch/.
Read-only reference sibling: plugins.v2/p115subsearch/ (DO NOT modify it).
Frontend source: frontend/pansearch/ (vite + module federation, build output
goes DIRECTLY into plugins.v2/pansearch/dist/assets per vite.config.js).
Current version 1.4.0 (HEAD 8aee773). Target: 1.5.0 with 4 features.

## Hard rules
1. Do not touch plugins.v2/p115subsearch/ at all (read-only reference).
2. Do not change plugins.v2/pansearch/ directory layout.
3. Do not rewire search channel registration logic (F2 only adds a
   validation step before transfer).
4. Frontend: only add necessary config fields, no UI redesign.
5. py_compile every changed .py file immediately after edit.
6. Commit small and per-feature with clear Chinese messages.
7. Config keys must reuse existing backend key names exactly
   (organize_after_transfer, max_transfer_links, pansou_check_enabled etc).
8. Never break MoviePilot runtime: handlers use OwnerDelegator pattern,
   attributes come from owner plugin instance via delegation.
9. dist/ must stay in git (.gitignore already allows pansearch dist).

## Architecture map (verified 2026-09-09)
- plugins.v2/pansearch/__init__.py (1984 lines): plugin class PanSearch(_PluginBase),
  plugin_version at line 118, config reading in init (~line 1100-1140),
  PanSouClient() init at line 1329, OnlineDocumentClient init at line 1389,
  SyncHandler() init at line 1778 (passes organize_after_transfer).
- plugins.v2/pansearch/core/config.py: default config dict (pansou_ keys around
  line 167-188, transfer keys around line 261-282).
- plugins.v2/pansearch/handlers/sync/movie.py (668 lines): MovieSyncProcessor,
  candidate loop at line ~290 (for resource_index, resource in
  enumerate(candidate_resources)), movie_transferred flag stops after first
  success (movie = stop after success, keep this behavior).
- plugins.v2/pansearch/handlers/sync/television.py (803 lines): TV loop over
  resource_batches (source_index, candidate_resources, is_cross_batch),
  per-resource inner loop at line ~350, transfer results processed at ~657-760,
  transferred_count incremented at line 695, success_episodes list at 285.
- plugins.v2/pansearch/handlers/sync/resources.py: _validated_resource_files at
  line 592 (share validation + listing, used by movie/tv/upgrade).
- plugins.v2/pansearch/handlers/sync/service.py: SyncHandler class, init kwargs
  at line 180+, uses _COMPONENT_TYPES + resolve_component for delegation.
- plugins.v2/pansearch/search/pansou/client.py (433 lines): PanSouClient with
  request_search, health, token auth, gated_request. NO check_links yet.
- plugins.v2/pansearch/search/online_docs/client.py (496 lines):
  OnlineDocumentClient.read(url) -> parse_online_document (real network fetch,
  no cache). service.py OnlineDocumentSearchService.search() calls
  self._client.read(document_url) per document per search.
- plugins.v2/pansearch/handlers/search/service.py: SearchHandler (owns
  _pansou_client, _search_registry).
- Frontend config fields: frontend/pansearch/src/config/fields/transfer.js
  already has organize_after_transfer switch field (line 50) -
  bundled from upstream 1.3.5. New fields go here or in fields/search/pansou.js.

## Reference implementation (p115subsearch v1.7.3, read-only!)
- plugins.v2/p115subsearch/clients/pansou.py line 331 check_links(items):
  POST {base}/api/check/links, payload {"items": [{"disk_type": "115",
  "url":..., "password":...}]}, returns results list with state
  ok|bad|locked|unsupported|uncertain; empty list on ANY failure (degrade).
- plugins.v2/p115subsearch/handlers/sync.py line 85 _build_pansou_check_map:
  batch check -> url-to-state map; None on failure = fallback to original
  validation. _pansou_check_single for uncovered links. Consumer pattern:
  state == "bad" -> skip link; other states -> original flow. locked is NOT
  dead (password-less links return locked).
- plugins.v2/p115subsearch/handlers/sync.py movie branch: per-channel
  _build_pansou_check_map call, max_transfer_links guard both at channel
  loop top and per-link inner loop.
- max_transfer_links semantics: per-subscribe cumulative successful transfer
  link count limit; when reached, stop searching more channels and links.
  Default 5.
- KDocs disk cache pattern: plugins.v2/p115subsearch/clients/kdocs.py
  _cache_path/_load_cache/_save_cache (line 300-355): JSON file
  {saved_at: ts, data: ...} in plugin data dir, TTL 6h
  (cache_ttl_hours * 3600), atomic tmp+replace write, in-memory memo with
  loaded_at, on expired/missing/failure re-fetch.

## Known pitfalls
- PanSou check_links endpoint sometimes 403/429/TLS-reset: client MUST return
  empty list on failure so caller degrades to original validation. Never let
  the check layer break transfers.
- Payload key is items (NOT urls); disk_type values: 115, quark etc.
- Movie branch currently: success on first transferred link stops the loop
  (movie_transferred flag). With max_transfer_links this stays: movies cap
  naturally; TV keeps transferring across channels until missing episodes
  filled or cap reached.
- Frontend build needs node 22 (host has v22.22.1). packageManager pnpm 10.25.0
  but npm install works too. If GitHub registry times out use npmmirror mirror.
- Build output overwrites plugins.v2/pansearch/dist/assets/ (vite
  emptyOutDir). After build, verify remoteEntry.js exists.
- Chinese text in JS/CSS files is normal; ensure UTF-8 no BOM.

## v1.5.1 bugfix batch (context added 2026-09-09, HEAD dafed56 = v1.5.0)
Goal: fix offline task status tracking (false failures + invisible 115
download progress). Four tasks T1-T4, behavior spec in the round prompts.

### Root evidence (verified by requirements owner on production container)
- Table offline_pending_tasks in /config/plugins/PanSearch/pansearch.db:
  all 7 pending rows carry task_id = "subscribe:910" (subscribe-level
  fallback built in _build_pending_record, handlers/sync/service.py
  ~2297-2313), not the real 115 clouddownload info_hash.
- Postprocess matching (handlers/sync/postprocess.py): magnet branch
  ~line 896 and ed2k branch ~944 do task_map.get(task_id.upper()); a
  fallback id never matches -> the item sits until _OFFLINE_TIMEOUT
  (service.py line 161, 30 min) and is then wrongly marked failed.
  Logs prove the file was actually on the 115 drive.
- Blacklist granularity bug: postprocess.py calls
  _add_offline_blacklist(item.get("share_url") or item.get("task_id"),
  reason) at ~900/909/939/951/960/975/984. If share_url is empty the
  fallback can be "subscribe:<id>" which blacklists the WHOLE subscribe
  for 1 day. subscribe-*/media-* keys must never enter the blacklist.
- API flakiness: clouddownload task list intermittently HTTP 502 ->
  drive/p115/offline.py get_offline_tasks (~84-105) falls back to a
  10-min stale cache (refresh_ok=False is tracked and exposed by
  get_offline_task_list_snapshot; postprocess reads offline_tasks_valid
  at ~552 but the timeout-fail branches 907/957/981 ignore it).
  Directory listing intermittently HTTP 405 -> p115 files.py
  _iter_directory (p115client iterdir) fails, list_files_by_cid_checked
  ~747 returns (False, []), so the sha1/dir reverse-lookup fallback is
  also dead.
- Deterministic dead links (errno 4100018, logged in drive/p115/share.py
  ~735 "link expired") get re-discovered and re-fail every round,
  inflating failure counts. history records: 53 success / 43 failed /
  7 processing, and the 7 processing rows carry an EMPTY status string
  -> frontend has nothing to show.

### Key code landmarks (v1.5.0 HEAD)
- drive/p115/offline.py: add_offline_download (~291, returns bool, calls
  add_offline_downloads_batch), batch (~305-448) already parses
  data.result info_hash per url and injects synthetic tasks into the
  cache; _format_offline_task (~30-64) yields id/percent/state;
  _format_offline_status (~140) maps state to Chinese text.
- handlers/sync/service.py: _queue_magnet_package ~2090 calls
  add_offline_download and only uses the bool; _add_offline_blacklist
  ~1902; _OFFLINE_TIMEOUT line 161; _pending_identity ~2201 derives
  info_hash from the url via _offline_hash.
- handlers/sync/postprocess.py: monitor_offline_strm_tasks ~444; share
  branch already locates files by staging name and source_sha1 via
  directory_snapshot (~579) -- reusable for a "file already exists"
  final verdict before declaring timeout failure.
- handlers/sync/history.py: _mark_offline_history_status_batch ~1388
  can already flip records to success and collect platform records.
- tests/test_wk1_logic.py: ast-extraction unit test harness usable for
  new logic tests without importing app.*.

### v1.5.1 progress (updated 2026-09-09, after T1-T3 rounds)
- Committed: 19c9df0 (T1 real info_hash handle + progress snapshot),
  0e933ef (T2 timeout file-exists verdict + blacklist guard),
  df17f8e (T3 502/405 backoff retry + defer on refresh_ok=False),
  fddefbb (T2 regression: ready verdict falls through to finalize same
  round, livelock fix). Unit tests: tests/test_v151_offline_logic.py,
  34 cases, all OK via `python3 -m unittest tests.test_v151_offline_logic`
  run from plugins.v2/pansearch/.
- Remaining: T4 (reconcile failed/processing history every round with
  notification; deterministic 4100018 dead-link single-resource blacklist)
  and version wrap-up (plugin_version 1.5.1 + package.v2.json PanSearch
  entry version/history). Do NOT touch dist/ this round.

### v1.5.1 guardrails
- Do NOT regress v1.5.0 F1-F4 (pansou prefilter, max_transfer_links,
  organize switch, kdocs cache). Do NOT touch plugins.v2/p115subsearch/.
- Frontend only if strictly required to surface progress fields; then
  rebuild dist in the same round (node 22 available).
- Version bump last: package.v2.json + __init__.py plugin_version =
  1.5.1 plus a Chinese v1.5.1 history entry in __init__.py.
- py_compile each edited file immediately; commit per task with Chinese
  messages. Do not commit scratch files.
