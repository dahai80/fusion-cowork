# Shared Markdown Skill Package Format

Issue #167. One format, two products: a package dropped into a skills directory is discoverable and executable in both **fusion-cowork** (DAG/skill invocation) and **fusion-agent-studio** (agent/slash-command invocation), with structured forms rendered from the same parameter schema in both web UIs.

Reference implementation (fusion-agent-studio): `agent_runtime/skills_store.py::parse_skill_md` (issue #430). Cowork loader: `fusion_cowork/skills/packages.py::parse_markdown`.

## Package layout

A package is a single Markdown file with an optional YAML frontmatter block:

```markdown
---
name: contract-review
command: /contract-review
description: Local contract review producing a clause-cited memo
version: "1.0"
allowed-tools: [fetch_url, file_input, file_output]
parameters:
  type: object
  required: [contract_path]
  properties:
    contract_path:
      type: string
      title: Contract file
      description: ~/Documents/contract.md
    strictness:
      type: string
      enum: [low, medium, high]
      default: medium
---

You are a contract review assistant. Review the contract below...
```

## Frontmatter fields

| Field | Aliases | Required | Notes |
|---|---|---|---|
| `name` | `command`, `slash` | yes | Invocation name; `/` prefix added if missing |
| `command` | `slash` | no | Explicit slash command (defaults to `name`) |
| `description` | — | recommended | Shown in menus/palette |
| `version` | — | no | Semver-ish string |
| `allowed-tools` | `allowed_tools`, `tools` | no | Recommended tool subset; list or comma string |
| `parameters` | `arguments` | no | JSON Schema (`type: object`, `properties`, `required`) driving structured forms |
| `output-template` | `output_template` | no | Output format block appended to instructions |

### Product-specific extensions

Unknown keys are preserved (studio: `extra`) or mapped (cowork) — they never break the other loader:

| Field | Product | Notes |
|---|---|---|
| `handler` | cowork | Python handler name for direct execution |
| `params` | cowork | Legacy ParamSpec list; takes precedence over `parameters` when both present |
| `workflow` | cowork | Recommended DAG definition (placeholders `{{param}}` substituted) |
| `aliases`, `category`, `title`, `title_en`, `description_en` | cowork | Menu/registry metadata |

Cowork maps `parameters.properties` to its ParamSpec form fields: `type` (`string`/`integer`/`number`/`boolean`/`array`), `title` → label, `enum` → options, `default`, `required` (top-level list or per-property flag), `description` → placeholder.

## Invocation

- **cowork**: `GET /api/skills` lists packages with `params` (form schema); `POST /api/skills/{name}/run` with `{"params": {...}}` executes via handler / workflow / instructions.
- **agent-studio**: slash command (`/contract-review`) with schema-driven structured form in the Composer; body is rendered as the agent prompt.

## Installation directories

| Tier | cowork | agent-studio |
|---|---|---|
| Built-in | `fusion_cowork/skills/packages/` | bundled skills |
| User | `~/.fusion-cowork/skills/` | `~/.fusion-agent-studio/skills/` |
| Project | — | `.fusion-agent-studio/skills`, `.claude/skills` |
| Shared | `FUSION_COWORK_SKILLS_DIRS` (colon-separated) | — |

To install one pack into both products, either drop it into both user directories or point cowork at a shared directory: `export FUSION_COWORK_SKILLS_DIRS=~/.fusion/skills` and place the file under `~/.fusion/skills/`.

Distribution through fusion-plugins-ecosystem (one pack installs into either product) is tracked in fusion-plugins-ecosystem issue #27.

## Verified shared example

`packages/contract-review.md` in this repo uses the shared format (`parameters` JSON Schema + `allowed-tools` + `version` + cowork `handler`) and is covered by `tests/test_issue_167_shared_skill_format.py`.
