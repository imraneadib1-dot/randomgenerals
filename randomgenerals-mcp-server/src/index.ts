#!/usr/bin/env node
/**
 * MCP server for randomgenerals.com.
 *
 * Wraps the OpenAI-compatible surface randomgenerals.com already exposes
 * with an API key (/v1/models, /v1/chat/completions) as MCP tools, so an
 * MCP-compatible client can list models and chat through the account's own
 * model routing (pip 1.0 -> OpenRouter -> local Ollama).
 *
 * Everything else in the app (image generation, web search, code
 * execution, video, threads, memory) is currently session-cookie
 * authenticated only and is not exposed here - see README.md.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import express from "express";
import { registerListModels } from "./tools/listModels.js";
import { registerChat } from "./tools/chat.js";

function createServer(): McpServer {
  const server = new McpServer({
    name: "randomgenerals-mcp-server",
    version: "1.0.0",
  });

  registerListModels(server);
  registerChat(server);

  return server;
}

function requireApiKey(): void {
  if (!process.env.RANDOMGENERALS_API_KEY) {
    console.error(
      "ERROR: RANDOMGENERALS_API_KEY environment variable is required.\n" +
        "Create one at https://randomgenerals.com under Settings, then set it, e.g.:\n" +
        "  RANDOMGENERALS_API_KEY=sk-... npx randomgenerals-mcp-server",
    );
    process.exit(1);
  }
}

async function runStdio(): Promise<void> {
  requireApiKey();
  const server = createServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("randomgenerals-mcp-server running via stdio");
}

async function runHttp(): Promise<void> {
  requireApiKey();
  const app = express();
  app.use(express.json());

  app.post("/mcp", async (req, res) => {
    // A fresh server + transport per request: stateless, so concurrent
    // requests never collide over request IDs or session state.
    const server = createServer();
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    });
    res.on("close", () => {
      transport.close();
      server.close();
    });
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  });

  const port = parseInt(process.env.PORT || "3000", 10);
  app.listen(port, () => {
    console.error(`randomgenerals-mcp-server running on http://localhost:${port}/mcp`);
  });
}

const transportMode = process.env.TRANSPORT || "stdio";
const run = transportMode === "http" ? runHttp() : runStdio();

run.catch((error) => {
  console.error("Server error:", error);
  process.exit(1);
});
