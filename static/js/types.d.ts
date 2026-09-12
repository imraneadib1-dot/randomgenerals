/* Ambient declarations for what the page puts on `window` from outside
   these modules: the CDN scripts index.html loads (highlight.js, KaTeX,
   mermaid, Paddle.js) and the Electron preload bridge (desktop/src/
   preload.js). Nothing here is shipped - it exists so tsc --checkJs can
   tell "a global the page provides" from "a name nobody defined".

   Keep the shapes minimal and honest: only the members the app calls.
   A wrong signature here is worse than `any`, because it turns a real
   mistake into a passing check. */

export {};

declare global {
  /** highlight.js 11 (UMD build). */
  const hljs: {
    highlightElement(el: HTMLElement): void;
    highlightAuto(code: string): { value: string; language?: string };
  };

  /** KaTeX auto-render contrib. */
  function renderMathInElement(
    el: HTMLElement,
    options?: {
      delimiters?: { left: string; right: string; display: boolean }[];
      throwOnError?: boolean;
      ignoredTags?: string[];
    },
  ): void;

  /** mermaid 10 (UMD build). */
  const mermaid: {
    initialize(config: Record<string, unknown>): void;
    render(id: string, source: string): Promise<{ svg: string }>;
    parse(source: string): Promise<boolean>;
  };

  /** Paddle.js v2 (Paddle Billing). Loaded on demand by loadPaddle(). */
  interface PaddleStatic {
    Environment: { set(env: "sandbox" | "production"): void };
    Initialize(options: {
      token: string;
      pwCustomer?: { id: string };
      eventCallback?: (event: { name: string; data?: unknown }) => void;
    }): void;
    Checkout: { open(options: { transactionId: string }): void };
    /** Retain: tell it the customer once they are known mid-session. */
    Update?(options: { pwCustomer: { id: string } }): void;
  }
  const Paddle: PaddleStatic;

  /* SCAFFOLDING, to be removed as the modules are split out.

     Nearly every element in app.js is fetched with getElementById and
     then treated as the input, select, form or image it is - which the
     DOM lib types as a bare HTMLElement. Two hundred such reads is not
     worth two hundred casts today; dom.js (the split's first module)
     will fetch elements with their real types, and this block goes
     with it. Until then these keep the check useful for what it is
     for: an undeclared name, a wrong call, a property that does not
     exist anywhere. */
  interface HTMLElement {
    value: string;
    disabled: boolean;
    checked: boolean;
    placeholder: string;
    files: FileList | null;
    src: string;
    href: string;
    required: boolean;
    open: boolean;
    min: string;
    max: string;
    autocomplete: string;
    reset(): void;
    requestSubmit(): void;
  }
  interface Element {
    hidden: boolean;
    dataset: DOMStringMap;
    style: CSSStyleDeclaration;
    click(): void;
  }
  interface EventTarget {
    closest(selector: string): Element | null;
  }
  interface Navigator {
    /** iOS Safari: true when running as an installed web app. */
    standalone?: boolean;
  }
  interface Window {
    SpeechRecognition?: new () => SpeechRecognition;
    webkitSpeechRecognition?: new () => SpeechRecognition;
  }

  /** The Electron bridge from desktop/src/preload.js. Absent on the web. */
  interface DesktopBridge {
    isDesktop: true;
    platform: string;
    terminal: {
      run(command: string, options?: Record<string, unknown>): Promise<
        { sessionId: string } | { error: string }
      >;
      kill(sessionId: string): Promise<unknown>;
      onOutput(
        handler: (payload: {
          sessionId: string;
          stream: "stdout" | "stderr";
          chunk: string;
        }) => void,
      ): () => void;
      onExit(
        handler: (payload: {
          sessionId: string;
          code: number | null;
          signal: string | null;
          timedOut: boolean;
        }) => void,
      ): () => void;
    };
    [key: string]: unknown;
  }

  interface Window {
    Paddle?: PaddleStatic;
    mermaid?: typeof mermaid;
    hljs?: typeof hljs;
    renderMathInElement?: typeof renderMathInElement;
    desktop?: DesktopBridge;
    /** model-viewer registers itself; the app only checks presence. */
    customElements: CustomElementRegistry;
  }
}
