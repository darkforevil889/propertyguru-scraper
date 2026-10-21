-- AI call on the online dashboard (run once: Supabase -> SQL Editor -> paste -> Run).
-- The PC copies checked calls here; your ✔ Keep ticks are saved here; "Check calls" online asks the PC via pc_tasks.

create table if not exists public.ai_calls (
  call_id text primary key,
  data jsonb not null,          -- phone, when, what they said, verdict, reason, transcripts
  keep boolean,                 -- your tick (null = not ticked by you yet -> the AI's verdict is used)
  keep_at timestamptz,
  updated_at timestamptz not null default now()
);

create table if not exists public.pc_tasks (
  id bigserial primary key,
  kind text not null,           -- 'ai_call'
  params jsonb not null default '{}'::jsonb,
  status text not null default 'pending',
  created_at timestamptz not null default now()
);

alter table public.ai_calls enable row level security;
alter table public.pc_tasks enable row level security;

drop policy if exists "logged in can use ai_calls" on public.ai_calls;
create policy "logged in can use ai_calls" on public.ai_calls
  for all to authenticated using (true) with check (true);

drop policy if exists "logged in can use pc_tasks" on public.pc_tasks;
create policy "logged in can use pc_tasks" on public.pc_tasks
  for all to authenticated using (true) with check (true);

-- Call recordings (owner-photos/ai_call/...) can be played by you when logged in
drop policy if exists "logged in can read ai_call audio" on storage.objects;
create policy "logged in can read ai_call audio" on storage.objects
  for select to authenticated using (bucket_id = 'owner-photos' and name like 'ai_call/%');
