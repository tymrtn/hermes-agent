-- Sample board matching the live kanban schema subset the adapter reads
-- (verified against ~/.hermes/kanban/boards/*/kanban.db on 2026-07-11).
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    body TEXT,
    assignee TEXT,
    status TEXT,
    priority INTEGER,
    created_at INTEGER,
    completed_at INTEGER,
    project_id TEXT
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    run_id INTEGER,
    kind TEXT,
    payload TEXT,
    created_at INTEGER
);
INSERT INTO tasks (id, title, body, assignee, status, priority, created_at, completed_at, project_id) VALUES
  ('T-1001', 'Repair the board notification subscription', 'sample', 'nagatha', 'done', 2, 1783300000, 1783600000, 'hermes-continuity'),
  ('T-1002', 'Offload the long-running collection step', 'sample', 'nagatha', 'blocked', 1, 1783300000, NULL, 'hermes-continuity'),
  ('T-1003', 'Wire the retriever hook for fresh sessions', 'sample', 'nagatha', 'todo', 2, 1783300000, NULL, 'hermes-continuity');
INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES
  ('T-1001', 1, 'status_change', '{"from":"running","to":"done"}', 1783600000);
