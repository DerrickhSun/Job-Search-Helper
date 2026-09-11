"""
Job Application Agent

Conversational agent for managing job application records and S3 sync.
Run directly: python agent.py
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """\
You are a job application assistant. You help users query and manage their job application records.

You have access to tools that can sync with S3 — use them only when the user's request actually
requires it. Avoid unnecessary S3 connections; prefer working with locally available data when
possible.

When asked about application history, cover letters, or syncing data, use the appropriate tool
rather than guessing about the current state of local or remote files.
"""

# TODO: define tools here (S3 download, S3 upload, local CSV lookup, …)
TOOLS: list[dict] = []


def _client():
    import anthropic
    return anthropic.Anthropic()


def run() -> None:
    client = _client()
    messages: list[dict] = []

    print("Job Application Agent  |  type 'exit' to quit\n")

    while True:
        try:
            raw = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if raw.lower() in ("exit", "quit", "q"):
            print("Goodbye.")
            break

        if not raw:
            continue

        messages.append({"role": "user", "content": raw})

        # Agentic loop — runs until end_turn or all tool calls are resolved.
        while True:
            kwargs: dict = dict(
                model=MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            if TOOLS:
                kwargs["tools"] = TOOLS

            with client.messages.stream(**kwargs) as stream:
                response = stream.get_final_message()

            messages.append({"role": "assistant", "content": response.content})

            # Print any text the assistant produced.
            text = " ".join(b.text for b in response.content if hasattr(b, "text"))
            if text:
                print(f"\nAssistant: {text}\n")

            if response.stop_reason == "end_turn":
                break

            # TODO: when tools are added, handle tool_use blocks here:
            #   for block in response.content:
            #       if block.type == "tool_use":
            #           result = call_tool(block.name, block.input)
            #           messages.append({"role": "user", "content": [
            #               {"type": "tool_result", "tool_use_id": block.id, "content": result}
            #           ]})
            #   continue  # loop back to let Claude process tool results

            # No tool handling yet — stop the inner loop.
            break


if __name__ == "__main__":
    run()
