-- Run once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run
-- Owner listings pulled from WhatsApp messages by the dashboard's WhatsApp page.

create table if not exists public.owner_listings (
    message_id      text primary key,          -- WhatsApp message id (stops duplicates on re-scan)
    chat            text,
    posted_at       timestamptz,
    sender_name     text,
    sender_phone    text,
    listing_type    text,                      -- Rent / Sale
    price_rm        bigint,
    sqft            integer,
    bedrooms        integer,
    bathrooms       integer,
    furnishing      text,
    place           text,
    phones          text,                      -- numbers mentioned in the message, '; '-separated
    photo_count     integer,
    message_text    text,
    scanned_by      text,
    scanned_at      timestamptz not null default now()
);

create index if not exists owner_listings_posted_idx on public.owner_listings (posted_at);

alter table public.owner_listings enable row level security;

drop policy if exists "logged-in users read owner listings" on public.owner_listings;
create policy "logged-in users read owner listings" on public.owner_listings
    for select to authenticated using (true);

drop policy if exists "logged-in users add owner listings" on public.owner_listings;
create policy "logged-in users add owner listings" on public.owner_listings
    for insert to authenticated with check (true);

drop policy if exists "logged-in users update owner listings" on public.owner_listings;
create policy "logged-in users update owner listings" on public.owner_listings
    for update to authenticated using (true) with check (true);

-- Photos: stored in a PRIVATE storage bucket; only logged-in users can see / add them
alter table public.owner_listings add column if not exists photo_paths jsonb not null default '[]'::jsonb;

insert into storage.buckets (id, name, public)
values ('owner-photos', 'owner-photos', false)
on conflict (id) do nothing;

drop policy if exists "logged-in users read owner photos" on storage.objects;
create policy "logged-in users read owner photos" on storage.objects
    for select to authenticated using (bucket_id = 'owner-photos');

drop policy if exists "logged-in users add owner photos" on storage.objects;
create policy "logged-in users add owner photos" on storage.objects
    for insert to authenticated with check (bucket_id = 'owner-photos');

drop policy if exists "logged-in users replace owner photos" on storage.objects;
create policy "logged-in users replace owner photos" on storage.objects
    for update to authenticated using (bucket_id = 'owner-photos') with check (bucket_id = 'owner-photos');
