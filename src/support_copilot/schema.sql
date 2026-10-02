CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'support_copilot_reader') THEN
    CREATE ROLE support_copilot_reader NOLOGIN;
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS customers (
  id BIGSERIAL PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
  id BIGSERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  format TEXT NOT NULL CHECK (format IN ('Blu-ray', 'DVD', '4K UHD', 'box set')),
  sku TEXT NOT NULL UNIQUE,
  price_cents INTEGER NOT NULL CHECK (price_cents >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
  id BIGSERIAL PRIMARY KEY,
  order_number TEXT NOT NULL UNIQUE,
  customer_id BIGINT NOT NULL REFERENCES customers(id),
  product_id BIGINT NOT NULL REFERENCES products(id),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  ordered_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'delivered',
  refund_status TEXT NOT NULL DEFAULT 'none' CHECK (refund_status IN ('none', 'approved', 'rejected'))
);

-- Durable run records are the source of truth; Redis is a queue and read cache.
CREATE TABLE IF NOT EXISTS support_runs (
  run_id TEXT PRIMARY KEY,
  payload JSONB NOT NULL,
  decision TEXT CHECK (decision IN ('approve', 'reject')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One simulated approved action per order, regardless of conversation/run.
CREATE TABLE IF NOT EXISTS refund_actions (
  order_number TEXT PRIMARY KEY REFERENCES orders(order_number),
  action TEXT NOT NULL DEFAULT 'simulated_refund' CHECK (action = 'simulated_refund'),
  request_id TEXT NOT NULL UNIQUE,
  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Adopt legacy simulated approvals without pretending they were paid transactions.
INSERT INTO refund_actions (order_number, request_id, amount_cents)
SELECT o.order_number, 'legacy:' || o.order_number, o.quantity * p.price_cents
FROM orders o JOIN products p ON p.id = o.product_id
WHERE o.refund_status = 'approved'
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS refund_decisions (
  request_id TEXT PRIMARY KEY,
  order_number TEXT NOT NULL,
  approved BOOLEAN NOT NULL,
  outcome TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runtime_settings (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL CHECK (value >= 0)
);

CREATE TABLE IF NOT EXISTS thread_owners (
  thread_id TEXT PRIMARY KEY,
  owner TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS help_document_embeddings (
  id BIGSERIAL PRIMARY KEY,
  document_name TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}',
  embedding vector({{EMBEDDING_DIM}}) NOT NULL,
  document_fingerprint TEXT NOT NULL,
  UNIQUE (document_name, chunk_index)
);

REVOKE ALL ON customers, products, orders FROM support_copilot_reader;
GRANT SELECT ON customers, products, orders TO support_copilot_reader;
GRANT support_copilot_reader TO CURRENT_USER;
