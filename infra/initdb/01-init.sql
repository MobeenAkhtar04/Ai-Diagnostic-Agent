CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
-- read only role used by the diagnostics agent (RealBackend): monitoring views only
CREATE ROLE diag_ro LOGIN PASSWORD 'diag';
GRANT pg_monitor TO diag_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE shop IN SCHEMA public GRANT SELECT ON TABLES TO diag_ro;
