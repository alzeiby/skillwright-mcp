# Repair and composition

## Repair

Generated skills execute deterministically. If a step cannot be resolved or executed safely, the tool returns `status="repair_required"` instead of changing the workflow automatically.

Repair evidence can include:

- workflow version
- failing step and operation
- expected semantic target
- current page URL and title
- accessibility snapshot
- ranked replacement candidates
- browser/action error
- workflow and current-step side-effect state

The connected agent can inspect that evidence and, if needed, use the normal `browser_*` tools to inspect the current page.

`skill_repair` accepts exactly one repair form:

- `replacement_target`
- `replacement_step`
- `replacement_workflow`

Target-only repair:

```text
skill_repair(
  name="search_docs",
  base_version=3,
  step=2,
  replacement_target={"role": "button", "name": "Search now"}
)
```

Skillwright replays the proposed workflow before saving it. Failed validation leaves the current version unchanged. Successful validation creates a new immutable version and refreshes the generated MCP tool.

Repair validation replays from the start. For workflows with external side effects, use the reported side-effect state when deciding whether a retry is safe.

## Composition

`skill_compose` creates a workflow whose steps call pinned versions of existing skills.

```text
skill_compose(
  name="download_report",
  inputs={
    "user": {"type": "string"},
    "report_id": {"type": "integer"}
  },
  calls=[
    {
      "skill": "portal_login",
      "inputs": {"user": "{{ user }}"}
    },
    {
      "skill": "open_report",
      "inputs": {"report_id": "{{ report_id }}"},
      "outputs": {"title": "report_title"}
    }
  ]
)
```

Composition keeps child workflows intact. The saved parent stores each child skill name and immutable version rather than flattening child browser steps into the parent.

Children execute in the same browser instance for one composed invocation, so navigation, login state, cookies, and page state carry across child calls.

Before saving, Skillwright checks:

- required child inputs are available
- referenced values exist
- primitive types are compatible
- nullable values are not passed into required non-null inputs
- declared parent outputs resolve to available non-null values

Changing a child later does not silently change an existing composed parent. Create a new parent version to adopt the newer child version.

[Back to docs](README.md)
