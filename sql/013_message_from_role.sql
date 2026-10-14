-- 013_message_from_role.sql — store sender role so recipients can reply
alter table messages add column if not exists from_role text
  check (from_role in ('admin','procurement','warehouse','expeditor','accounts'));
