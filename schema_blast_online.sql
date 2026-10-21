-- WhatsApp blast from the online dashboard (run once: Supabase -> SQL Editor -> paste -> Run).
-- The online page saves blasts here; the PC sends them and writes progress back.

create table if not exists public.blast_jobs (
  id text primary key,
  created_at timestamptz not null default now(),
  spec jsonb not null,                       -- what to send (numbers, message, auto replies, timing, pause / cancel)
  progress jsonb not null default '{}'::jsonb -- who got it - written by the PC
);

create table if not exists public.pc_status (
  name text primary key,                     -- 'pc'
  data jsonb not null,                       -- last heartbeat + which WhatsApp numbers are connected
  updated_at timestamptz not null default now()
);

alter table public.blast_jobs enable row level security;
alter table public.pc_status enable row level security;

-- Only you (logged in to the dashboard) can see or create blasts. The PC uses the secret key.
drop policy if exists "logged in can use blast_jobs" on public.blast_jobs;
create policy "logged in can use blast_jobs" on public.blast_jobs
  for all to authenticated using (true) with check (true);

drop policy if exists "logged in can read pc_status" on public.pc_status;
create policy "logged in can read pc_status" on public.pc_status
  for select to authenticated using (true);

-- Photos / PDFs for blasts made online go into the private bucket owner-photos, folder blast/
drop policy if exists "logged in can upload blast files" on storage.objects;
create policy "logged in can upload blast files" on storage.objects
  for insert to authenticated with check (bucket_id = 'owner-photos' and name like 'blast/%');

drop policy if exists "logged in can update blast files" on storage.objects;
create policy "logged in can update blast files" on storage.objects
  for update to authenticated using (bucket_id = 'owner-photos' and name like 'blast/%');

drop policy if exists "logged in can read blast files" on storage.objects;
create policy "logged in can read blast files" on storage.objects
  for select to authenticated using (bucket_id = 'owner-photos' and name like 'blast/%');
