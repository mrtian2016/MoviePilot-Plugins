import {createBasicSection} from "./fields/basic.js";
import {createDriveSection} from "./fields/drive/index.js";
import {createCloudDriveItems, createResourceTypeItems} from "./fields/helpers.js";
import {createNotifySection} from "./fields/notify.js";
import {createSearchSection} from "./fields/search/index.js";
import {createTransferSection} from "./fields/transfer.js";
import {createUpgradeSection} from "./fields/upgrade.js";
import {createSubscribeSection} from "./fields/subscribe.js";
import {createCheckinSection} from "./fields/checkin.js";

export function createConfigSections(options, config = {}) {
  const cloudDriveItems = createCloudDriveItems(options);
  const resourceTypeItems = createResourceTypeItems(cloudDriveItems, config);
  return [
    createBasicSection(cloudDriveItems),
    createSubscribeSection(options),
    createCheckinSection(),
    createTransferSection(options),
    createDriveSection(options),
    createSearchSection(resourceTypeItems, options),
    createUpgradeSection(options),
    createNotifySection(options),
  ]
}
