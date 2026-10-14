-- 012_message_pdf.sql — add PDF attachment URL to messages
alter table messages add column if not exists po_pdf_url text;
