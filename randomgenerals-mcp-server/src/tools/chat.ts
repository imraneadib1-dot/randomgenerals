/**
 * randomgenerals_chat - POST /v1/chat/completions
 */
import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { getClient, describeApiError } from "../client.js";
import { CHARACTER_LIMIT } from "../constants.js";
import type { ChatCompletionResponse, ChatMessage } from "../types.js";

const ChatMessageSchema = z.object({
  role: z
    .enum(["system", "user", "assistant"])
    .describe("Who this message is from."),
  content: z.string().min(1).describe("The message text."),
});

const ChatInputSchema = z
  .object({
    messages: z
      .array(ChatMessageSchema)
      .min(1, "Provide at least one message.")
      .describe(
        "The conversation so far, oldest first. A single user question is " +
          'just [{"role": "user", "content": "..."}]; include prior turns ' +
          "and an optional leading system message for multi-turn context.",
      ),
    model: z
      .string()
      .optional()
      .describe(
        "Optional model id (see randomgenerals_list_models). Omit to let " +
          "the server route to whichever channel - pip 1.0, OpenRouter, or a " +
          "local Ollama - is fastest and available right now. An unknown " +
          "id is not an error: the server falls back rather than " +
          "rejecting it, since editors often send their own default " +
          'names (e.g. "gpt-4o").',
      ),
    temperature: z
      .number()
      .min(0)
      .max(2)
      .optional()
      .describe(
        "Sampling temperature, 0-2. Lower is more deterministic. Omit to " +
          "use the server's default.",
      ),
    max_tokens: z
      .number()
      .int()
      .positive()
      .optional()
      .describe("Maximum tokens to generate. Omit for no explicit limit."),
  })
  .strict();

type ChatInput = z.infer<typeof ChatInputSchema>;

export function registerChat(server: McpServer): void {
  server.registerTool(
    "randomgenerals_chat",
    {
      title: "Chat via randomgenerals.com",
      description: `Send a conversation to randomgenerals.com and get back one assistant reply.

This calls POST /v1/chat/completions (non-streaming) using the account's own model routing - the same fast-then-fallback chain (pip 1.0 -> OpenRouter -> local Ollama) the randomgenerals.com website itself uses. It does NOT search the web, run code, or generate images; it is plain chat completion only. Each call is metered against the account's credit balance.

Args:
  - messages (array, required): conversation so far, oldest first, each {role: "system"|"user"|"assistant", content: string}.
  - model (string, optional): a specific model id from randomgenerals_list_models. Omit to auto-route.
  - temperature (number, optional): 0-2, sampling randomness.
  - max_tokens (number, optional): generation cap.

Returns:
  The assistant's reply as text, plus the model that actually answered (which may differ from the one requested if it fell back) and token usage when the server reports it.

Examples:
  - Use when: "ask my randomgenerals model to summarize this paragraph" -> messages=[{role:"user", content:"Summarize: ..."}]
  - Use when: continuing a conversation -> pass every prior turn in messages, not just the latest one; the server has no memory of previous calls.
  - Don't use when: you need real-time web results, code execution, or image generation - those are not exposed by this server yet.

Error Handling:
  - "Error: Invalid API key..." (401) - RANDOMGENERALS_API_KEY is missing or wrong.
  - "Error: Out of credits..." (402) - the account's credit balance is too low; credits refill on a timer.
  - "Error: ... needs a Pro plan." (403) - the requested model is Pro-only for this account.
  - "Error: No model is available right now..." (503) - every channel (pip 1.0, OpenRouter, Ollama) failed or is unreachable.`,
      inputSchema: ChatInputSchema,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    async (params: ChatInput) => {
      try {
        const client = getClient();
        const body: Record<string, unknown> = {
          messages: params.messages as ChatMessage[],
          stream: false,
        };
        if (params.model) body.model = params.model;
        if (params.temperature !== undefined)
          body.temperature = params.temperature;
        if (params.max_tokens !== undefined)
          body.max_tokens = params.max_tokens;

        const { data } = await client.post<ChatCompletionResponse>(
          "/chat/completions",
          body,
        );

        const choice = data.choices?.[0];
        const text = choice?.message?.content ?? "";

        if (!text) {
          return {
            content: [
              {
                type: "text",
                text: "The model returned an empty reply. Try rephrasing, or try again.",
              },
            ],
          };
        }

        let displayText = text;
        let truncated = false;
        if (displayText.length > CHARACTER_LIMIT) {
          displayText =
            displayText.slice(0, CHARACTER_LIMIT) +
            `\n\n[... truncated at ${CHARACTER_LIMIT} characters]`;
          truncated = true;
        }

        const meta = [`_model: ${data.model}`];
        if (choice?.finish_reason && choice.finish_reason !== "stop") {
          meta.push(`finish_reason: ${choice.finish_reason}`);
        }
        if (data.usage) {
          meta.push(
            `tokens: ${data.usage.prompt_tokens} prompt + ${data.usage.completion_tokens} completion`,
          );
        }

        return {
          content: [
            { type: "text", text: displayText },
            { type: "text", text: `\n---\n${meta.join(" · ")}_` },
          ],
          structuredContent: {
            model: data.model,
            finish_reason: choice?.finish_reason,
            usage: data.usage,
            truncated,
          },
        };
      } catch (error) {
        return {
          isError: true,
          content: [{ type: "text", text: describeApiError(error) }],
        };
      }
    },
  );
}
