-- The sample table the example reacts to. Replace it with your own.
--
-- `phone` must be in E.164 format (+ and the country code, no spaces).
-- `whatsapp_opt_in` is the person's consent to be messaged on WhatsApp. It defaults to false, and the
-- function never sends to a row where it is not true. Collect it before you set it.
create table public.orders (
  id bigint generated always as identity primary key,
  phone text,
  whatsapp_opt_in boolean not null default false,
  status text not null default 'received',
  created_at timestamptz not null default now()
);

-- Supabase exposes `public` through its API: keep the table closed until you write your own policies.
alter table public.orders enable row level security;
