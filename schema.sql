-- Run once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run

create table if not exists public.listings (
    listing_id          bigint primary key,
    search_key          text,
    title               text,
    price_rm            integer,
    initial_price_rm    integer,
    previous_price_rm   integer,
    price_changed_at    timestamptz,
    psf_rm              numeric,
    floor_area_sqft     integer,
    bedrooms            integer,
    bathrooms           integer,
    property_type       text,
    tenure              text,
    build_year          text,
    address             text,
    area                text,
    district            text,
    region              text,
    nearest_station     text,
    posted_on           text,
    status              text,
    agent_name          text,
    agent_license       text,
    agency_id           bigint,
    photo_count         integer,
    thumbnail           text,
    url                 text,
    -- filled from the listing's own page
    latitude            double precision,
    longitude           double precision,
    postcode            text,
    furnishing          text,
    furnishing_items    text,
    tenancy             text,
    facilities          text,
    amenities           text,
    developer           text,
    completed_year      text,
    title_type          text,
    headline            text,
    description         text,
    first_posted        timestamp,
    last_updated        timestamp,
    expires             timestamp,
    details_scraped_at  timestamptz,
    -- bookkeeping
    first_seen          timestamptz not null default now(),
    last_seen           timestamptz not null default now()
);

create index if not exists listings_district_idx  on public.listings (district);
create index if not exists listings_last_seen_idx on public.listings (last_seen);

create table if not exists public.price_history (
    id          bigserial primary key,
    listing_id  bigint not null references public.listings(listing_id) on delete cascade,
    price_rm    integer not null,
    seen_at     timestamptz not null default now()
);

create index if not exists price_history_listing_idx on public.price_history (listing_id);

-- Security: logged-in users can READ. Nobody can write with the public key;
-- the scraper writes with the secret key, which bypasses these rules.
alter table public.listings      enable row level security;
alter table public.price_history enable row level security;

drop policy if exists "logged-in users read listings" on public.listings;
create policy "logged-in users read listings" on public.listings
    for select to authenticated using (true);

drop policy if exists "logged-in users read price history" on public.price_history;
create policy "logged-in users read price history" on public.price_history
    for select to authenticated using (true);
