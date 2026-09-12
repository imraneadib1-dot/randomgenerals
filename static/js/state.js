// Every piece of mutable state the app shares between modules.
//
// These were top-level `let`s in one 5,000-line script. An ES module
// binding cannot be reassigned by an importer, so the state lives on
// one object and is read and written as state.<name>. Initial values
// are exactly what the `let`s had.
export const state = {
  currentBay: "chat",
  currentThreadId: null,
  providers: [],
  activeProvider: null,
  localImageAvailable: false,
  imageBackend: "flux",
  imageSize: "square",
  imageStyle: "none",
  accountNickname: "",
  settingsDoc: null,
  pendingAttachments: [],
  uploadingCount: 0,
  currentStrength: "quick",
  recommended: {},
  activeStreamController: null,
  retriedAfterLostThread: false,
  currentUser: null,
  subscriptionState: null,
  upgradePoll: null,
  localAuthMode: "signup",
  billingLive: false,
  paddleReady: null,
  lastPaddleCustomer: "",
  genKind: "video",
  videoPoll: null,
  mermaidReady: false,
  settingsSaveTimer: null,
};
