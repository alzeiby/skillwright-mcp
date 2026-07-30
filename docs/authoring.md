# Authoring skills

## Recording

`skill_record_start` marks the beginning of a browser-action range in the current Skillwright process. `skill_record_stop` compiles the successful actions in that range into a workflow.

Recording state is process-local. Browser actions themselves are persisted in SQLite authoring history.

If the useful actions are already in the current browser history, save a range directly:

```text
skill_save_from_history(
  name="search_docs",
  start_event=12,
  end_event=18,
  description="Search the docs"
)
```

Deterministic replay and repair validation do not append rows to authoring history.

## Targets

Snapshot refs such as `e5` are temporary Playwright MCP references. They are used while authoring but are not the replay identity of a saved element.

Saved targets can contain:

- accessible role and name
- a durable Playwright locator
- stable attributes
- nearby text
- page URL context
- compatibility fields from older workflow versions

Replay resolves the current page element from that evidence.

## Inputs

`skill_parameterize` replaces a recorded literal with a workflow input and writes a new version.

```text
skill_parameterize(
  name="search_docs",
  bindings=[{
    "step": 1,
    "field": "value",
    "input_name": "query",
    "input_type": "string",
    "description": "Search query"
  }]
)
```

Supported primitive types:

- `string`
- `number`
- `integer`
- `boolean`

Parameterizable fields:

| Step | Field |
| --- | --- |
| `navigate` | `url` |
| `fill` | `value` |
| `select` | `select_value` with `item_index` |
| `wait` | `text` or `text_gone` |

Generated MCP tools expose public inputs as real typed parameters with required/default semantics.

## Outputs

`skill_output_add` appends an extraction step and declares a typed output:

```text
skill_output_add(
  name="search_docs",
  output_name="result_title",
  target={"role": "heading", "name": "Search results"},
  output_type="string"
)
```

Declared outputs appear in the generated MCP tool's structured output schema. They are also the typed boundary used by composition.

## Secrets

Secret inputs are strings, required, server-bound, and omitted from the public generated-tool schema.

During authoring use `browser_fill_secret`:

```text
browser_fill_secret(
  target="e7",
  secret_ref="LOGIN_PASSWORD",
  input_name="password",
  element="Password"
)
```

For `LOGIN_PASSWORD`, Skillwright reads:

```text
SKILLWRIGHT_SECRET_LOGIN_PASSWORD
```

The value is resolved on each invocation. Rotating an environment variable does not require a new workflow version.

Use `skill_secret_status`, `skill_secret_bind`, and `skill_secret_unbind` to inspect or change bindings without exposing plaintext values.

## Versions

Workflow definitions are immutable. Recording, parameterization, output changes, repair, composition, and rollback create new versions rather than rewriting an existing definition.

Use:

- `skill_versions` to list versions
- `skill_get` to inspect a version
- `skill_rollback` to create a new current version from an older definition

Generated MCP tools always point at the current saved version.

See [Tool reference](tools.md) for the complete built-in MCP surface.
