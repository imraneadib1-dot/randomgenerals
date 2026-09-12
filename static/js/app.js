// The app. Imports every module and runs their load-time statements in
// the order the old single script ran them, then boots. Nothing here
// but wiring: see state.js, dom.js and the section modules.

import { mountShell } from "./shell.js";
import { mountComposer } from "./composer.js";
import { mountVoice } from "./voice.js";
import { mountAttachments } from "./attachments.js";
import { mountStrength } from "./strength.js";
import { mountBays } from "./bays.js";
import { mountPreview } from "./preview.js";
import { mountProviders } from "./providers.js";
import { mountCredits } from "./credits.js";
import { mountSidebar } from "./sidebar.js";
import { mountMessage } from "./message.js";
import { mountChat } from "./chat.js";
import { mountImage } from "./image.js";
import { mountModal } from "./modal.js";
import { mountMemory } from "./memory.js";
import { mountAuth } from "./auth.js";
import { mountBilling } from "./billing.js";
import { mountAppearance } from "./appearance.js";
import { mountBoot } from "./boot.js";
import { mountVideo } from "./video.js";
import { mountDiagram } from "./diagram.js";
import { mountSettings } from "./settings.js";
import { mountAccount } from "./account.js";
import { mountConnectors } from "./connectors.js";
import { mountInstall } from "./install.js";
import { mountProfile } from "./profile.js";
import { boot } from "./boot.js";

mountShell();
mountComposer();
mountVoice();
mountAttachments();
mountStrength();
mountBays();
mountPreview();
mountProviders();
mountCredits();
mountSidebar();
mountMessage();
mountChat();
mountImage();
mountModal();
mountMemory();
mountAuth();
mountBilling();
mountAppearance();
mountBoot();
mountVideo();
mountDiagram();
mountSettings();
mountAccount();
mountConnectors();
mountInstall();
mountProfile();

// Last, after every module's declarations exist and every listener is
// attached - which is what retires the temporal-dead-zone crashes the
// old file documented twice.
boot();
