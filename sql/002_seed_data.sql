-- ============================================================
-- Seed data — run this AFTER 001_schema.sql
-- Adjust names/emails to match reality before running.
-- ============================================================

insert into buyers (name, notification_email_sender) values
    ('Chevron', 'Chevron.Notification'),
    ('Aveon', null),      -- fill in their notification sender if they have one
    ('Hillking', null)
on conflict (name) do nothing;

insert into suppliers (name, product_line) values
    ('Flexitallic', 'gasket'),
    ('AIV', 'valve')
    -- add an 'lng' supplier here once you confirm who that is
on conflict (name) do nothing;
