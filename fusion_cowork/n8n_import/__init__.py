"""n8n workflow import (issue #395, import-only v1).

Commitment: what we accept runs; what we cannot run is declared up front,
never silently half-imported. Export to n8n format is out of scope for v1.

- analyzer: classify every n8n node (mappable / mappable_with_warning /
  unsupported) + user-facing coverage report
- expressions: n8n expression subset -> cowork ${step.<id>.<path>} refs
  (independent module, contract tests over shared fixtures)
- importer: n8n export JSON -> cowork workflow dict (0903 canvas schema),
  import-and-verify status honesty (imported vs imported_unverified)
"""

from .analyzer import N8nImportReport, analyze_n8n_workflow
from .expressions import UnsupportedExpression, translate_expression_value
from .importer import import_n8n_workflow

__all__ = [
    "N8nImportReport",
    "analyze_n8n_workflow",
    "import_n8n_workflow",
    "UnsupportedExpression",
    "translate_expression_value",
]
