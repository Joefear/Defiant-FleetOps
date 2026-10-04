"""Immutable templates and a durable local-print outbox, scoped by authenticated RLS."""

from sqlalchemy import insert, select, update
from uuid6 import uuid7

from fleetops.api.label_schemas import TemplateCreate
from fleetops.db.metadata import assets, label_templates, print_jobs, receipt_lines, receipts
from fleetops.labels.adapters.registry import adapter_for
from fleetops.labels.adapters.storage import OutputUnavailable
from fleetops.labels.render import render


class LabelNotFound(Exception):
    """Invisible and nonexistent targets have the same external result."""


class LabelConflict(Exception):
    """The durable job cannot satisfy the requested operation in its present state."""


def create_template(connection, organization, values):
    """Validate again for internal callers; PostgreSQL independently enforces D14."""
    body = TemplateCreate.model_validate(values)
    return (
        connection.execute(
            insert(label_templates)
            .values(id=uuid7(), org_id=organization, **body.model_dump())
            .returning(label_templates)
        )
        .mappings()
        .one()
    )


def get_template(connection, template_id):
    row = (
        connection.execute(select(label_templates).where(label_templates.c.id == template_id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise LabelNotFound("Label template not found")
    return row


def enqueue(connection, organization, asset_id, template_id, output_format):
    """Create PENDING inside the request transaction; no output is dispatched here.

    An immutable database-derived snapshot separates a later print retry from mutable
    descriptive metadata. Labels do not participate in Asset global versioning.
    """
    get_template(connection, template_id)
    if (
        connection.execute(select(assets.c.id).where(assets.c.id == asset_id)).scalar_one_or_none()
        is None
    ):
        raise LabelNotFound("Asset not found")
    return (
        connection.execute(
            insert(print_jobs)
            .values(
                id=uuid7(),
                org_id=organization,
                template_id=template_id,
                entity_id=asset_id,
                output_format=output_format,
                adapter_name=adapter_for(output_format),
            )
            .returning(print_jobs)
        )
        .mappings()
        .one()
    )


def enqueue_receipt(connection, organization, receipt_id, template_id, output_format):
    """Label actual serialized units only, never ordered or nonserialized quantities.

    Each receiving line already represents one serialized unit. Original receipt links
    remain provenance after correction; no fictional Asset is inferred from totals.
    """
    get_template(connection, template_id)
    if (
        connection.execute(
            select(receipts.c.id).where(receipts.c.id == receipt_id)
        ).scalar_one_or_none()
        is None
    ):
        raise LabelNotFound("Receipt not found")
    targets = (
        connection.execute(
            select(receipt_lines.c.asset_id)
            .where(
                receipt_lines.c.receipt_id == receipt_id,
                receipt_lines.c.asset_id.is_not(None),
            )
            .distinct()
            .order_by(receipt_lines.c.asset_id)
            .limit(501)
        )
        .scalars()
        .all()
    )
    if len(targets) > 500:
        raise LabelConflict("Receipt label batch exceeds 500 Assets")
    return [
        enqueue(connection, organization, target, template_id, output_format) for target in targets
    ]


def get_job(connection, job_id, *, lock=False):
    """All lookups use ordinary tenant-scoped access, including the retry row lock."""
    query = select(print_jobs).where(print_jobs.c.id == job_id)
    if lock:
        query = query.with_for_update()
    row = connection.execute(query).mappings().one_or_none()
    if row is None:
        raise LabelNotFound("Print job not found")
    return row


def dispatch(connection, job_id, adapters):
    """Serialize duplicate dispatches and publish the deterministic job-addressed file.

    The queue transaction must already have committed. A file surviving a delivery
    transaction rollback is reused byte-for-byte on retry. This local adapter contract
    makes no exactly-once promise about future physical/network printer integrations.
    """
    job = get_job(connection, job_id, lock=True)
    adapter = adapters.get(job["adapter_name"])
    if job["status"] == "SUCCEEDED":
        read_artifact(connection, job_id, adapters)
        return job
    status, digest, error_code = "FAILED", None, None
    if adapter is None:
        error_code = "OUTPUT_UNAVAILABLE"
    else:
        try:
            label = render(job["render_snapshot"])
        except (ValueError, TypeError, KeyError, RuntimeError):
            error_code = "RENDER_FAILED"
        else:
            try:
                digest = adapter.publish(job["org_id"], job["id"], label, job["output_format"])
                status = "SUCCEEDED"
            except OutputUnavailable:
                error_code = "OUTPUT_UNAVAILABLE"
    return (
        connection.execute(
            update(print_jobs)
            .where(print_jobs.c.id == job_id)
            .values(status=status, artifact_sha256=digest, error_code=error_code)
            .returning(print_jobs)
        )
        .mappings()
        .one()
    )


def read_artifact(connection, job_id, adapters):
    """Require a successful durable record and verify actual bytes on every download."""
    job = get_job(connection, job_id)
    if job["status"] != "SUCCEEDED":
        raise LabelConflict("Print output is not ready")
    adapter = adapters.get(job["adapter_name"])
    if adapter is None:
        raise OutputUnavailable("Output storage is unavailable")
    return job, adapter.read(job["org_id"], job["id"], job["output_format"], job["artifact_sha256"])
