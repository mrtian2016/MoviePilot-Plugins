export function createP123Groups(options) {
  return [
    {
      tab: "123",
      title: "123账号",
      hideHeading: true,
      fields: [
        {
          key: "p123_account_info",
          type: "account",
          accountKey: "drive:123",
          data: options.accounts?.["123"] || {},
          cols: 12,
        },
        {
          key: "p123_token",
          label: "123 Token",
          type: "password",
          hint: "推荐点击右侧二维码按钮，使用 123 云盘 App 扫码登录",
          scanProvider: "123",
          cols: 12,
        },
        {
          key: "p123_transfer_path",
          label: "网盘转存路径",
          type: "cloud-directory",
          placeholder: "/",
          driveProvider: "123",
          hint: "123分享和离线任务先保存到此路径，之后按平台规则整理。",
          cols: 6,
        },
        {
          key: "p123_media_path",
          label: "媒体库目录",
          type: "cloud-directory",
          placeholder: "/",
          driveProvider: "123",
          hint: "最终媒体从此目录开始按平台规则分类；默认 / 表示网盘根目录。",
          cols: 6,
        },
      ],
    },
    {
      tab: "123",
      title: "请求超时",
      icon: "mdi-timer-cog-outline",
      hint: "作用于 123 网盘登录和 HTTP 请求，范围 5-300 秒。",
      fields: [
        {
          key: "p123_request_timeout",
          label: "请求超时（秒）",
          type: "number",
          min: 5,
          max: 300,
          cols: 6,
        },
      ],
    },
  ]
}
