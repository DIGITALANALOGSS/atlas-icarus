INSERT INTO tenants (tenant_id, slug)
VALUES (
  '11111111-1111-1111-1111-111111111111',
  'development'
)
ON CONFLICT (tenant_id) DO NOTHING;
