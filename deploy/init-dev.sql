CREATE ROLE context_app LOGIN PASSWORD 'local-runtime' NOSUPERUSER NOBYPASSRLS;

INSERT INTO tenants (tenant_id) VALUES ('demo');
INSERT INTO policy_versions (tenant_id, application_id, version, rules)
VALUES
    ('demo', '', 1, '{"sources":["knowledge","history","memory"],"instruction":"Cite evidence and qualify uncertainty."}'),
    ('demo', 'it-helpdesk', 1, '{"sources":["knowledge","history","memory"]}');
