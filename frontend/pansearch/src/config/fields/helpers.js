export const enabled = (key) => (config) => Boolean(config[key]);

export function createCloudDriveItems(options) {
  return options.cloudDrives?.length ? options.cloudDrives : [{title: "115网盘", value: "115"}];
}

export function createResourceTypeItems(cloudDriveItems, config) {
  const resourceTypes = [
    {title: "115分享", value: "115"},
    {title: "123分享", value: "123"},
    {title: "夸克分享", value: "quark"},
    {title: "光鸭分享", value: "guangya"},
    {title: "天翼云盘", value: "tianyi"},
    {title: "阿里云盘", value: "alipan"},
    {title: "ED2K", value: "ed2k"},
    {title: "Magnet", value: "magnet"},
  ]
  const activeDrive = cloudDriveItems.find((item) => item.value === (config.cloud_drive || "115"));
  const supportedTypes = new Set(activeDrive?.resource_types || ["115", "ed2k", "magnet"]);
  const targetCanUpload = activeDrive?.capabilities?.includes("local_upload");
  if (config.cross_transfer_enabled && targetCanUpload) {
    cloudDriveItems.forEach((drive) => {
      const capabilities = new Set(drive.capabilities || []);
      if (
        drive.value === activeDrive?.value ||
        !capabilities.has("share_transfer") ||
        !capabilities.has("file_download")
      ) {
        return;
      }
      ;(drive.resource_types || []).forEach((value) => {
        if (!["ed2k", "magnet"].includes(value)) supportedTypes.add(value);
      })
    })
  }
  return resourceTypes.filter((item) => supportedTypes.has(item.value));
}
