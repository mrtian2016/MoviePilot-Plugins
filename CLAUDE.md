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
