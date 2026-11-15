/**
 * Type definitions mirroring the OpenAI-compatible shapes returned by
 * randomgenerals.com's /v1/models and /v1/chat/completions routes
 * (see openai_api.py in the backend for the source of truth).
 */

export interface ModelListResponse {
  object: "list";
  data: Array<{
    id: string;
    object: "model";
    created?: number;
    owned_by?: string;
  }>;
}

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ChatCompletionChoice {
  index: number;
  message: {
    role: "assistant";
    content: string;
  };
  finish_reason: string;
}

export interface ChatCompletionResponse {
  id: string;
  object: "chat.completion";
  created: number;
  model: string;
  choices: ChatCompletionChoice[];
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

/** The error body shape returned by _api_error() in app.py. */
export interface ApiErrorBody {
  error: {
    message: string;
    type: string;
    code?: string;
  };
}
