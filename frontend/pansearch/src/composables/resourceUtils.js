/**
 * 网盘资源页面共享的工具函数和常量。
 * 所有纯函数和静态数据集中在此，供 composables 和组件共用。
 */

// RAW SVG ICONS
export const RAW_ICONS = {
  douban: `<svg class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" width="18" height="18" data-rating-logo="douban" style="flex-shrink: 0;"><path d="M1003.52 872.448c0 72.38997333-58.68202667 131.072-131.072 131.072H151.552c-72.38997333 0-131.072-58.68202667-131.072-131.072V151.552c0-72.38997333 58.68202667-131.072 131.072-131.072h720.896c72.38997333 0 131.072 58.68202667 131.072 131.072v720.896z" fill="#00B51D"></path><path d="M822.59694933 184.32H201.40305067a11.501568 11.501568 0 0 0-11.501568 11.501568v46.01173333c0 6.35153067 5.15003733 11.501568 11.501568 11.501568h621.19389866a11.501568 11.501568 0 0 0 11.501568-11.501568v-46.01173333A11.501568 11.501568 0 0 0 822.59694933 184.32zM822.59694933 770.654208h-158.75549866l47.45352533-150.09928533h58.88955733a11.501568 11.501568 0 0 0 11.501568-11.501568V330.41066667a11.49610667 11.49610667 0 0 0-11.501568-11.501568h-516.36906666a11.501568 11.501568 0 0 0-11.501568 11.501568v278.642688c0 6.35153067 5.144576 11.501568 11.501568 11.501568h364.183552l-47.45898667 150.09928533h-131.23584l-34.799616-110.051328c-2.00430933-6.356992-8.781824-11.501568-15.13335467-11.501568H319.05655467c-6.35153067 0-9.87409067 5.144576-7.85885867 11.501568l34.79415467 110.051328H201.40305067a11.501568 11.501568 0 0 0-11.501568 11.501568v46.01719467c0 6.35153067 5.15003733 11.501568 11.501568 11.501568h621.19389866a11.501568 11.501568 0 0 0 11.501568-11.501568v-46.01719467a11.501568 11.501568 0 0 0-11.501568-11.501568z m-471.007232-233.82152533V402.620416c0-6.35153067 5.144576-11.501568 11.501568-11.501568h297.811968c6.35153067 0 11.501568 5.15549867 11.501568 11.501568v134.21226667a11.501568 11.501568 0 0 1-11.501568 11.501568H363.09128533a11.501568 11.501568 0 0 1-11.501568-11.501568z" fill="#FFFFFF"></path></svg>`,
  imdb: `<svg id="home_img" class="ipc-logo" xmlns="http://www.w3.org/2000/svg" width="36" height="18" viewBox="0 0 64 32" data-rating-logo="imdb" style="flex-shrink: 0;"><g fill="#F5C518"><rect x="0" y="0" width="100%" height="100%" rx="4"></rect></g><g transform="translate(8.000000, 7.000000)" fill="#000000" fill-rule="nonzero"><polygon points="0 18 5 18 5 0 0 0"></polygon><path d="M15.6725178,0 L14.5534833,8.40846934 L13.8582008,3.83502426 C13.65661,2.37009263 13.4632474,1.09175121 13.278113,0 L7,0 L7,18 L11.2416347,18 L11.2580911,6.11380679 L13.0436094,18 L16.0633571,18 L17.7583653,5.8517865 L17.7707076,18 L22,18 L22,0 L15.6725178,0 Z"></path><path d="M24,18 L24,0 L31.8045586,0 C33.5693522,0 35,1.41994415 35,3.17660424 L35,14.8233958 C35,16.5777858 33.5716617,18 31.8045586,18 L24,18 Z M29.8322479,3.2395236 C29.6339219,3.13233348 29.2545158,3.08072342 28.7026524,3.08072342 L28.7026524,14.8914865 C29.4312846,14.8914865 29.8796736,14.7604764 30.0478195,14.4865461 C30.2159654,14.2165858 30.3021941,13.486105 30.3021941,12.2871637 L30.3021941,5.3078959 C30.3021941,4.49404499 30.272014,3.97397442 30.2159654,3.74371416 C30.1599168,3.5134539 30.0348852,3.34671372 29.8322479,3.2395236 Z"></path><path d="M44.4299079,4.50685823 L44.749518,4.50685823 C46.5447098,4.50685823 48,5.91267586 48,7.64486762 L48,14.8619906 C48,16.5950653 46.5451816,18 44.749518,18 L44.4299079,18 C43.3314617,18 42.3602746,17.4736618 41.7718697,16.6682739 L41.4838962,17.7687785 L37,17.7687785 L37,0 L41.7843263,0 L41.7843263,5.78053556 C42.4024982,5.01015739 43.3551514,4.50685823 44.4299079,4.50685823 Z M43.4055679,13.2842155 L43.4055679,9.01907814 C43.4055679,8.31433946 43.3603268,7.85185468 43.2660746,7.63896485 C43.1718224,7.42607505 42.7955881,7.2893916 42.5316822,7.2893916 C42.267776,7.2893916 41.8607934,7.40047379 41.7816216,7.58767002 L41.7816216,9.01907814 L41.7816216,13.4207851 L41.7816216,14.8074788 C41.8721037,15.0130276 42.2602358,15.1274059 42.5316822,15.1274059 C42.8031285,15.1274059 43.1982131,15.0166981 43.281155,14.8074788 C43.3640968,14.5982595 43.4055679,14.0880581 43.4055679,13.2842155 Z"></path></g></svg>`,
  tmdb: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 273.42 35.52" width="38" height="18" data-rating-logo="tmdb" style="flex-shrink: 0;"><defs><linearGradient id="tmdb-grad" y1="17.76" x2="273.42" y2="17.76" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#90cea1"></stop><stop offset="0.56" stop-color="#3cbec9"></stop><stop offset="1" stop-color="#00b3e5"></stop></linearGradient></defs><path fill="url(#tmdb-grad)" d="M191.85,35.37h63.9A17.67,17.67,0,0,0,273.42,17.7h0A17.67,17.67,0,0,0,255.75,0h-63.9A17.67,17.67,0,0,0,174.18,17.7h0A17.67,17.67,0,0,0,191.85,35.37ZM10.1,35.42h7.8V6.92H28V0H0v6.9H10.1Zm28.1,0H46V8.25h.1L55.05,35.4h6L70.3,8.25h.1V35.4h7.8V0H66.45l-8.2,23.1h-.1L50,0H38.2ZM89.14.12h11.7a33.56,33.56,0,0,1,8.08,1,18.52,18.52,0,0,1,6.67,3.08,15.09,15.09,0,0,1,4.53,5.52,18.5,18.5,0,0,1,1.67,8.25,16.91,16.91,0,0,1-1.62,7.58,16.3,16.3,0,0,1-4.38,5.5,19.24,19.24,0,0,1-6.35,3.37,24.53,24.53,0,0,1-7.55,1.15H89.14Zm7.8,28.2h4a21.66,21.66,0,0,0,5-.55A10.58,10.58,0,0,0,110,26a8.73,8.73,0,0,0,2.68-3.35,11.9,11.9,0,0,0,1-5.08,9.87,9.87,0,0,0-1-4.52,9.17,9.17,0,0,0-2.63-3.18A11.61,11.61,0,0,0,106.22,8a17.06,17.06,0,0,0-4.68-.63h-4.6ZM133.09.12h13.2a32.87,32.87,0,0,1,4.63.33,12.66,12.66,0,0,1,4.17,1.3,7.94,7.94,0,0,1,3,2.72,8.34,8.34,0,0,1,1.15,4.65,7.48,7.48,0,0,1-1.67,5,9.13,9.13,0,0,1-4.43,2.82V17a10.28,10.28,0,0,1,3.18,1,8.51,8.51,0,0,1,2.45,1.85,7.79,7.79,0,0,1,1.57,2.62,9.16,9.16,0,0,1,.55,3.2,8.52,8.52,0,0,1-1.2,4.68,9.32,9.32,0,0,1-3.1,3A13.38,13.38,0,0,1,152.32,35a22.5,22.5,0,0,1-4.73.5h-14.5Zm7.8,14.15h5.65a7.65,7.65,0,0,0,1.78-.2,4.78,4.78,0,0,0,1.57-.65,3.43,3.43,0,0,0,1.13-1.2,3.63,3.63,0,0,0,.42-1.8A3.3,3.3,0,0,0,151,8.6a3.42,3.42,0,0,0-1.23-1.13A6.07,6.07,0,0,0,148,6.9a9.9,9.9,0,0,0-1.85-.18h-5.3Zm0,14.65h7a8.27,8.27,0,0,0,1.83-.2,4.67,4.67,0,0,0,1.67-.7,3.93,3.93,0,0,0,1.23-1.3,3.8,3.8,0,0,0,.47-1.95,3.16,3.16,0,0,0-.62-2,4,4,0,0,0-1.58-1.18,8.23,8.23,0,0,0-2-.55,15.12,15.12,0,0,0-2.05-.15h-5.9Z"></path></svg>`,
};

// TMDB 题材映射
export const TMDB_GENRES = {
  28: "动作",
  12: "冒险",
  16: "动画",
  35: "喜剧",
  80: "犯罪",
  99: "纪录",
  18: "剧情",
  10751: "家庭",
  14: "奇幻",
  36: "历史",
  27: "恐怖",
  10402: "音乐",
  9648: "悬疑",
  10749: "爱情",
  878: "科幻",
  10770: "电视电影",
  53: "惊悚",
  10752: "战争",
  37: "西部",
  10759: "动作冒险",
  10762: "儿童",
  10763: "新闻",
  10764: "真人秀",
  10765: "科幻奇幻",
  10766: "肥皂剧",
  10767: "脱口秀",
  10768: "战争政治",
};

// API 响应解包
export function unwrapApiResponse(response) {
  if (response?.success !== undefined) return response;
  if (response?.data?.success !== undefined) return response.data;
  return response || {};
}

export function responseItems(response) {
  const result = unwrapApiResponse(response);
  const candidates = [result, result?.data, result?.data?.data, response, response?.data, response?.data?.data];
  for (const candidate of candidates) {
    if (Array.isArray(candidate?.items)) return candidate.items;
    if (Array.isArray(candidate)) return candidate;
  }
  return [];
}

export function prepareMediaItems(items) {
  return Array.isArray(items) ? items : [];
}

// 评分
export function getRatingValue(item) {
  const raw = item?.vote_average ?? item?.rating ?? 0;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : 0;
}

export function getRatingText(item) {
  return getRatingValue(item).toFixed(1);
}

export function formatVoteCount(count) {
  if (!count && count !== 0) return "";
  const num = Number(count);
  if (!num || num <= 0) return "";
  if (num >= 10000) return `(${(num / 10000).toFixed(1).replace(/\.0$/, "")}w)`;
  if (num >= 1000) return `(${(num / 1000).toFixed(1).replace(/\.0$/, "")}k)`;
  return `(${num})`;
}

export function getMediaRatingInfo(item, platform) {
  if (!item) return null;
  if (platform === "tmdb") {
    const rawScore =
      item.tmdb_rating ??
      (item.media_source === "themoviedb" || (!item.imdb_rating && !item.douban_rating && !item.douban_id)
        ? (item.vote_average ?? item.rating)
        : null);
    const val = Number(rawScore);
    if (!Number.isFinite(val) || val <= 0) return null;
    return {score: `${val.toFixed(1)}/10`, votes: formatVoteCount(item.tmdb_vote_count ?? item.vote_count)};
  }
  if (platform === "imdb") {
    const rawScore = item.imdb_rating ?? (item.media_source === "imdb" ? (item.vote_average ?? item.rating) : null);
    const val = Number(rawScore);
    if (!Number.isFinite(val) || val <= 0) return null;
    return {score: `${val.toFixed(1)}/10`, votes: formatVoteCount(item.imdb_vote_count)};
  }
  if (platform === "douban") {
    const rawScore =
      item.douban_rating ??
      (item.douban_id || item.media_source === "douban" ? (item.vote_average ?? item.rating) : null);
    const val = Number(rawScore);
    if (!Number.isFinite(val) || val <= 0) return null;
    return {score: `${val.toFixed(1)}/10`, votes: formatVoteCount(item.douban_vote_count)};
  }
  return null;
}

// 媒体元数据
export function metadataLabels(value) {
  if (Array.isArray(value)) return value.flatMap((item) => metadataLabels(item));
  if (value && typeof value === "object") {
    return [value.name || value.label || value.value || value.iso_3166_1 || value.iso_639_1]
      .filter(Boolean)
      .map((item) => String(item).trim());
  }
  return value
    ? String(value)
      .split(/[\/|,，;；]+/)
      .map((item) => item.trim())
      .filter(Boolean)
    : [];
}

export function mediaCategory(item) {
  const value = metadataLabels(item?.category || item?.metadata_category || item?.media_category);
  return value[0] || (item?.media_type === "tv" ? "剧集" : "电影");
}

export function mediaGenres(item) {
  const parsed = metadataLabels(item?.genres || item?.genre || item?.types);
  if (parsed && parsed.length) return parsed;
  if (Array.isArray(item?.genre_ids)) return item.genre_ids.map((id) => TMDB_GENRES[id]).filter(Boolean);
  return [];
}

export function mediaLanguages(item) {
  const list = [];
  if (item?.original_language) list.push(item.original_language);
  if (item?.language) list.push(item.language);
  if (Array.isArray(item?.languages)) list.push(...item.languages);
  if (Array.isArray(item?.spoken_languages)) {
    list.push(...item.spoken_languages.map((l) => (typeof l === "object" ? l.iso_639_1 || l.name : l)));
  }
  return [...new Set(list.filter(Boolean).map((s) => String(s).trim()))];
}

export function mediaRegions(item) {
  return metadataLabels(item?.regions || item?.origin_country || item?.countries || item?.production_countries);
}

export function getMediaGenresList(item) {
  if (!item) return [];
  const labels = mediaGenres(item);
  if (labels && labels.length) return labels;
  if (Array.isArray(item.genre_ids)) {
    const list = item.genre_ids.map((id) => TMDB_GENRES[id] || id).filter(Boolean);
    if (list.length) return list;
  }
  if (Array.isArray(item.tags)) return item.tags;
  return [];
}

// 格式化
export function formatBytes(bytes) {
  const num = Number(bytes || 0);
  if (!num || num < 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let val = num;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(2)} ${units[i]}`;
}

export function getResourceSize(resource) {
  const formatted = String(resource?.size_formatted || "").trim();
  if (formatted && formatted !== "0") return formatted;
  return formatBytes(resource?.size ?? resource?.size_bytes);
}

export function getBackdropStyle(media) {
  const rawUrl =
    media?.backdrop_url ||
    media?._currentPoster ||
    media?.poster_url ||
    media?.backdrop_path ||
    media?.poster_path ||
    "";
  const url = String(rawUrl).trim();
  if (!url) return "none";
  return `url("${url.replace(/"/g, "\\\"")}")`;
}

// 媒体 Key
export function mediaItemKey(item) {
  return `${item.media_type || "m"}_${item.media_id || item.tmdb_id || item.douban_id || item.bangumi_id || item.anilist_id || item.title}_${item.year || ""}`;
}

export function resKey(res, idx) {
  return `${res.source || "s"}_${res.url || res.title || idx}_${idx}`;
}

// 资源类型
export function getNormalizedResourceType(item) {
  const raw = String(item?.resource_type || item?.pan_type || "")
    .toLowerCase()
    .trim();
  const aliases = {aliyun: "alipan", ali: "alipan", "115pan": "115", "123pan": "123", magnetlink: "magnet"};
  return aliases[raw] || raw || "other";
}

export function getResourceTypeName(type) {
  const map = {
    115: "115网盘",
    quark: "夸克网盘",
    alipan: "阿里云盘",
    baidu: "百度网盘",
    uc: "UC网盘",
    tianyi: "天翼云盘",
    123: "123网盘",
    xunlei: "迅雷网盘",
    magnet: "磁力链接",
    ed2k: "电驴链接",
    torrent: "BT种子",
    pikpak: "PikPak",
    guangya: "光鸭网盘",
  };
  const key = String(type || "").toLowerCase();
  return map[key] || (type ? String(type).toUpperCase() : "未知类型");
}

export function getResourceTabIcon(type) {
  const map = {
    all: "mdi-apps",
    115: "mdi-cloud",
    quark: "mdi-cloud-outline",
    alipan: "mdi-cloud-sync-outline",
    baidu: "mdi-cloud-circle-outline",
    uc: "mdi-cloud-download-outline",
    tianyi: "mdi-cloud-check-outline",
    123: "mdi-cloud-refresh-outline",
    guangya: "mdi-cloud-outline",
    xunlei: "mdi-flash",
    magnet: "mdi-magnet",
    ed2k: "mdi-link-variant",
    torrent: "mdi-seed",
    pikpak: "mdi-cloud-upload-outline",
  };
  return map[String(type || "").toLowerCase()] || "mdi-folder-outline";
}

export function getResourceTypeIcon(type) {
  return getResourceTabIcon(type);
}

export function getSourceColor(source) {
  const map = {
    hdhive: "amber-darken-1",
    piratebay: "teal",
    uindex: "blue",
    seedhub: "deep-purple",
    pansou: "indigo",
    juying: "orange",
    pinglian: "cyan",
    dian115: "amber-darken-2",
  };
  return map[String(source).toLowerCase()] || "grey";
}

export function getSourceName(source) {
  const map = {
    pansou: "PanSou",
    hdhive: "HDHive",
    dian115: "Dian115",
    juying: "聚影",
    seedhub: "SeedHub",
    pinglian: "盘链",
    piratebay: "海盗湾",
    uindex: "UIndex",
    online_docs: "在线文档",
  };
  return map[String(source).toLowerCase()] || source || "未知";
}

export function getTypeColor(type) {
  const map = {
    115: "primary",
    quark: "amber-darken-3",
    alipan: "blue",
    baidu: "indigo",
    uc: "deep-orange",
    tianyi: "teal",
    123: "purple",
    xunlei: "light-blue-darken-1",
    magnet: "red-darken-1",
    ed2k: "blue-grey-darken-1",
    pikpak: "deep-purple",
  };
  return map[String(type || "").toLowerCase()] || "blue-grey";
}

export function getChannelDefaultIcon(key) {
  const map = {
    piratebay: "mdi-skull-crossbones",
    uindex: "mdi-magnet",
    pansou: "mdi-cloud-search",
    seedhub: "mdi-seed",
    juying: "mdi-filmstrip",
    pinglian: "mdi-link-variant",
    dian115: "mdi-cloud-download",
  };
  return map[key] || "mdi-magnify";
}

// 标签提取
export function getTagColorClass(tag) {
  const t = String(tag || "").toUpperCase();
  if (t.includes("4K") || t.includes("2160") || t.includes("UHD")) return "tag-4k";
  if (t.includes("1080") || t.includes("FHD")) return "tag-1080p";
  if (t.includes("720")) return "tag-720p";
  if (t.includes("ISO") || t.includes("原盘") || t.includes("BLURAY") || t.includes("REMUX")) return "tag-remux";
  if (t.includes("杜比视界") || t.includes("DV") || t.includes("VISION")) return "tag-dv";
  if (t.includes("HDR")) return "tag-hdr";
  if (t.includes("WEB")) return "tag-web";
  if (t.includes("中字") || t.includes("简繁") || t.includes("内嵌") || t.includes("双语")) return "tag-sub";
  if (t.includes("国语") || t.includes("国配") || t.includes("粤语")) return "tag-dub";
  if (t.includes("全景声") || t.includes("ATMOS")) return "tag-atmos";
  if (t.includes("60帧") || t.includes("120帧") || t.includes("FPS")) return "tag-fps";
  return "tag-generic";
}

export function getExtractedTags(res) {
  const specs = [];
  const rawTitle = String(res.title || "");
  const title = rawTitle.toUpperCase();
  if (/(?:\b|\[|\.)(?:4K|2160P|UHD)(?:\b|\]|\.)/i.test(title)) specs.push("4K");
  else if (/(?:\b|\[|\.)(?:1080P|1080I|FHD)(?:\b|\]|\.)/i.test(title)) specs.push("1080P");
  else if (/(?:\b|\[|\.)(?:720P)(?:\b|\]|\.)/i.test(title)) specs.push("720P");
  if (/\.ISO\b/i.test(title) || /\[\d+(?:\.\d+)?GB\]\.ISO/i.test(title)) specs.push("原盘ISO");
  else if (/REMUX/i.test(title)) specs.push("REMUX");
  else if (/(?:\b|\[|\.)(?:BDMV|BLURAY|BLU-RAY)(?:\b|\]|\.)/i.test(title)) specs.push("BluRay");
  else if (/WEB-DL|WEBDL|WEB-RIP/i.test(title)) specs.push("WEB-DL");
  if (/DV|DOLBY\s*VISION|杜比视界/i.test(title)) specs.push("杜比视界");
  if (/HDR10\+/i.test(title)) specs.push("HDR10+");
  else if (/(?:\b|\[|\.)HDR10?(?:\b|\]|\.)/i.test(title)) specs.push("HDR");
  if (/60FPS|60帧/i.test(title)) specs.push("60帧");
  else if (/120FPS|120帧/i.test(title)) specs.push("120帧");
  const languageTag = title.match(/内封(?:简繁英|简繁中英|简繁|简中|繁中|中文|英语|英文)/i);
  if (languageTag) specs.push(languageTag[0]);
  else if (/中字|内嵌|简繁|双语|中英|\bCHS\b|\bCHT\b/i.test(title)) specs.push("中字");
  if (/国语|国配|国粤/i.test(title)) specs.push("国语");
  if (/粤语/i.test(title)) specs.push("粤语");
  if (/ATMOS|全景声/i.test(title)) specs.push("杜比全景声");
  return specs;
}

// 库存
export function getLibraryEpisodesCount(media) {
  if (!media) return 0;
  if (typeof media.library_episodes_total === "number") return media.library_episodes_total;
  return (media.seasons || []).reduce((acc, s) => acc + (s.library_episodes_count || 0), 0);
}

export function hasMissingEpisodes(item) {
  if (item?.has_missing_episodes !== undefined) {
    return Boolean(item.has_missing_episodes);
  }
  return (
    item?.media_type === "tv" &&
    (item?.seasons || []).some((season) => (season?.episodes || []).some((episode) => !episode?.in_library))
  );
}

export function getLibrarySummary(media) {
  const names = (media?.library_items || [])
    .map((item) => item?.library || item?.title || item?.path || "")
    .map((value) => String(value).trim())
    .filter(Boolean);
  return [...new Set(names)].slice(0, 3).join("、");
}

// 剪贴板
export async function copyToClipboard(text, showMessage) {
  if (!text) return;
  try {
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        copied = true;
      } catch (_) {
      }
    }
    if (!copied) {
      const input = document.createElement("textarea");
      input.value = text;
      input.setAttribute("readonly", "");
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.focus();
      input.select();
      document.execCommand("copy");
      document.body.removeChild(input);
    }
    showMessage?.("已成功复制链接到剪贴板！");
  } catch (e) {
    showMessage?.("复制失败，请手动复制", "warning");
  }
}

// 海报错误处理
export function handlePosterError(item) {
  if (!item || item._fallbackTried) {
    if (item) item._imgFailed = true;
    return;
  }
  item._fallbackTried = true;
  item._currentPoster = item.poster_url || "";
}

// 预览文件工具
export function previewFileIcon(file) {
  if (file?.is_dir) return "mdi-folder";
  const name = String(file?.name || "").toLowerCase();
  if (/\.(mp4|mkv|ts|mov|avi|flv|wmv|rmvb|m4v|iso)$/.test(name)) return "mdi-movie-open-outline";
  if (/\.(mp3|flac|wav|aac|ape|m4a|ogg)$/.test(name)) return "mdi-music";
  if (/\.(srt|ass|ssa|sup|vtt)$/.test(name)) return "mdi-subtitles-outline";
  if (/\.(zip|rar|7z|tar|gz)$/.test(name)) return "mdi-archive-outline";
  return "mdi-file-outline";
}

export function previewFileExtension(value) {
  const name = String(value || "未命名文件");
  const extensionMatch = name.match(/(\.[^./\\\s]{1,12})$/);
  return extensionMatch ? extensionMatch[1] : "";
}

export function previewFileStem(value) {
  const name = String(value || "未命名文件");
  const extension = previewFileExtension(name);
  const slash = Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\"));
  const basename = slash >= 0 ? name.slice(slash + 1) : name;
  return extension ? basename.slice(0, -extension.length) : basename;
}

// 季集工具
export function extractSeasonFromTitle(text) {
  if (!text) return null;
  const match = String(text).match(/(?:S|Season|第)\s*(\d{1,2})(?:季|\b)/i);
  if (match && match[1]) {
    const s = parseInt(match[1], 10);
    if (s > 0 && s <= 999) return s;
  }
  return null;
}

// 筛选翻译表
export const mediaFilterTranslations = {
  category: {
    movie: "电影",
    movies: "电影",
    film: "电影",
    tv: "剧集",
    television: "剧集",
    series: "剧集",
    anime: "动漫",
    animation: "动漫",
    documentary: "纪录片",
    variety: "综艺",
  },
  type: {
    action: "动作",
    adventure: "冒险",
    animation: "动画",
    comedy: "喜剧",
    crime: "犯罪",
    documentary: "纪录片",
    drama: "剧情",
    family: "家庭",
    fantasy: "奇幻",
    history: "历史",
    horror: "恐怖",
    music: "音乐",
    mystery: "悬疑",
    romance: "爱情",
    "science fiction": "科幻",
    "sci-fi": "科幻",
    "tv movie": "电视电影",
    thriller: "惊悚",
    war: "战争",
    western: "西部",
    reality: "真人秀",
    news: "新闻",
    kids: "儿童",
    talk: "脱口秀",
  },
  region: {
    china: "中国大陆",
    "mainland china": "中国大陆",
    "hong kong": "中国香港",
    taiwan: "中国台湾",
    japan: "日本",
    "south korea": "韩国",
    korea: "韩国",
    "united states": "美国",
    usa: "美国",
    "united kingdom": "英国",
    uk: "英国",
    france: "法国",
    germany: "德国",
    india: "印度",
    thailand: "泰国",
    canada: "加拿大",
    australia: "澳大利亚",
  },
};

export function mediaFilterTitle(value, group) {
  const text = String(value || "").trim();
  if (!text || text === "全部" || /[\u3400-\u9fff]/.test(text)) return text;
  const translated = mediaFilterTranslations[group]?.[text.toLowerCase()];
  if (translated) return translated;
  if (group === "region" && /^[A-Za-z]{2}$/.test(text)) {
    try {
      return new Intl.DisplayNames(["zh-CN"], {type: "region"}).of(text.toUpperCase()) || text;
    } catch {
      return text;
    }
  }
  if (group === "language") {
    const langMap = {
      zh: "华语",
      cn: "国语",
      zho: "汉语",
      en: "英语",
      ja: "日语",
      ko: "韩语",
      fr: "法语",
      de: "德语",
      es: "西班牙语",
      it: "意大利语",
      ru: "俄语",
      hi: "印地语",
      th: "泰语",
      cantonese: "粤语",
      mandarin: "普通话",
    };
    const low = text.toLowerCase();
    if (langMap[low]) return langMap[low];
    try {
      return new Intl.DisplayNames(["zh-CN"], {type: "language"}).of(low) || text;
    } catch {
      return text;
    }
  }
  return text;
}

// 默认渠道
export const DEFAULT_CHANNELS = [
  {key: "pansou", name: "PanSou", icon: "mdi-cloud-search"},
  {key: "hdhive", name: "HDHive", icon: "mdi-hexagon-multiple-outline"},
  {key: "dian115", name: "Dian115", icon: "mdi-cloud-download"},
  {key: "juying", name: "聚影", icon: "mdi-filmstrip"},
  {key: "seedhub", name: "SeedHub", icon: "mdi-seed"},
  {key: "pinglian", name: "盘链", icon: "mdi-link-variant"},
  {key: "piratebay", name: "海盗湾", icon: "mdi-skull-crossbones"},
  {key: "uindex", name: "UIndex", icon: "mdi-magnet"},
];

// 推荐 Tab
export const RECOMMEND_TABS = [
  {value: "tmdb_trending", title: "流行趋势", icon: "mdi-fire"},
  {value: "douban_movie_showing", title: "正在热映", icon: "mdi-movie-open"},
  {value: "douban_movie_hot", title: "热门电影", icon: "mdi-filmstrip"},
  {value: "douban_tv_hot", title: "热门剧集", icon: "mdi-television-classic"},
  {value: "bangumi_calendar", title: "每日放送", icon: "mdi-calendar-star"},
  {value: "anilist_popular", title: "本季新番", icon: "mdi-star-face"},
];

// 跨盘转存判断
export function isCrossTransferResource(res, mainDrive) {
  const t = String(res?.resource_type || "").toLowerCase();
  if (!t || ["magnet", "ed2k", "cloud"].includes(t)) return false;
  return t !== String(mainDrive || "115").toLowerCase();
}

// 预览资源判断
export function canPreviewResource(item) {
  const source = String(item?.source || "").toLowerCase();
  const resType = getNormalizedResourceType(item);
  if (resType === "ed2k") return false;
  const previewableType = Boolean(
    item?.can_preview ||
    ["115", "quark", "alipan", "uc", "tianyi", "baidu", "123", "guangya", "magnet", "torrent"].includes(resType),
  );
  if (!previewableType) return false;
  const pendingHdhive = source === "hdhive" && Boolean(item?.resource_ref) && item?.supports_file_preview !== false;
  const pendingProvider = source === "juying" && Boolean(item?.provider_data?.resource_id);
  const pendingResolution = Boolean(item?.pending_resolution && ["seedhub", "pinglian"].includes(source));
  return Boolean(item?.url || pendingHdhive || pendingProvider || pendingResolution);
}

export function previewResourceKey(item) {
  const source = String(item?.source || "").toLowerCase();
  const providerData = item?.provider_data || {};
  const resourceId = String(providerData.resource_id || "");
  if (source === "juying" && resourceId) return `${source}:${resourceId}`;
  return String(
    item?.url ||
    `${source}:${item?.resource_type || ""}:${
      item?.resource_ref || providerData.seed_id || providerData.path || providerData.resource_id || item?.id || ""
    }`,
  );
}

export function isPointUnlockResource(res) {
  const source = String(res?.source || "").toLowerCase();
  return ["hdhive", "dian115"].includes(source);
}

export function getResourcePointStatus(res) {
  if (res?.need_unlock && Number(res.unlock_points || 0) > 0) {
    return {label: `${Number(res.unlock_points || 0)} 积分`, color: "warning"};
  }
  if (isPointUnlockResource(res) && Number(res?.unlock_points || 0) === 0) {
    return {label: "免费", color: "success"};
  }
  if (res?.need_access) {
    return {label: "需获取", color: "info"};
  }
  return null;
}
