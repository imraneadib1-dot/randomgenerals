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
import { mountArtifacts } from "./artifacts.js";
import { mountBrowser } from "./browser.js";
import { mountModel3d } from "./model3d.js";
import { mountChrome } from "./chrome.js";
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
import { mountConnectors, mountApiKey } from "./connectors.js";
import { mountInstall } from "./install.js";
import { mountProfile } from "./profile.js";
import { mountSlashMenu } from "./slashmenu.js";
import { mountProjects } from "./projects.js";
import { mountSearch } from "./search.js";
import { mountHome } from "./home.js";
import { boot } from "./boot.js";
import { installErrorReporting } from "./toast.js";

mountShell();
mountComposer();
mountVoice();
mountAttachments();
mountStrength();
mountBays();
mountPreview();
mountArtifacts();
mountBrowser();
mountModel3d();
mountChrome();
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
mountApiKey();
mountInstall();
mountProfile();
mountSlashMenu();
mountProjects();
mountSearch();
mountHome();

// Anything nobody caught becomes a toast, not a console line.
installErrorReporting();

// Last, after every module's declarations exist and every listener is
// attached - which is what retires the temporal-dead-zone crashes the
// old file documented twice.
boot();
