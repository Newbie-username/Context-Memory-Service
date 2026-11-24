from dataclasses import dataclass

from pydantic import Field, ValidationError
from sqlalchemy import Connection, text

from app.core.contracts import Contract, Identifier, LongText, Source
from app.core.errors import ServiceError, denied


class Rules(Contract):
    sources: frozenset[Source]
    instruction: LongText | None = None
    constraints: dict[Identifier, LongText] = Field(default_factory=dict, max_length=32)


@dataclass(frozen=True)
class EffectivePolicy:
    sources: frozenset[Source]
    instructions: tuple[str, ...]
    constraints: dict[str, str]
    versions: dict[str, int]


def effective_policy(
    conn: Connection, tenant: str, application: str, scopes: frozenset[str]
) -> EffectivePolicy:
    rows = conn.execute(
        text("""
        SELECT DISTINCT ON (application_id, scope) application_id, scope, version, rules
        FROM policy_versions
        WHERE tenant_id = :tenant AND application_id IN ('', :application)
          AND (scope = '' OR scope = ANY(CAST(:scopes AS text[])))
          AND starts_at <= now() AND (ends_at IS NULL OR ends_at > now())
        ORDER BY application_id, scope, version DESC
    """),
        {"tenant": tenant, "application": application, "scopes": sorted(scopes)},
    ).all()
    required = {(tenant_app, "") for tenant_app in ("", application)}
    if not required <= {(row.application_id, row.scope) for row in rows}:
        raise denied()

    sources = frozenset({"knowledge", "history", "memory"})
    instructions = []
    constraints = {}
    versions = {}
    for row in rows:
        try:
            rules = Rules.model_validate(row.rules)
        except ValidationError:
            raise ServiceError(503, "POLICY_INVALID") from None
        sources &= rules.sources
        # Only tenant/application text is trusted. Scoped rules narrow access or add constraints.
        if rules.instruction is not None and not row.scope:
            instructions.append(rules.instruction)
        for key, value in rules.constraints.items():
            if key in constraints and constraints[key] != value:
                raise ServiceError(403, "AUTH_SCOPE_DENIED", reason="policy_conflict")
            constraints[key] = value
        selector = "tenant" if not row.application_id else (row.scope or "application")
        versions[selector] = row.version
    return EffectivePolicy(sources, tuple(instructions), constraints, versions)
