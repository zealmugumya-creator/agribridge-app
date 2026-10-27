-- Migration 0008: transporters table + system_events table
-- Run in Supabase SQL Editor on project lvloaweijidgcuezdnmu
-- Safe to re-run (IF NOT EXISTS guards)

-- ── system_events ─────────────────────────────────────────────────────────────
-- Stores all backend failures/warnings so the admin ⚠ Errors tab can show them.
CREATE TABLE IF NOT EXISTS public.system_events (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type  text        NOT NULL,
  severity    text        NOT NULL DEFAULT 'error'
              CHECK (severity IN ('error', 'warning', 'info')),
  details     jsonb,
  user_id     uuid,
  created_at  timestamptz DEFAULT now()
);
ALTER TABLE public.system_events ENABLE ROW LEVEL SECURITY;
-- Service role (backend) has full access; no client-side reads allowed.
-- TO service_role is mandatory: without it the policy applies to PUBLIC.
DROP POLICY IF EXISTS "service role full access" ON public.system_events;
CREATE POLICY "service role full access" ON public.system_events
  FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE INDEX IF NOT EXISTS system_events_created_at_idx ON public.system_events (created_at DESC);
CREATE INDEX IF NOT EXISTS system_events_severity_idx   ON public.system_events (severity, created_at DESC);

-- ── transporters ──────────────────────────────────────────────────────────────
-- One row per transporter account on the platform.
-- supabase_uid links to auth.users so the transporter can log in and toggle availability.
CREATE TABLE IF NOT EXISTS public.transporters (
  id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  supabase_uid          uuid        UNIQUE,  -- FK to auth.users (soft link; no FK constraint to avoid cross-schema issues)
  full_name             text        NOT NULL,
  phone                 text        NOT NULL,
  email                 text,
  vehicle_type          text,       -- pickup | lorry | motorcycle | minibus | van | tractor
  plate_number          text,
  district              text,
  capacity_kg           numeric,
  notes                 text,       -- MOU reference, any admin notes
  status                text        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
  is_available          boolean     NOT NULL DEFAULT false,
  availability_updated_at timestamptz,
  created_at            timestamptz DEFAULT now(),
  updated_at            timestamptz DEFAULT now()
);
ALTER TABLE public.transporters ENABLE ROW LEVEL SECURITY;

-- Backend (service role) can do everything. TO service_role is mandatory:
-- without it the policy applies to PUBLIC.
DROP POLICY IF EXISTS "service role full access" ON public.transporters;
CREATE POLICY "service role full access" ON public.transporters
  FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Transporters can read and update their OWN row only (via supabase_uid = auth.uid()).
DROP POLICY IF EXISTS "transporter read own" ON public.transporters;
CREATE POLICY "transporter read own" ON public.transporters
  FOR SELECT USING (supabase_uid = auth.uid());

DROP POLICY IF EXISTS "transporter update own availability" ON public.transporters;
CREATE POLICY "transporter update own availability" ON public.transporters
  FOR UPDATE USING (supabase_uid = auth.uid())
  WITH CHECK (supabase_uid = auth.uid());

CREATE INDEX IF NOT EXISTS transporters_district_idx    ON public.transporters (district);
CREATE INDEX IF NOT EXISTS transporters_available_idx   ON public.transporters (is_available, status);
CREATE INDEX IF NOT EXISTS transporters_supabase_uid_idx ON public.transporters (supabase_uid);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;
DROP TRIGGER IF EXISTS transporters_updated_at ON public.transporters;
CREATE TRIGGER transporters_updated_at
  BEFORE UPDATE ON public.transporters
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
