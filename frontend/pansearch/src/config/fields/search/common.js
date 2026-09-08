import {enabled} from "../helpers.js";

const SOURCE_ITEMS = [
  {title: "HDHive", value: "hdhive"},
  {title: "Dian115", value: "dian115"},
  {title: "PanSou", value: "pansou"},
  {title: "聚影", value: "juying"},
  {title: "盘链", value: "pinglian"},
  {title: "SeedHub", value: "seedhub"},
  {title: "海盗湾", value: "piratebay"},
  {title: "UIndex", value: "uindex"},
  {title: "在线文档", value: "online_docs"},
]

export function createCommonSearchGroups(resourceTypeItems) {
  return [
    {
      tab: "common",
      title: "搜索顺序",
      icon: "mdi-sort",
      hint: "按勾选顺序设置搜索源优先级；未选择的搜索源不会发起请求。",
      fields: [
        {
          key: "search_source_order",
          label: "搜索资源优先级",
          hint: "已选搜索源按当前顺序查询；留空时不搜索任何渠道。",
          type: "select",
          items: SOURCE_ITEMS,
          multiple: true,
          cols: 12,
        },
        {
          key: "resource_type_order",
          label: "资源类型优先级",
          hint: "开启跨盘转存后可选择其他已接入网盘；仅搜索和处理已选类型，并按当前顺序优先匹配。",
          type: "select",
          items: resourceTypeItems,
          multiple: true,
          cols: 12,
        },
        {
          key: "magnet_metadata_url_template",
          label: "Magnet元数据地址模板",
          hint: "必须包含 {info_hash}；返回内容会由torf解码并校验Info Hash。",
          placeholder: "https://itorrents.org/torrent/{info_hash}.torrent",
          cols: 12,
        },
      ],
    },
    {
      tab: "common",
      title: "搜索代理",
      icon: "mdi-lan-connect",
      hint: "仅代理搜索渠道请求；测试使用 Cloudflare Trace 获取请求延迟、出口 IP、地区和节点。",
      fields: [
        {
          key: "search_proxy",
          label: "代理地址",
          type: "proxy",
          hint: "支持 http://、https://、socks5:// 和 host:port，留空则直连。",
          placeholder: "http://127.0.0.1:7890",
          cols: 12,
        },
        {
          key: "search_proxy_username",
          label: "代理用户名",
          cols: 6,
        },
        {
          key: "search_proxy_password",
          label: "代理密码",
          type: "password",
          cols: 6,
        },
      ],
    },
    {
      tab: "common",
      title: "搜索性能",
      icon: "mdi-speedometer",
      hint: "控制跨搜索源查询和本地缓存；115接口与积分解锁仍保持串行限速。",
      fields: [
        {
          key: "search_cache_enabled",
          label: "启用搜索缓存",
          type: "switch",
          cols: 4,
        },
        {
          key: "search_cache_ttl_minutes",
          label: "缓存时间（分钟）",
          type: "number",
          min: 1,
          max: 1440,
          cols: 4,
          show: enabled("search_cache_enabled"),
        },
        {
          key: "search_concurrency",
          label: "搜索并发数",
          hint: "1为逐源查询，建议2，最大5",
          type: "number",
          min: 1,
          max: 5,
          cols: 4,
        },
      ],
    },
  ]
}
