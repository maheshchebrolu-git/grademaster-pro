ALTER TABLE grading_audit
ADD COLUMN IF NOT EXISTS section TEXT,
ADD COLUMN IF NOT EXISTS canvas_student_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_assignment_student_id
ON grading_audit (assignment_id, student_id);
