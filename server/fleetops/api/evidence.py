"""Authenticated raw-byte uploads, verified downloads and immutable typed links."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from fleetops.api.context import RequestContext
from fleetops.api.evidence_schemas import (
    AttachmentLinkCreate,
    AttachmentLinkOut,
    AttachmentOut,
    UploadMetadata,
)
from fleetops.evidence import service
from fleetops.evidence.storage import EvidenceStorageError


@contextmanager
def evidence_errors():
    """Translate bounded failures without revealing storage paths or SQL diagnostics."""
    try:
        yield
    except service.EvidenceNotFound as error:
        raise HTTPException(404, str(error)) from error
    except service.EvidenceConflict as error:
        raise HTTPException(409, str(error)) from error
    except service.EvidenceInvalid as error:
        raise HTTPException(422, str(error)) from error
    except EvidenceStorageError as error:
        raise HTTPException(
            503, "Evidence storage is unavailable or failed integrity checks"
        ) from error


def create_evidence_router(
    authenticated: Callable[..., Iterator[RequestContext]], *, upload_limit: int
) -> APIRouter:
    """Stream a bounded request before committing content-addressed capture metadata."""
    router = APIRouter()
    Context = Annotated[RequestContext, Depends(authenticated)]

    @router.post("/attachments", response_model=AttachmentOut, status_code=201)
    async def upload(
        request: Request, metadata: Annotated[UploadMetadata, Query()], context: Context
    ):
        """Hash raw bytes on the server; no multipart parser or filename-based path exists."""
        if context.evidence_storage is None:
            raise HTTPException(503, "Evidence storage is not configured")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > upload_limit:
                raise HTTPException(413, "Attachment exceeds the configured upload limit")
            content.extend(chunk)
        with evidence_errors():
            return await run_in_threadpool(
                service.capture,
                context.connection,
                context.evidence_storage,
                context.identity.organization_id,
                bytes(content),
                metadata.model_dump(),
            )

    @router.get("/attachments/{attachment_id}", response_model=AttachmentOut)
    def metadata(attachment_id: UUID, context: Context):
        """Read first-capture provenance, including after supersession."""
        with evidence_errors():
            return service.get_attachment(context.connection, attachment_id)

    @router.get("/attachments/{attachment_id}/content")
    def content(attachment_id: UUID, context: Context):
        """Always download inert bytes; uploaded HTML/SVG must not execute in the app origin."""
        with evidence_errors():
            row = service.get_attachment(context.connection, attachment_id)
            data = service.read_content(context.evidence_storage, row)
            return Response(
                data,
                media_type="application/octet-stream",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": "attachment; filename*=UTF-8''"
                    + quote(row["original_filename"], safe=""),
                },
            )

    @router.post(
        "/attachments/{attachment_id}/links", response_model=AttachmentLinkOut, status_code=201
    )
    def link(attachment_id: UUID, body: AttachmentLinkCreate, context: Context):
        """Append evidence purpose against a current tenant-owned record."""
        with evidence_errors():
            return service.link(
                context.connection, context.evidence_storage, attachment_id, body.model_dump()
            )

    @router.get("/attachments/{attachment_id}/links", response_model=list[AttachmentLinkOut])
    def links(attachment_id: UUID, context: Context):
        """Read immutable link observations under the shared tenant transaction."""
        with evidence_errors():
            return service.list_links(context.connection, attachment_id)

    return router
