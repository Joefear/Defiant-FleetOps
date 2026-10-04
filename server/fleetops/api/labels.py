"""Authenticated template, durable print-request and artifact endpoints for Slice 12."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from fleetops.api.context import RequestContext
from fleetops.api.label_schemas import PrintJobOut, PrintRequest, TemplateCreate, TemplateOut
from fleetops.db.metadata import label_templates
from fleetops.labels import service
from fleetops.labels.adapters.registry import registry
from fleetops.labels.adapters.storage import OutputUnavailable


@contextmanager
def label_errors():
    """Never expose output directory names, SQL text or operating-system diagnostics."""
    try:
        yield
    except service.LabelNotFound as error:
        raise HTTPException(404, str(error)) from error
    except service.LabelConflict as error:
        raise HTTPException(409, str(error)) from error
    except OutputUnavailable as error:
        raise HTTPException(
            503, "Label output is unavailable or failed integrity checks"
        ) from error


def create_label_router(
    authenticated: Callable[..., Iterator[RequestContext]], *, output_root
) -> APIRouter:
    """Queue first, dispatch in a subsequent transaction, and expose no output paths."""
    router = APIRouter()
    Context = Annotated[RequestContext, Depends(authenticated)]
    adapters = registry(output_root)

    @router.post("/label-templates", response_model=TemplateOut, status_code=201)
    def create_template(body: TemplateCreate, context: Context):
        return service.create_template(
            context.connection, context.identity.organization_id, body.model_dump()
        )

    @router.get("/label-templates", response_model=list[TemplateOut])
    def templates(context: Context):
        return (
            context.connection.execute(select(label_templates).order_by(label_templates.c.id))
            .mappings()
            .all()
        )

    @router.post("/assets/{asset_id}/labels", response_model=PrintJobOut, status_code=202)
    def print_asset(asset_id: UUID, body: PrintRequest, context: Context):
        with label_errors():
            return service.enqueue(
                context.connection,
                context.identity.organization_id,
                asset_id,
                body.template_id,
                body.output_format,
            )

    @router.post("/receipts/{receipt_id}/labels", response_model=list[PrintJobOut], status_code=202)
    def print_receipt(receipt_id: UUID, body: PrintRequest, context: Context):
        with label_errors():
            return service.enqueue_receipt(
                context.connection,
                context.identity.organization_id,
                receipt_id,
                body.template_id,
                body.output_format,
            )

    @router.get("/print-jobs/{job_id}", response_model=PrintJobOut)
    def job(job_id: UUID, context: Context):
        with label_errors():
            return service.get_job(context.connection, job_id)

    @router.post("/print-jobs/{job_id}/dispatch", response_model=PrintJobOut)
    def dispatch(job_id: UUID, response: Response, context: Context):
        with label_errors():
            result = service.dispatch(context.connection, job_id, adapters)
            # Return a failure response without raising: the FAILED outbox record
            # must commit so a missing output directory does not erase the request.
            if result["status"] == "FAILED":
                response.status_code = 503
            return result

    @router.get("/print-jobs/{job_id}/content")
    def content(job_id: UUID, context: Context):
        with label_errors():
            job, data = service.read_artifact(context.connection, job_id, adapters)
        media = {"PNG": "image/png", "PDF": "application/pdf", "ZPL": "application/octet-stream"}
        extension = job["output_format"].lower()
        return Response(
            data,
            media_type=media[job["output_format"]],
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'attachment; filename="{job_id}.{extension}"',
            },
        )

    return router
