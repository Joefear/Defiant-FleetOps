"""Authenticated Asset surface; receipt-owned creation and identifier capture stay deferred."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from fleetops.api.asset_schemas import (
    AssetDiscrepancy,
    AssetOut,
    AssetPatch,
    CustodyOut,
    CustodyRequest,
    IdentifierOut,
    MovementOut,
    MovementRequest,
    OwnershipOut,
    OwnershipRequest,
    PhysicalChangeRequest,
    TransitionOut,
    TransitionRequest,
)
from fleetops.api.assignment_schemas import (
    AssignmentEventOut,
    AssignmentHistoryOut,
    AssignmentRequest,
    ConfigurationOut,
    ConfigurationRequest,
)
from fleetops.api.context import RequestContext
from fleetops.domain import asset_facts, assets, assignments, configurations
from fleetops.domain.lifecycle import LifecycleInvalid


@contextmanager
def asset_errors():
    """Expose stable failures while the shared request dependency rolls back all writes."""
    try:
        yield
    except assets.AssetNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except assets.AssetConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (assets.AssetInvalid, LifecycleInvalid) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def create_asset_router(authenticated: Callable[..., Iterator[RequestContext]]) -> APIRouter:
    """Every route uses the existing bearer session and transaction-local tenant context."""
    router = APIRouter()
    Context = Annotated[RequestContext, Depends(authenticated)]

    @router.get("/assets", response_model=list[AssetOut])
    def list_assets(context: Context):
        """List only the current tenant's Assets."""
        return assets.list_assets(context.connection)

    @router.get("/assets/{asset_id}", response_model=AssetOut)
    def get_asset(asset_id: UUID, context: Context):
        """Resolve permanent UUID identity through RLS."""
        with asset_errors():
            return assets.get_asset(context.connection, asset_id)

    @router.patch("/assets/{asset_id}", response_model=AssetOut)
    def patch_asset(asset_id: UUID, body: AssetPatch, context: Context):
        """Attribute descriptive changes to the authenticated actor and database clock."""
        with asset_errors():
            return assets.patch_asset(
                context.connection,
                asset_id,
                performer_id=context.identity.actor_id,
                values=body.model_dump(exclude_unset=True),
            )

    @router.get("/assets/{asset_id}/identifiers", response_model=list[IdentifierOut])
    def identifiers(asset_id: UUID, context: Context):
        """Read capture evidence without introducing a manual identifier-write workflow."""
        with asset_errors():
            return assets.list_identifiers(context.connection, asset_id)

    @router.get("/assets/{asset_id}/transitions", response_model=list[TransitionOut])
    def transitions(asset_id: UUID, context: Context):
        """Return authoritative result-version order, independent of event clocks."""
        with asset_errors():
            return assets.list_transitions(context.connection, asset_id)

    @router.post("/assets/{asset_id}/transitions", response_model=TransitionOut, status_code=201)
    def transition(asset_id: UUID, body: TransitionRequest, context: Context):
        """Validate lifecycle legality before invoking atomic history/projection admission."""
        with asset_errors():
            return assets.transition_asset(
                context.connection,
                asset_id,
                values=body.model_dump(),
            )

    @router.post("/assets/{asset_id}/movements", response_model=MovementOut, status_code=201)
    def move(asset_id: UUID, body: MovementRequest, context: Context):
        """Move or record unknown location under the shared global Asset version."""
        with asset_errors():
            return asset_facts.move_asset(context.connection, asset_id, values=body.model_dump())

    @router.get("/assets/{asset_id}/movements", response_model=list[MovementOut])
    def movements(asset_id: UUID, context: Context):
        """Read movement history in produced-version order."""
        with asset_errors():
            return asset_facts.list_movements(context.connection, asset_id)

    @router.post("/assets/{asset_id}/custody-changes", response_model=CustodyOut, status_code=201)
    def change_custody(asset_id: UUID, body: CustodyRequest, context: Context):
        """Change custody independently of ownership and lifecycle state."""
        with asset_errors():
            return asset_facts.change_custody(
                context.connection, asset_id, values=body.model_dump()
            )

    @router.get("/assets/{asset_id}/custody-changes", response_model=list[CustodyOut])
    def custody_changes(asset_id: UUID, context: Context):
        """Read immutable prior/new custody facts."""
        with asset_errors():
            return asset_facts.list_custody_changes(context.connection, asset_id)

    @router.post(
        "/assets/{asset_id}/ownership-changes", response_model=OwnershipOut, status_code=201
    )
    def change_ownership(asset_id: UUID, body: OwnershipRequest, context: Context):
        """Change owner while retaining independent custody and location."""
        with asset_errors():
            return asset_facts.change_ownership(
                context.connection, asset_id, values=body.model_dump()
            )

    @router.get("/assets/{asset_id}/ownership-changes", response_model=list[OwnershipOut])
    def ownership_changes(asset_id: UUID, context: Context):
        """Read immutable ownership history in produced-version order."""
        with asset_errors():
            return asset_facts.list_ownership_changes(context.connection, asset_id)

    @router.post(
        "/assets/{asset_id}/assignments", response_model=AssignmentEventOut, status_code=201
    )
    def assign(asset_id: UUID, body: AssignmentRequest, context: Context):
        """Assign or reassign with one immutable event and one global produced version."""
        with asset_errors():
            return assignments.assign_asset(context.connection, asset_id, values=body.model_dump())

    @router.post(
        "/assets/{asset_id}/unassignment", response_model=AssignmentEventOut, status_code=201
    )
    def unassign(asset_id: UUID, body: PhysicalChangeRequest, context: Context):
        """Unassign using the authoritative current event, never a caller-selected prior pair."""
        with asset_errors():
            return assignments.unassign_asset(
                context.connection, asset_id, values=body.model_dump()
            )

    @router.get("/assets/{asset_id}/assignments", response_model=AssignmentHistoryOut)
    def assignment_history(asset_id: UUID, context: Context):
        """Return creation witness, immutable events, and derived historical intervals."""
        with asset_errors():
            return assignments.assignment_history(context.connection, asset_id)

    @router.post(
        "/assets/{asset_id}/configurations", response_model=ConfigurationOut, status_code=201
    )
    def configure(asset_id: UUID, body: ConfigurationRequest, context: Context):
        """Append a configuration without consuming an Asset version."""
        with asset_errors():
            return configurations.append_configuration(
                context.connection,
                asset_id,
                org_id=context.identity.organization_id,
                values=body.model_dump(),
            )

    @router.get("/assets/{asset_id}/configurations", response_model=list[ConfigurationOut])
    def configuration_history(asset_id: UUID, context: Context):
        """Read configuration allocation order without exposing global sequence values."""
        with asset_errors():
            return configurations.list_configurations(context.connection, asset_id)

    @router.get("/assets/{asset_id}/configurations/current", response_model=ConfigurationOut | None)
    def current_configuration(asset_id: UUID, context: Context):
        """Return the latest visible configuration, or NULL when none has been recorded."""
        with asset_errors():
            return configurations.current_configuration(context.connection, asset_id)

    @router.get("/health/assets/reconciliation", response_model=list[AssetDiscrepancy])
    def reconciliation(context: Context):
        """Report baseline, history and global-version disagreements without repair."""
        return asset_facts.reconcile_assets(context.connection)

    return router
