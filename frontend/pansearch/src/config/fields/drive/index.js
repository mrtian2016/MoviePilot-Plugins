import {createGuangyaGroups} from "./guangya.js";
import {createP115Groups} from "./p115.js";
import {createP123Groups} from "./p123.js";
import {createQuarkGroups} from "./quark.js";
import {createTianyiGroups} from "./tianyi.js";
import {createAliPanGroups} from "./alipan.js";

export function createDriveSection(options) {
  return {
    value: "drive",
    title: "网盘配置",
    icon: "mdi-cloud-cog-outline",
    subtabs: [
      {value: "115", title: "115网盘", icon: "mdi-cloud-outline"},
      {value: "123", title: "123网盘", icon: "mdi-cloud-outline"},
      {value: "quark", title: "夸克网盘", icon: "mdi-cloud-outline"},
      {value: "guangya", title: "光鸭网盘", icon: "mdi-cloud-outline"},
      {value: "tianyi", title: "天翼云盘", icon: "mdi-cloud-outline"},
      {value: "alipan", title: "阿里云盘", icon: "mdi-cloud-outline"},
    ],
    groups: [
      ...createP115Groups(options),
      ...createP123Groups(options),
      ...createQuarkGroups(options),
      ...createGuangyaGroups(options),
      ...createTianyiGroups(options),
      ...createAliPanGroups(options),
    ],
  }
}
