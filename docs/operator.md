# The operator subagent

`screen-agent` gives an agent exact, cheap perception. This is how you put that behind a
single delegation instead of a dozen tool calls in the main conversation.

You give it a **goal**. It runs its own perceive-act-verify loop in its own context and
returns one JSON object. The main conversation sees that object, not the twenty tool
results and screenshots behind it.

## Measured

The same task both ways — go to page 3 of a results list, open the 3rd result:

| | Main agent, tool by tool | Operator subagent |
| --- | --- | --- |
| screenshots | 12 | **0** |
| tool calls | 15 | 8 |
| what the caller receives | every result and image | one JSON object |
| correctness | coordinate estimated off a downscaled image | `page_click` verified the URL |

Both reached `.../item3/23`. Run `python bench/bench_latency.py` for the per-operation
costs behind the left-hand column; the right-hand column is a property of the protocol,
not a timing.

## Install

`~/.config/opencode/opencode.jsonc` (global) or `opencode.json` in the project:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "screen-agent": { "type": "local", "command": ["screen-agent", "serve"] }
    }
  },
  "agent": {
    "desktop-operator": {
      "description": "Drives the Windows desktop through screen-agent. Give it a GOAL, not a list of clicks: it runs its own perceive-act-verify loop and returns a single JSON result.",
      "mode": "subagent",
      "steps": 40,
      "permissions": [
        { "action": "*", "resource": "*", "effect": "allow" },
        { "action": "edit", "resource": "*", "effect": "deny" },
        { "action": "subagent", "resource": "*", "effect": "deny" }
      ],
      "system": "…"
    }
  }
}
```

The `system` prompt is in `operator-system.md` in this directory, verbatim.

Then use it like any subagent, with a goal:

```
delegate to desktop-operator: go to page 3 of the Bing results for "mcp protocol"
and open the 3rd result; tell me the URL you ended on
```

## Why the permissions are shaped that way

A subagent uses **its own** permissions, not a subset of its parent's. That is the point
worth knowing: anything you do not explicitly deny here, you have granted. The list above
denies exactly two things — writing files, and nesting further subagents — and allows the
rest, so the operator can call the screen-agent tools and read a screenshot if it truly
needs one, without depending on how the MCP tool names are normalised.

Denying `edit` matters more than it looks. The operator runs unattended against a real
desktop; the one thing it has no business doing is modifying the machine it is driving.

## The two failure modes, and what happens in each

**The model does not follow the protocol.** Observed with a small free model
(`longcat-2.5-preview-free`): it completed the goal correctly and then returned prose
instead of JSON. The task succeeded; the contract was broken. A caller that only does
`json.loads(result)` will fail on that, so parse defensively:

```python
def parse_operator(text: str) -> dict:
    """Accept a bare JSON object, or one embedded in prose."""
    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON in operator result: {text[:200]}")
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        # prose wrapped it, or trailed something after it - take the last complete object
        for end in range(len(text), start, -1):
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"unparseable operator result: {text[:200]}")
```

Or pin a model in the agent config. A small free model is fine for the loop and unreliable
for the output contract; those are different skills.

**The model reaches for the wrong browser.** opencode ships its own `tools.browser.*`
namespace, and before the tools were renamed an operator called it, saw it
disconnected, and concluded the working `browser_find` was also broken — then walked away
from a task it could have finished. That is why the page tools are named `page_*` and why
the system prompt says so explicitly. It is a name collision with the host, and it cost a
real run.

## Honest limits

- **One goal per delegation is the unit.** A subagent is not free: the call itself is an
  inference, and so is reading the result. Against a 0.9-second task that is a net loss,
  and the loop is better driven directly. The operator earns its keep when a task needs
  many perception steps, or when the images would otherwise pile up in the main context.
- **The output is prose, not a schema.** Subagent results are text; JSON inside them is a
  convention, not a guarantee. The OpenCode docs confirm there is no output-schema
  mechanism for subagents.
- **Nothing is proven about a small model.** The successful run took 103 s, nearly all of
  it inference on a free model against 8 tool calls that cost under 10 ms. The loop is
  fast; the model is the variable.
- **A stale MCP server looks like a missing tool.** An MCP server is spawned when a
  session starts and lives for it, so tools added to the package are invisible until the
  client restarts. The operator cannot tell "the tool does not exist" from "the server is
  old", so it is told to report it rather than work around it — and you restart the
  client, which takes seconds.
