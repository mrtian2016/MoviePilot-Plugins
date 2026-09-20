export function createPirateBayGroups() {
  return [
    {
      tab: "piratebay",
      title: "海盗湾 (The Pirate Bay)",
      icon: "mdi-pirate",
      fields: [
        {
          key: "piratebay_base_url",
          label: "API 接口地址",
          hint: "默认 https://apibay.org，可填入可用镜像。",
          cols: 12,
        },
        {
          key: "test_piratebay",
          label: "测试搜索",
          type: "test-source",
          source: "piratebay",
          cols: 12,
        },
      ],
    },
    {
      tab: "piratebay",
      title: "搜索与限速",
      icon: "mdi-shield-search",
      hint: "针对海盗湾 API 设置结果条数与并发间隔。",
      fields: [
        {
          key: "piratebay_result_limit",
          label: "候选上限",
          type: "number",
          min: 1,
          max: 80,
          cols: 4,
        },
        {
          key: "piratebay_request_interval",
          label: "请求间隔",
          type: "number",
          min: 0.2,
          max: 10,
          step: 0.1,
          suffix: "秒",
          cols: 4,
        },
        {
          key: "piratebay_timeout",
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
