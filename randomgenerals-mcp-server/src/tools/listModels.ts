/**
 * randomgenerals_list_models - GET /v1/models
 */
import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { getClient, describeApiError } from "../client.js";
import type { ModelListResponse } from "../types.js";

const ListModelsInputSchema = z.object({}).strict();

export function registerListModels(server: McpServer): void {
  server.registerTool(
    "randomgenerals_list_models",
    {
      title: "List randomgenerals.com Models",
      description: `List the model names currently available to your randomgenerals.com account.

This calls GET /v1/models on the account's own deployment. What comes back depends on what is actually reachable right now - "pip-1.0" when the engine is up, then the account's local Ollama models when one is configured and online; with neither, a single "pip-1.0" entry standing in for whichever channel answers automatically. It does NOT create, modify, or delete anything.

Args: none.

Returns:
  A markdown list of model ids. Pass one of these ids as the \`model\` argument to randomgenerals_chat, or omit \`model\` there entirely to let the server route automatically.

Examples:
  - Use when: "what models can I use on my randomgenerals account" -> call with no arguments.
  - Don't use when: you just want to send a chat message - call randomgenerals_chat directly, it will route to a working model on its own.

Error Handling:
  - Returns "Error: Invalid API key..." (401) if RANDOMGENERALS_API_KEY is missing, wrong, or revoked.`,
      inputSchema: ListModelsInputSchema,
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    async () => {
      try {
        const client = getClient();
        const { data } = await client.get<ModelListResponse>("/models");
        const ids = data.data.map((m) => m.id);

        if (ids.length === 0) {
          return {
            content: [
              {
                type: "text",
                text: "No models are currently reported as available.",
              },
            ],
          };
        }

        const lines = [
          `# Available Models (${ids.length})`,
          "",
          ...ids.map((id) => `- \`${id}\``),
        ];

        return {
          content: [{ type: "text", text: lines.join("\n") }],
          structuredContent: { models: ids },
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
