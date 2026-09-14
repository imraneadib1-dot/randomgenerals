// Every element the app holds a handle to, looked up once.
//
// Module scripts are deferred, so the document is parsed by the time
// this runs - the same guarantee the old end-of-body script had.

export const chatLog = document.getElementById("chatLog");

export const emptyState = document.getElementById("emptyState");

export const emptyEyebrow = document.getElementById("emptyEyebrow");

export const emptyTitle = document.getElementById("emptyTitle");

export const emptySub = document.getElementById("emptySub");

export const emptyHints = document.getElementById("emptyHints");

export const chatForm = document.getElementById("chatForm");

export const messageInput = document.getElementById("messageInput");

export const sendBtn = document.getElementById("sendBtn");

export const fileInput = document.getElementById("fileInput");

export const folderInput = document.getElementById("folderInput");

export const attachFileBtn = document.getElementById("attachFileBtn");

export const attachFolderBtn = document.getElementById("attachFolderBtn");

export const attachChips = document.getElementById("attachChips");

export const strengthToggle = document.getElementById("strengthToggle");

export const micBtn = document.getElementById("micBtn");

export const chatModeControls = document.getElementById("chatModeControls");

export const imageModeControls = document.getElementById("imageModeControls");

export const imageQualityToggle = document.getElementById("imageQualityToggle");

export const imageModeNote = document.getElementById("imageModeNote");

export const imageSizeSelect = document.getElementById("imageSizeSelect");

export const imageStyleSelect = document.getElementById("imageStyleSelect");

export const modelSelect = document.getElementById("modelSelect");

export const channelRow = document.getElementById("channelRow");

export const patchBayLabel = document.getElementById("patchBayLabel");

export const channelNote = document.getElementById("channelNote");

export const sidebarBottom = document.getElementById("sidebarBottom");

export const statusDot = document.getElementById("statusDot");

export const statusText = document.getElementById("statusText");

export const threadList = document.getElementById("threadList");

export const newChatBtn = document.getElementById("newChatBtn");

export const deleteBtn = document.getElementById("deleteBtn");

export const topbarTitle = document.getElementById("topbarTitle");

export const topbarModelChip = document.getElementById("topbarModelChip");

export const sidebar = document.getElementById("sidebar");

export const mobileToggle = document.getElementById("mobileToggle");

export const clockEl = document.getElementById("clock");

export const composerHintText = document.getElementById("composerHintText");

export const creditBarFill = document.getElementById("creditBarFill");

export const creditCount = document.getElementById("creditCount");

export const clearThreadsBtn = document.getElementById("clearThreads");

/* ----------------------------------------------------------------
   Settings modal — tabs, account, plan, about
   ---------------------------------------------------------------- */
export const settingsBtn = document.getElementById("settingsBtn");

export const settingsBtnLabel = document.getElementById("settingsBtnLabel");

export const settingsBackdrop = document.getElementById("settingsBackdrop");

export const settingsClose = document.getElementById("settingsClose");

export const authSignedOut = document.getElementById("authSignedOut");

export const authSignedIn = document.getElementById("authSignedIn");

export const googleSignInBtn = document.getElementById("googleSignInBtn");

export const googleAuthError = document.getElementById("googleAuthError");

export const googleNotConfiguredNote = document.getElementById(
  "googleNotConfiguredNote",
);

export const accountAvatar = document.getElementById("accountAvatar");

export const accountEmail = document.getElementById("accountEmail");

export const accountPlanBadge = document.getElementById("accountPlanBadge");

export const logoutBtn = document.getElementById("logoutBtn");

export const planNote = document.getElementById("planNote");

export const planError = document.getElementById("planError");

export const planFineprint = document.getElementById("planFineprint");

export const planFreeBtn = document.getElementById("planFreeBtn");

export const planProBtn = document.getElementById("planProBtn");

export const aboutStatusDot = document.getElementById("aboutStatusDot");

export const aboutStatusText = document.getElementById("aboutStatusText");

export const aboutModelName = document.getElementById("aboutModelName");

/* ---- Memory: custom instructions + remembered facts ---- */
export const customInstructionsInput = document.getElementById(
  "customInstructionsInput",
);

export const saveInstructionsBtn = document.getElementById("saveInstructionsBtn");

export const instructionsSavedNote = document.getElementById(
  "instructionsSavedNote",
);

export const memoryList = document.getElementById("memoryList");

export const memoryAddForm = document.getElementById("memoryAddForm");

export const memoryAddInput = document.getElementById("memoryAddInput");

/* ---- Local email/password signup + login ---- */
export const localAuthForm = document.getElementById("localAuthForm");

export const localAuthEmail = document.getElementById("localAuthEmail");

export const localAuthPassword = document.getElementById("localAuthPassword");

export const localAuthSubmit = document.getElementById("localAuthSubmit");

export const localAuthError = document.getElementById("localAuthError");

export const localAuthSwitch = document.getElementById("localAuthSwitch");

export const localAuthName = document.getElementById("localAuthName");

export const localAuthAge = document.getElementById("localAuthAge");

export const forgotSwitch = document.getElementById("forgotSwitch");

export const resetForm = document.getElementById("resetForm");

/* ----------------------------------------------------------------
   Appearance — accent colour, sky-theme override, account info.
   All of it is per-browser (localStorage), not per-account: it's a
   display preference, not data worth a server round-trip, and it needs
   to apply before first paint rather than after a fetch resolves.
   ---------------------------------------------------------------- */
export const accentSwatches = document.getElementById("accentSwatches");

export const customAccent = document.getElementById("customAccent");

export const skyThemeSelect = document.getElementById("skyThemeSelect");

export const userInfoGrid = document.getElementById("userInfoGrid");

/* ---- Subscription dashboard: real plan status from Stripe ---- */
export const subDashboard = document.getElementById("subDashboard");

export const subPlanValue = document.getElementById("subPlanValue");

export const subStatusRow = document.getElementById("subStatusRow");

export const subStatusValue = document.getElementById("subStatusValue");

export const subRenewalRow = document.getElementById("subRenewalRow");

export const subRenewalLabel = document.getElementById("subRenewalLabel");

export const subRenewalValue = document.getElementById("subRenewalValue");

export const managePlanBtn = document.getElementById("managePlanBtn");

export const subEmailValue = document.getElementById("subEmailValue");

export const subVerifiedValue = document.getElementById("subVerifiedValue");

export const subSinceValue = document.getElementById("subSinceValue");

export const subCreditsValue = document.getElementById("subCreditsValue");

export const subPaymentRow = document.getElementById("subPaymentRow");

export const subPaymentValue = document.getElementById("subPaymentValue");

/* ----------------------------------------------------------------
   Boot screen — shown until the first real data load resolves, with
   a minimum hold so it never just flashes on a fast connection.
   ---------------------------------------------------------------- */
export const bootScreen = document.getElementById("bootScreen");

export const bootLabel = document.getElementById("bootLabel");

/* ----------------------------------------------------------------
   Collapsible sidebar
   ---------------------------------------------------------------- */
export const sidebarCollapseBtn = document.getElementById("sidebarCollapse");

export const appShell = document.querySelector(".app");

/* ----------------------------------------------------------------
   Video bay - generation

   A panel rather than a thread: a clip takes minutes and has settings,
   which is not a shape the chat log has a message type for.

   This replaced an ffmpeg editor. There is no source footage: the
   inputs are a sentence, the model's settings and optionally a start
   image; the output is a clip the server has fetched, checked and kept
   (videogen.py), listed in the grid the studio draws.

   Generation is slow (minutes, not seconds) and costs real money per
   clip, so two things matter more here than elsewhere: the remaining
   quota is always on screen, and the wait says what is happening rather
   than showing an unexplained spinner.
   ---------------------------------------------------------------- */
export const videoBay = document.getElementById("videoBay");

export const genForm = document.getElementById("genForm");

export const genPrompt = document.getElementById("genPrompt");

export const genRun = document.getElementById("genRun");

export const genSeconds = /** @type {HTMLSelectElement} */ (document.getElementById("genSeconds"));

export const genRatio = /** @type {HTMLSelectElement} */ (document.getElementById("genRatio"));

export const genQuality = /** @type {HTMLSelectElement} */ (document.getElementById("genQuality"));

export const genQuotaEl = document.getElementById("genQuota");

export const genLocked = document.getElementById("genLocked");

export const genLockedText = document.getElementById("genLockedText");

export const genSub = document.getElementById("genSub");

export const videoStatusEl = document.getElementById("videoStatus");

export const videoResult = document.getElementById("videoResult");

export const modelOut = document.getElementById("modelOut");

export const genTitle = document.getElementById("genTitle");

export const videoDownload = document.getElementById("videoDownload");

export const genExamples = document.getElementById("genExamples");

// The "See Pro" button in the locked state. Opens the settings panel at
// the plan section rather than a separate modal, so there is one place
// in this app where a plan is chosen.
export const genUpgrade = document.getElementById("genUpgrade");
