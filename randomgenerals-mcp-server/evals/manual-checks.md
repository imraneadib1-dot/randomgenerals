# Manual checks

Not a scored eval suite (see the README for why) — a short list of
scenarios worth running by hand against a real account whenever
`client.ts`, `tools/chat.ts`, or `tools/listModels.ts` change. Each has a
single expected *behavior*, even though the exact wording of a live model
reply is not something to assert on.

1. **Valid key, list models.** `randomgenerals_list_models` with a real
   key returns at least one model id, formatted as a markdown list.
2. **Valid key, simple chat.** `randomgenerals_chat` with
   `messages=[{role:"user",content:"Say the word banana."}]` returns a
   reply containing "banana" and a `_model: ...` footer line.
3. **Multi-turn context.** Send two calls: first establishes a fact
   ("My favorite color is teal"), second asks "What's my favorite
   color?" with *both* turns in `messages`. Reply should reference teal —
   this checks that the tool is passing full history, not just the
   latest message.
4. **Missing key.** Unset `RANDOMGENERALS_API_KEY` — the process should
   exit 1 immediately with a message pointing at Settings, before ever
   reaching the MCP handshake.
5. **Revoked/wrong key.** A well-formed but wrong key gets a single
   sentence starting "Error: Invalid API key" (HTTP 401), not a raw
   stack trace or axios error dump.
6. **Pro-only model, free account.** Requesting a model gated to Pro
   returns "Error: ... needs a Pro plan." (HTTP 403), not a silent
   downgrade to a different model.
7. **Out of credits.** On an account with a drained balance, the error
   reads "Error: Out of credits..." (HTTP 402).
8. **Empty messages array.** `randomgenerals_chat` with `messages: []`
   is rejected before any network call, with a validation error
   mentioning "at least one message" — confirms Zod's `.min(1)` is wired
   up, not just documented.
9. **Unknown model id.** Passing a nonsense `model` string (e.g.
   `"not-a-real-model"`) should NOT error — the backend falls back to a
   working channel per `_v1_route()`'s documented behavior. Confirms the
   tool isn't second-guessing the backend's own fallback logic.
10. **Long reply truncation.** Ask for a very long generation
    (`max_tokens` high, prompt like "write 3000 words about..."). If the
    reply exceeds `CHARACTER_LIMIT` (12000 chars), the response should end
    with a `[... truncated at 12000 characters]` marker and
    `structuredContent.truncated` should be `true`.
