-- Migration 025: Financial transactions persistence + project linking
-- Run in Supabase SQL Editor

-- 1. Financial transactions — stores ALL money movements from Stripe, Whop, Mercury
CREATE TABLE IF NOT EXISTS financial_transactions (
  id            uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  external_id   text NOT NULL,
  source        text NOT NULL,  -- stripe_uvul, stripe_lba, stripe_oll, stripe_2clicks, mercury_oll, mercury_2clicks, mercury_lba, whop
  type          text NOT NULL CHECK (type IN ('sale','income','expense','refund','transfer')),
  amount        numeric(14,2) NOT NULL,  -- always positive
  currency      text NOT NULL DEFAULT 'USD',
  txn_date      timestamptz NOT NULL,
  description   text DEFAULT '',
  counterparty  text DEFAULT '',
  project_id    uuid REFERENCES projects(id) ON DELETE SET NULL,
  metadata      jsonb DEFAULT '{}',
  auto_assigned boolean DEFAULT false,
  synced_at     timestamptz DEFAULT now(),
  created_at    timestamptz DEFAULT now(),
  updated_at    timestamptz DEFAULT now(),

  CONSTRAINT uq_external_source UNIQUE (external_id, source)
);

CREATE INDEX IF NOT EXISTS idx_ft_project_id ON financial_transactions(project_id);
CREATE INDEX IF NOT EXISTS idx_ft_source ON financial_transactions(source);
CREATE INDEX IF NOT EXISTS idx_ft_date ON financial_transactions(txn_date DESC);
CREATE INDEX IF NOT EXISTS idx_ft_type ON financial_transactions(type);

-- 2. Assignment rules — auto-tag transactions to projects
CREATE TABLE IF NOT EXISTS transaction_assignment_rules (
  id            uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  field         text NOT NULL,  -- 'source', 'counterparty', 'description', 'metadata.campaign_id', etc.
  operator      text NOT NULL DEFAULT 'equals' CHECK (operator IN ('equals','contains','starts_with')),
  value         text NOT NULL,
  priority      int DEFAULT 0,  -- higher = checked first
  enabled       boolean DEFAULT true,
  created_at    timestamptz DEFAULT now(),
  updated_at    timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tar_enabled ON transaction_assignment_rules(enabled, priority DESC);

-- 3. Sync cursors — track last sync per source
CREATE TABLE IF NOT EXISTS sync_cursors (
  id              text PRIMARY KEY,  -- e.g. 'stripe_uvul', 'mercury_oll', 'whop'
  last_synced_at  timestamptz,
  last_external_id text DEFAULT '',
  metadata        jsonb DEFAULT '{}',
  updated_at      timestamptz DEFAULT now()
);

-- 4. Profitability RPC function
CREATE OR REPLACE FUNCTION fn_project_profitability(p_days int DEFAULT 30)
RETURNS TABLE (
  project_id    uuid,
  project_name  text,
  revenue_usd   numeric,
  revenue_mxn   numeric,
  expenses_usd  numeric,
  expenses_mxn  numeric,
  refunds_usd   numeric,
  transaction_count bigint
) AS $$
BEGIN
  RETURN QUERY
  SELECT
    ft.project_id,
    p.name AS project_name,
    COALESCE(SUM(CASE WHEN ft.type IN ('sale','income') AND ft.currency = 'USD' THEN ft.amount ELSE 0 END), 0) AS revenue_usd,
    COALESCE(SUM(CASE WHEN ft.type IN ('sale','income') AND ft.currency = 'MXN' THEN ft.amount ELSE 0 END), 0) AS revenue_mxn,
    COALESCE(SUM(CASE WHEN ft.type = 'expense' AND ft.currency = 'USD' THEN ft.amount ELSE 0 END), 0) AS expenses_usd,
    COALESCE(SUM(CASE WHEN ft.type = 'expense' AND ft.currency = 'MXN' THEN ft.amount ELSE 0 END), 0) AS expenses_mxn,
    COALESCE(SUM(CASE WHEN ft.type = 'refund' THEN ft.amount ELSE 0 END), 0) AS refunds_usd,
    COUNT(*)::bigint AS transaction_count
  FROM financial_transactions ft
  JOIN projects p ON p.id = ft.project_id
  WHERE ft.project_id IS NOT NULL
    AND ft.type != 'transfer'
    AND ft.txn_date >= (NOW() - (p_days || ' days')::interval)
  GROUP BY ft.project_id, p.name
  ORDER BY revenue_usd DESC;
END;
$$ LANGUAGE plpgsql;

-- Enable RLS (optional, since only super admin accesses these)
ALTER TABLE financial_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE transaction_assignment_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_cursors ENABLE ROW LEVEL SECURITY;

-- Service role can do everything
CREATE POLICY "service_all" ON financial_transactions FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON transaction_assignment_rules FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "service_all" ON sync_cursors FOR ALL USING (true) WITH CHECK (true);
