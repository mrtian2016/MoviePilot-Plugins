export function createTianyiGroups(options) {
  return [
    {
      tab: "tianyi",
      title: "天翼账号",
      hideHeading: true,
      fields: [
        {
          key: "tianyi_account_info",
          type: "account",
          accountKey: "drive:tianyi",
          data: options.accounts?.tianyi || {},
          cols: 12,
        },
        {
          key: "tianyi_cookie",
          label: "登录 Cookie",
          type: "password",
          cols: 12,
          hint: "可继续使用 Cookie 登录，也可点击右侧二维码扫码登录。",
          scanProvider: "tianyi",
        },
        {key: "tianyi_access_token", label: "Access Token", type: "password", cols: 6},
        {key: "tianyi_refresh_token", label: "Refresh Token", type: "password", cols: 6},
        {
          key: "tianyi_transfer_path",
          label: "网盘转存路径",
          type: "cloud-directory",
          placeholder: "/",
          driveProvider: "tianyi",
          hint: "天翼跨盘上传和整理任务先保存到此路径，之后按规则整理。",
          cols: 6,
        },
        {
          key: "tianyi_media_path",
          label: "媒体库目录",
          type: "cloud-directory",
          placeholder: "/",
          driveProvider: "tianyi",
          hint: "最终媒体从此目录开始按规则分类；默认 / 表示网盘根目录。",
          cols: 6,
        },
      ],
    },
    {
      tab: "tianyi",
      title: "请求超时",
      icon: "mdi-timer-cog-outline",
      hint: "作用于天翼云盘 HTTP 请求，范围 10-300 秒。",
      fields: [{key: "tianyi_request_timeout", label: "请求超时（秒）", type: "number", min: 10, max: 300, cols: 6}],
    },
  ]
}
