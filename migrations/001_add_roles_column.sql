-- Add roles column to users table
ALTER TABLE users ADD COLUMN roles VARCHAR(255) NOT NULL DEFAULT 'employee';

-- Update existing users to have the default role
UPDATE users SET roles = 'employee' WHERE roles IS NULL;