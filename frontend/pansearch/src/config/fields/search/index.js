import {createCommonSearchGroups} from "./common.js";
import {createHdhiveGroups} from "./hdhive.js";
import {createDian115Groups} from "./dian115.js";
import {createJuyingGroups} from "./juying.js";
import {createPansouGroups} from "./pansou.js";
import {createSeedhubGroups} from "./seedhub.js";
import {createPirateBayGroups} from "./piratebay.js";
import {createUIndexGroups} from "./uindex.js";
import {createPinglianGroups} from "./pinglian.js";
import {createOnlineDocsGroups} from "./online_docs.js";

export function createSearchSection(resourceTypeItems, options = {}) {
  return {
    value: "search",
    title: "搜索渠道",
    icon: "mdi-magnify",
    subtabs: [
      {value: "common", title: "通用设置", icon: "mdi-tune"},
      {value: "pansou", title: "PanSou", icon: "mdi-magnify-scan"},
      {value: "hdhive", title: "HDHive", icon: "mdi-hexagon-multiple-outline"},
      {value: "dian115", title: "Dian115", icon: "mdi-cloud-search"},
      {value: "juying", title: "聚影", icon: "mdi-movie-search-outline"},
      {value: "pinglian", title: "盘链", icon: "mdi-link-variant"},
      {value: "seedhub", title: "SeedHub", icon: "mdi-seed-outline"},
      {value: "piratebay", title: "海盗湾", icon: "mdi-pirate"},
      {value: "uindex", title: "UIndex", icon: "mdi-magnet"},
      {value: "online_docs", title: "在线文档", icon: "mdi-file-document-outline"},
    ],
    groups: [
      ...createCommonSearchGroups(resourceTypeItems),
      ...createPansouGroups(options.pansou || {}),
      ...createHdhiveGroups(options),
      ...createJuyingGroups(options),
      ...createSeedhubGroups(),
      ...createPirateBayGroups(),
      ...createUIndexGroups(),
      ...createDian115Groups(options),
      ...createPinglianGroups(options),
      ...createOnlineDocsGroups(resourceTypeItems),
    ],
  }
}
