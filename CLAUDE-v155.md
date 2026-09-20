# PanSearch v1.5.5 round context (2026-09-11)

## Goal
Media library directory classification fix: add movie/tv media directory config,
fix directory computation bug, close the organize-after-transfer switch loop.

## User requirement (re-stated 2026-09-11)
1. The plugin must control whether files are organized after transfer (switch).
2. When organize is ON: transfer into movie dir and tv dir separately, create
   media category folders using MoviePilot's own classification rules.
3. When organize is OFF: only transfer into staging dir and let MoviePilot
   do the organizing.
4. Side issue: newest episodes already landed in /影视库/电视剧/国产剧/ but the
   plugin shows them stuck in 处理中/下载中 forever - after fixing directory
   logic the status must advance naturally to terminal state.

## Current state and root cause (adopt directly)
- Plugin config has single cloud_media_path=/影视库, cloud_transfer_path=
  /未整理/待整理; organize_after_transfer switch exists (default False).
- MoviePilot directory config (user.db systemconfig key=Directories):
  TV    -> library_path=/影视库/电视剧/ (library_category_folder=true)
  Movie -> library_path=/影视库/电影/
  115整理 -> library_path=/影视库/ (library_type_folder=true)
- ROOT CAUSE: handlers/sync/service.py _platform_classified_root() L3085-3141:
    directory = DirectoryHelper().get_dir(media=mediainfo, include_unsorted=False)
    if directory:
        updates = {"library_path": root_path}   # BUG: overrides type dir
        target_directory = directory.model_copy(deep=True, update=updates)
        classified_root = TransHandler().get_dest_dir(
            mediainfo=mediainfo, target_dir=target_directory)
    fallback L3127-3137: when get_dir returns None,
    Path(root_path)/media_type_value
  The override drops the 电视剧/电影 layer -> wrong path /影视库/国产剧/xxx.
- Status stuck: plugin searches files by wrong cloud_dir -> never finds
  them -> postprocess never reaches terminal state. 5 stuck tasks already
  landed successfully in transferhistory (status=1).

## Relevant code locations
- _platform_classified_root: service.py L3085
- _platform_rename_path: L3143 (select root by type, then classified_root)
- _platform_target: L3171
- _effective_mediainfo: L3188
- _resolve_resource_season_dir: L3203 (postprocess path resolution)
- _scan_cloud_resource_episodes / _find_cloud_movie_file: L3376 / L3395
- All call sites pass self._CLOUD_MEDIA_ROOT (= __init__.py _cloud_media_path)
- Plugin config read in __init__.py: L1100 cloud_media_path, L1088
  cloud_transfer_path, L1127 organize_after_transfer; SyncHandler
  instantiation L1811-1818 (cloud_media_root=self._cloud_media_path)
- service.py constructor: cloud_media_root: str = "/" (L199);
  _normalize_cloud_path (L3472)

## F1 (P0) new movie/tv media dir config + fix directory computation
- Add config keys movie_media_path, tv_media_path (cloud-directory selector),
  backward compatible: fall back to cloud_media_path when unset, then "/".
- Fix _platform_classified_root: NEVER override MoviePilot directory
  library_path with cloud_media_path. Select root by mediainfo.type
  (MediaType.MOVIE.value="电影", TV.value="电视剧"): movie_media_path /
  tv_media_path, fallback cloud_media_path; pass that root as root_path into
  _platform_rename_path; classification keeps going through MoviePilot rules
  (TransHandler.get_dest_dir + DirectoryHelper.get_dir(include_unsorted=False)),
  producing stable structure: <media root>/<category>/<title>/Season N/...
- Keep fallback branch but correct it: when get_dir has no result, land on
  "电影"/"电视剧" under the selected root.
- Acceptance: unit test mocks MoviePilot dir hit TV (library_path=
  /影视库/电视剧/) -> final cloud_dir starts with /影视库/电视剧/ and
  contains a category layer; mock movie -> /影视库/电影/; grep asserts
  _platform_classified_root no longer contains "library_path": root_path
  override.

## F2 (P0) frontend config page + dist rebuild
- frontend/pansearch/src/config/fields/drive/p115.js: add movie_media_path
  and tv_media_path cloud-directory fields (labels 电影媒体目录 /
  电视剧媒体目录, hint: when organize on, files land per type under these
  dirs; unset falls back to media library dir). Keep cloud_media_path
  as fallback.
- Rebuild frontend bundle and commit plugins.v2/pansearch/dist/ output
  (the bundle command is available locally).
- Acceptance: grep -rn "movie_media_path|tv_media_path"
  frontend/pansearch/src/ hits; dist assets contain the new field
  names; field JSON structure matches other cloud-directory fields.

## F3 (P1) organize switch closed loop + status convergence
- Confirm organize_after_transfer full chain: ON -> F1 directory organize
  (move + STRM); OFF -> file stays in staging dir (cloud_transfer_path)
  and is done, no media category dir created (add test assertions).
- After directory fix, existing stuck tasks must advance naturally:
  verify postprocess file path resolution uniformly goes through F1
  computed logic (_resolve_resource_season_dir,
  _scan_local_resource_episodes etc.), never searching by old cloud_dir.
- Acceptance: unit test covers organize_after_transfer=False -> no move /
  no category creation; grep asserts postprocess path resolution shares
  F1 path source.

## Hard red lines
- NEVER add abstract members to core/cloud.py; the six-provider contract
  regression test (tests/test_v152_provider_contract.py) must stay green.
- No DB schema change; new config keys via config.get(key, default), no
  migration.
- After each F run the full test suite (currently 186 cases + new ones);
  py_compile every changed file.
- Unit test style: existing tests/ unittest + ast extraction pattern.
- Python 3.11 compatible: f-string same-quote nesting raises SyntaxError;
  take value first, then concatenate.
- Frontend src and dist must be committed together; missing dist means
  incomplete.
- Container has app deps; contract tests can run via
  docker exec -i moviepilot-v2 /opt/venv/bin/python.

## Test commands
- Host full suite (must run from /tmp/mp115-fork root, conftest at root):
  /vol4/1000/hermes/workspaces/crawler-tools/venv/bin/python -m pytest
  plugins.v2/pansearch/tests/ -q
- Single file: same command + tests/test_v155_classified_roots.py
- py_compile: /usr/bin/python3 -m py_compile <changed file>
- HARD: claude rounds must NOT commit or push; developer commits per F
  with message noting v1.5.5 F<n>.
