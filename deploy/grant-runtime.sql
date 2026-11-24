GRANT SELECT ON tenants, policy_versions, alembic_version TO :"runtime_role";
GRANT SELECT, INSERT ON sessions, session_events, context_builds TO :"runtime_role";
GRANT UPDATE (next_sequence) ON sessions TO :"runtime_role";
GRANT INSERT ON audit_events TO :"runtime_role";
