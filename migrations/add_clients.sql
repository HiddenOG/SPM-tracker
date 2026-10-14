-- Extend client check constraint to include ExxonMobil, TotalEnergies, Others
-- Run in Supabase SQL Editor

ALTER TABLE quotations DROP CONSTRAINT IF EXISTS quotations_client_check;
ALTER TABLE quotations ADD CONSTRAINT quotations_client_check
  CHECK (client IN ('chevron','nlng','seplat','exxon','total','others'));
