-- Add company discriminator to quotations so SPM and Daniel Mag quotes are tracked separately.
-- Existing rows default to 'spm'.
ALTER TABLE quotations ADD COLUMN IF NOT EXISTS company TEXT NOT NULL DEFAULT 'spm';
