-- Run these in the Supabase SQL Editor (Dashboard > SQL Editor)

-- Table: qr_codes (stores QR codes for registered users)
CREATE TABLE qr_codes (
  id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  target_url  TEXT        NOT NULL,
  file_name   TEXT        NOT NULL,
  image_url   TEXT        NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Table: api_keys (one active key per user)
CREATE TABLE api_keys (
  id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  api_key     TEXT        UNIQUE NOT NULL,
  is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Enable Row Level Security (the backend uses the service role key,
-- so RLS won't block server-side queries, but it protects direct DB access)
ALTER TABLE qr_codes  ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys  ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own QR codes"
  ON qr_codes FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own QR codes"
  ON qr_codes FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can view own API keys"
  ON api_keys FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Users can manage own API keys"
  ON api_keys FOR ALL USING (auth.uid() = user_id);
