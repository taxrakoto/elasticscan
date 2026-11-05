"""Create transactional job storage"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('jobs',
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('team_id', sa.Text(), nullable=False),
    sa.Column('created_by', sa.Text(), nullable=False),
    sa.Column('scan_type', sa.Text(), nullable=False),
    sa.Column('parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('result', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('error', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('worker_id', sa.Text(), nullable=True),
    sa.Column('claim_token', sa.Uuid(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancellation_requested_by', sa.Text(), nullable=True),
    sa.Column('cancellation_requested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(status = 'completed' AND result IS NOT NULL AND jsonb_typeof(result) = 'object' AND error IS NULL) OR (status = 'failed' AND result IS NULL AND error IS NOT NULL AND jsonb_typeof(error) = 'object') OR (status IN ('pending', 'running', 'cancelled') AND result IS NULL AND error IS NULL)", name='ck_jobs_outcome'),
    sa.CheckConstraint("(status IN ('completed', 'failed', 'cancelled')) = (finished_at IS NOT NULL)", name='ck_jobs_finished'),
    sa.CheckConstraint("jsonb_typeof(parameters) = 'object'", name='ck_jobs_parameters'),
    sa.CheckConstraint("status <> 'running' OR started_at IS NOT NULL", name='ck_jobs_started'),
    sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'cancelled')", name='ck_jobs_status'),
    sa.CheckConstraint('(claim_token IS NULL) = (lease_expires_at IS NULL)', name='ck_jobs_lease'),
    sa.CheckConstraint('attempt_count >= 0', name='ck_jobs_attempt_count'),
    sa.CheckConstraint('length(btrim(scan_type)) > 0', name='ck_jobs_scan_type'),
    sa.CheckConstraint('length(btrim(team_id)) > 0 AND length(btrim(created_by)) > 0', name='ck_jobs_owner'),
    sa.PrimaryKeyConstraint('job_id')
    )
    op.create_index('ix_jobs_recovery', 'jobs', ['status', 'lease_expires_at'], unique=False)
    op.create_index('ix_jobs_team_created', 'jobs', ['team_id', sa.literal_column('created_at DESC'), sa.literal_column('job_id DESC')], unique=False)
    op.create_table('dispatch_intents',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('schema_version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('suppressed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('claim_token', sa.Uuid(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error_code', sa.Text(), nullable=True),
    sa.CheckConstraint('(claim_token IS NULL) = (lease_expires_at IS NULL)', name='ck_dispatch_lease'),
    sa.CheckConstraint('attempt_count >= 0', name='ck_dispatch_attempt_count'),
    sa.CheckConstraint('schema_version > 0', name='ck_dispatch_schema_version'),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.job_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id')
    )
    op.create_index('ix_dispatch_due', 'dispatch_intents', ['next_attempt_at'], unique=False, postgresql_where=sa.text('published_at IS NULL AND suppressed_at IS NULL'))
    # SQLAlchemy declarations cannot protect writes from workers or raw SQL.
    op.execute("""
        CREATE FUNCTION protect_job_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.job_id, NEW.team_id, NEW.created_by, NEW.scan_type,
                   NEW.parameters, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.job_id, OLD.team_id, OLD.created_by, OLD.scan_type,
                   OLD.parameters, OLD.created_at) THEN
                RAISE EXCEPTION 'Accepted job identity and parameters are immutable'
                    USING ERRCODE = '23514';
            END IF;
            NEW.updated_at := statement_timestamp();
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER jobs_protect_identity BEFORE UPDATE ON jobs
        FOR EACH ROW EXECUTE FUNCTION protect_job_identity()
    """)


def downgrade():
    op.execute('DROP TRIGGER jobs_protect_identity ON jobs')
    op.execute('DROP FUNCTION protect_job_identity()')
    op.drop_index('ix_dispatch_due', table_name='dispatch_intents', postgresql_where=sa.text('published_at IS NULL AND suppressed_at IS NULL'))
    op.drop_table('dispatch_intents')
    op.drop_index('ix_jobs_team_created', table_name='jobs')
    op.drop_index('ix_jobs_recovery', table_name='jobs')
    op.drop_table('jobs')
