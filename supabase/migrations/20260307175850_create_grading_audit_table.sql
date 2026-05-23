CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS grading_audit (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- context
    student_id TEXT NOT NULL,
    student_name TEXT,
    assignment_id TEXT NOT NULL,
    assignment_name TEXT,
    
    -- edge cases (directly in the main block now)
    submission_type TEXT,        -- 'single_file', 'multi_file', 'no_submission'
    attempt_number INT DEFAULT 1,
    file_paths TEXT[],           -- local mac paths for my audit trail
    
    -- cloud processing
    batch_id TEXT,
    status TEXT DEFAULT 'pending', 
    
    -- ai outputs
    raw_analysis_json JSONB,
    internal_ai_justification TEXT,
    final_score INT,
    ta_comment TEXT,              -- the strict 6-7 word feedback
    confidence_score FLOAT,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);