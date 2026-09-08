export function createAliPanGroups(options) {
  return [
    {
      tab: "alipan",
      title: "阿里云盘账号",
      hideHeading: true,
      fields: [
        {
          key: "alipan_account_info",
          type: "account",
          accountKey: "drive:alipan",
          data: options.accounts?.alipan || {},
          cols: 12,
        },
        {
          key: "alipan_access_token",
          label: "Access Token",
          type: "password",
          cols: 6,
          hint: "可直接填写现有 Access Token；扫码登录后会自动写回。",
          scanProvider: "alipan",
        },
        {
          key: "alipan_refresh_token",
          label: "Refresh Token",
          type: "password",
          cols: 6,
          hint: "用于 Access Token 失效后自动刷新；扫码登录后会自动写回。",
        },
        {
          key: "alipan_transfer_path",
          label: "网盘转存路径",
          type: "cloud-directory",
          placeholder: "/",
          driveProvider: "alipan",
          hint: "分享转存和跨盘上传先保存到此路径。",
          cols: 6,
        },
        {
          key: "alipan_media_path",
          label: "媒体库目录",
          type: "cloud-directory",
          placeholder: "/",
          driveProvider: "alipan",
          hint: "最终媒体目录；默认 / 表示网盘根目录。",
          cols: 6,
        },
      ],
    },
    {
      tab: "alipan",
      title: "请求超时",
      icon: "mdi-timer-cog-outline",
      hint: "作用于阿里云盘请求和分片上传，范围 10-300 秒。",
      fields: [
        {
          key: "alipan_request_timeout",
          label: "请求超时（秒）",
          type: "number",
          min: 10,
          max: 300,
          cols: 6,
        },
      ],
    },
  ]
}
