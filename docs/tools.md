# Tool reference

Skillwright exposes a fixed authoring surface plus one generated MCP tool for each saved skill.

## Browser

| Tool | Purpose |
| --- | --- |
| `browser_navigate` | Navigate the interactive authoring browser |
| `browser_snapshot` | Read the current accessibility snapshot |
| `browser_click` | Click a snapshot target |
| `browser_fill` | Fill ordinary persisted text |
| `browser_fill_secret` | Fill an environment-backed secret without persisting plaintext |
| `browser_select` | Select one or more dropdown values |
| `browser_wait` | Wait for time, text, or text disappearance |

## Recording and discovery

| Tool | Purpose |
| --- | --- |
| `skill_record_start` | Mark the start of a recording range |
| `skill_record_stop` | Compile and save the current recording |
| `skill_save_from_history` | Save an existing contiguous action range |
| `skill_list` | List saved skills and current versions |
| `skill_search` | Search skills by name and description |
| `skill_get` | Read one workflow definition |

## Workflow schema

| Tool | Purpose |
| --- | --- |
| `skill_parameterize` | Replace recorded literals with typed inputs |
| `skill_output_add` | Add a typed extraction output |

## Secrets

| Tool | Purpose |
| --- | --- |
| `skill_secret_bind` | Bind a secret input to an environment reference |
| `skill_secret_unbind` | Remove a secret binding |
| `skill_secret_status` | Show whether secret inputs are configured |

## Versions, repair, and composition

| Tool | Purpose |
| --- | --- |
| `skill_versions` | List immutable versions |
| `skill_rollback` | Create a new version from an older definition |
| `skill_repair` | Replay-validate a proposed repair and save it on success |
| `skill_compose` | Create a skill from pinned versions of existing skills |

## Generated tools

Every saved skill gets a stable generated name such as:

```text
skillwright_search_docs_911bf176
```

Public workflow inputs become typed MCP parameters. Secret inputs are omitted. Declared workflow outputs become structured output fields.

[Back to docs](README.md)
