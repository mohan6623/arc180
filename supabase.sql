-- ARC/180 shared scoreboard — run once in Supabase: SQL Editor -> New query -> paste -> Run.
-- Only the scoreboard lives in the cloud. Chats and notes never come here.

create table if not exists public.claims (
  username   text not null,
  date       date not null,
  kind       text not null default 'study',        -- study | build
  xp         int  not null default 50,
  source     text not null default 'app',          -- app | kb
  claimed_at timestamptz not null default now(),
  primary key (username, date)
);

alter table public.claims enable row level security;

-- Two-person private project: the anon key is shared only between the two
-- learners, so policies are permissive. Tighten to auth.uid() checks if you
-- add real logins or open the project up.
create policy "scoreboard read"   on public.claims for select using (true);
create policy "scoreboard insert" on public.claims for insert with check (true);
create policy "scoreboard update" on public.claims for update using (true);

-- The knowledge-base doorbell: each machine upserts its HEAD after pushing, so
-- the other one pulls immediately instead of polling on a schedule. Content
-- travels through git; only this pointer goes through the cloud.
create table if not exists public.kb_sync (
  username  text primary key,
  head      text not null,
  pushed_at timestamptz not null default now()
);

alter table public.kb_sync enable row level security;
create policy "doorbell read"   on public.kb_sync for select using (true);
create policy "doorbell insert" on public.kb_sync for insert with check (true);
create policy "doorbell update" on public.kb_sync for update using (true);

-- push changes to connected apps over websocket
alter publication supabase_realtime add table public.claims;
alter publication supabase_realtime add table public.kb_sync;
