export function createUIndexGroups() {
  return [
    {
      tab: "uindex",
      title: "UIndex 磁力搜索",
      icon: "mdi-magnet",
      fields: [
        {
          key: "uindex_base_url",
          label: "服务地址",
          hint: "默认 https://uindex.org，可填入自定义镜像。",
          cols: 12,
        },
        {
          key: "test_uindex",
          label: "测试搜索",
          type: "test-source",
          source: "uindex",
          cols: 12,
        },
      ],
    },
    {
      tab: "uindex",
      title: "搜索与限速",
      icon: "mdi-shield-search",
      hint: "针对 UIndex 页面检索设置结果条数与并发间隔。",
      fields: [
        {
          key: "uindex_result_limit",
          label: "候选上限",
          type: "number",
          min: 1,
          max: 80,
          cols: 4,
        },
        {
          key: "uindex_request_interval",
          label: "请求间隔",
          type: "number",
          min: 0.2,
          max: 10,
          step: 0.1,
          suffix: "秒",
          cols: 4,
        },
        {
          key: "uindex_timeout",
          label: "请求超时",
          type: "number",
          min: 5,
          max: 60,
          suffix: "秒",
          cols: 4,
        },
      ],
    },
  ];
}
