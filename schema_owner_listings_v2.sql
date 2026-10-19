-- Run once in Supabase: SQL Editor -> New query -> paste -> Run
-- Adds owner name, unit number and the other details read from owner PDFs / catalogue screenshots.
alter table public.owner_listings
    add column if not exists owner_name    text,
    add column if not exists unit_no       text,
    add column if not exists address       text,
    add column if not exists rental_rm     bigint,
    add column if not exists tenure        text,
    add column if not exists property_type text,
    add column if not exists price_psf     numeric,
    add column if not exists owner_no      integer;

-- A new scan replaces that chat's previously saved listings, so logged-in users may delete rows
drop policy if exists "logged-in users delete owner listings" on public.owner_listings;
create policy "logged-in users delete owner listings" on public.owner_listings
    for delete to authenticated using (true);

-- Make Supabase pick up the new columns straight away
notify pgrst, 'reload schema';
