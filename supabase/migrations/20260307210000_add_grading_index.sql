CREATE UNIQUE INDEX IF NOT EXISTS idx_assignment_student
ON grading_audit (assignment_id, student_name);
